#!/usr/bin/env node
// 把某個 chat channel 在 OpenClaw 本機留下的全部對話撈出來。
//
//   node dump-channel.js --tap ../.tap
//   node dump-channel.js --channel telegram --target -100XXXXXXXXXX --format md
//   node dump-channel.js --tap ../.tap --user-only --format txt
//
// .tap 長這樣（本身不進版控，裡面有頻道 id）：
//   channel: telegram
//   target: -100XXXXXXXXXX
//   since: 2026-08-11 15:58      ← 上次檢討看到哪裡；# 開頭的行是註解
//
// since 會自動生效：只印那個時間點之後的紀錄，要看全部就 --since 0。
// 每做完一輪檢討就把 since 推到當下——它是接力棒，不是紀念碑。
//
// 為什麼要有這支：openclaw CLI 沒有「讀取頻道歷史」的指令。
//   openclaw session search        → 沒有這個指令（只有 sessions list/tail/...）
//   openclaw message read --channel telegram → Unsupported Telegram action: read
//   openclaw message search        → 只支援 Discord
// 但 gateway 早就把每一輪都寫在本機了，這支把三種本機來源合併還原：
//   1. <sessionId>.trajectory.jsonl 的 prompt.submitted
//      → prompt 內含 Conversation info(metadata，有 message_id) + Conversation
//        context(前幾則，格式 "#id  時間 GMT+8 名字: 內容") + 這次的新訊息
//      → 唯一能拿到 telegram message id 與「AI 沒被叫醒也看得到的鄰近訊息」的來源
//   2. <sessionId>.jsonl 與 <sessionId>.jsonl.reset.*
//      → 乾淨的 role:user 原文，但沒有 message id
//   3. agent/codex-home/sessions/YYYY/MM/DD/rollout-*.jsonl
//      → runtime 另一份副本，補 1 已被輪替掉的部分
//
// 三份都會有缺漏（session 輪替、context window 裁切），合併後才接近完整；
// 輸出的 coverage 區塊會告訴你還缺哪些 message id。

const fs = require('fs');
const path = require('path');
const os = require('os');

// ---------- args ----------
const argv = process.argv.slice(2);
function arg(name, dflt) {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : dflt;
}
const has = (name) => argv.includes('--' + name);

let channel = arg('channel');
let target = arg('target');
let since = arg('since');
const tapBots = [];
const tapPath = arg('tap');
if (tapPath) {
  for (const line of fs.readFileSync(tapPath, 'utf8').split('\n')) {
    const m = line.match(/^\s*(channel|target|since|bot)\s*:\s*(.+?)\s*$/);
    if (!m) continue;                       // # 開頭的註解就是這樣被略過的
    if (m[1] === 'channel') channel = m[2];
    else if (m[1] === 'target') target = m[2];
    else if (m[1] === 'bot') tapBots.push(m[2]);
    else if (since === undefined) since = m[2];   // --since 蓋過 .tap
  }
}
if (!channel || !target) {
  console.error('usage: node dump-channel.js (--tap <file> | --channel <c> --target <id>) [--agent <id>] [--format md|txt|json] [--user-only] [--since "YYYY-MM-DD HH:MM"|0]');
  process.exit(2);
}

const stateDir = process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw');
const agentsDir = path.join(stateDir, 'agents');
const onlyAgent = arg('agent');
const format = arg('format', 'txt');
const userOnly = has('user-only');

// since：只看這個時間點之後的紀錄。所有時間戳都是「YYYY-MM-DD HH:MM:SS」
// 的當地時間字串，補滿到同寬之後直接字串比大小就對了，不必經過 Date。
// `--since 0`（或任何補不成日期的值）等於不過濾。
const pad = (ts) => {
  const raw = String(ts == null ? '' : ts).trim().replace('T', ' ');
  if (raw.length === 10) return raw + ' 00:00:00';   // 只有日期
  if (raw.length === 16) return raw + ':00';         // 到分
  return raw.slice(0, 19);
};
const sinceStamp = (() => {
  const raw = String(since === undefined ? '' : since).trim().replace('T', ' ');
  if (!/^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?$/.test(raw)) return null;
  return pad(raw);
})();
const localStamp = (ms) => {
  const d = new Date(ms);
  const two = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ` +
         `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
};

// target 正規化：telegram 是裸 id，discord 是 channel:<id>，session key 是
// agent:<id>:<channel>:<group|channel>:<rawId>。統一剝到裸 id 再比對。
const bareTarget = String(target).replace(/^(?:[a-z-]+:)*?([^:]+)$/i, '$1');
const CHAT_TAG = `${channel}:${bareTarget}`;              // 出現在 prompt metadata 的 chat_id
const KEY_RE = new RegExp(`^(?:agent:([^:]+):)?${channel}:(?:group|channel|thread|dm|direct|user|chat):${bareTarget.replace(/[-[\]{}()*+?.,\\^$|#]/g, '\\$&')}$`);

// ---------- collect ----------
const byId = new Map();      // telegram message id -> {id, ts, sender, text}
const noId = [];             // {ts, iso, sender, text}  來自 transcript，沒有 id

function addById(id, ts, sender, text) {
  const k = String(id);
  const cur = byId.get(k);
  if (!cur) byId.set(k, { id: Number(k), ts, sender, text });
  else if (text && text.length > (cur.text || '').length) cur.text = text;
}

// 從一段 prompt 文字裡榨出 metadata + context 各則 + 本次新訊息
function harvestPrompt(p) {
  if (!p.includes(CHAT_TAG)) return;
  let meta = null;
  const m = p.match(/Conversation info \(untrusted metadata\):\s*```json\s*([\s\S]*?)```/);
  if (m) { try { meta = JSON.parse(m[1]); } catch {} }
  if (meta && meta.chat_id && meta.chat_id !== CHAT_TAG) return;

  let tail = null;
  const ctxIdx = p.indexOf('Conversation context (untrusted');
  if (ctxIdx >= 0) {
    const block = p.slice(ctxIdx);
    for (const ln of block.split('\n')) {
      const cm = ln.match(/^#(\d+)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) GMT\+\d+ ([^:]+): (.*)$/);
      if (cm) addById(cm[1], cm[2], cm[3].trim(), cm[4]);
    }
    const parts = block.split('\n\n');
    tail = parts[parts.length - 1].trim();
  } else if (m) {
    tail = p.slice(p.indexOf(m[0]) + m[0].length).trim();
  }
  if (meta && meta.message_id && tail != null) {
    const ts = String(meta.timestamp || '').replace(/^\w{3} /, '').replace(/ GMT\+\d+$/, '');
    addById(meta.message_id, ts, (meta.sender && meta.sender.name) || '?', tail);
  }
}

// 深走一個 JSON 物件，對每個含 CHAT_TAG 的字串試 harvestPrompt
function harvestJsonLine(line) {
  let o; try { o = JSON.parse(line); } catch { return; }
  const stack = [o];
  while (stack.length) {
    const v = stack.pop();
    if (typeof v === 'string') { if (v.includes(CHAT_TAG)) harvestPrompt(v); }
    else if (v && typeof v === 'object') for (const k of Object.keys(v)) stack.push(v[k]);
  }
}

function walk(dir, cb) {
  let ents; try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
  for (const e of ents) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, cb); else cb(p);
  }
}

const agentIds = (onlyAgent ? [onlyAgent] : fs.readdirSync(agentsDir))
  .filter((a) => fs.existsSync(path.join(agentsDir, a)));

const stats = { trajectoryFiles: 0, transcriptFiles: 0, rolloutFiles: 0 };

for (const agentId of agentIds) {
  const root = path.join(agentsDir, agentId);

  // (1)+(3) 任何 jsonl 裡帶 chat_id 的 prompt
  walk(root, (f) => {
    if (!/\.jsonl(\.reset\..*)?$/.test(f)) return;
    let txt; try { txt = fs.readFileSync(f, 'utf8'); } catch { return; }
    if (!txt.includes(CHAT_TAG)) return;
    if (f.includes('codex-home')) stats.rolloutFiles++;
    else if (f.endsWith('.trajectory.jsonl')) stats.trajectoryFiles++;
    for (const line of txt.split('\n')) if (line.includes(CHAT_TAG)) harvestJsonLine(line);
  });

  // (2) transcript：先用 trajectory 首行把 sessionId -> sessionKey 建表
  const sessDir = path.join(root, 'sessions');
  if (!fs.existsSync(sessDir)) continue;
  const sidKey = new Map();
  for (const f of fs.readdirSync(sessDir)) {
    if (!f.endsWith('.trajectory.jsonl')) continue;
    const first = fs.readFileSync(path.join(sessDir, f), 'utf8').split('\n', 1)[0];
    try { const o = JSON.parse(first); if (o.sessionId && o.sessionKey) sidKey.set(o.sessionId, o.sessionKey); } catch {}
  }
  for (const f of fs.readdirSync(sessDir)) {
    const m = f.match(/^([0-9a-f-]{36})\.jsonl(\.reset\..*)?$/);
    if (!m) continue;
    const key = sidKey.get(m[1]);
    if (!key || !KEY_RE.test(key)) continue;
    stats.transcriptFiles++;
    for (const line of fs.readFileSync(path.join(sessDir, f), 'utf8').split('\n')) {
      if (!line.trim()) continue;
      let o; try { o = JSON.parse(line); } catch { continue; }
      if (o.type !== 'message') continue;
      const msg = o.message;
      if (!msg || msg.role !== 'user' || typeof msg.content !== 'string') continue;
      if (msg.sourceChannel !== channel) continue;
      noId.push({ ts: msg.timestamp, iso: new Date(msg.timestamp).toISOString(),
                  sender: msg.senderName || '?', text: msg.content });
    }
  }
}

// ---------- merge ----------
const all = [...byId.values()].sort((a, b) => a.id - b.id);
const knownText = new Set(all.map((r) => r.text.trim()));
const seen = new Set();
const extras = noId
  .sort((a, b) => a.ts - b.ts)
  .filter((r) => {
    const t = r.text.trim();
    if (knownText.has(t)) return false;
    const k = r.ts + '|' + t;
    if (seen.has(k)) return false;
    seen.add(k); return true;
  });

const missing = [];
if (all.length) for (let i = all[0].id; i <= all[all.length - 1].id; i++) if (!byId.has(String(i))) missing.push(i);

// --user-only 只留人類發言。機器人叫什麼名字每個人不一樣：先看 --bot
// （可重複），再看 .tap 的 bot:，都沒有才退回「只留出現次數最多的那個
// 發話者」這個粗略猜法——猜錯會整份濾錯，所以 .tap 裡該寫就寫。
const botNames = argv
  .reduce((acc, a, i) => (a === '--bot' ? [...acc, argv[i + 1]] : acc), [])
  .concat(tapBots);
let excluded = new Set(botNames);
if (userOnly && excluded.size === 0) {
  const counts = new Map();
  for (const r of all) counts.set(r.sender, (counts.get(r.sender) || 0) + 1);
  const human = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
  if (human) excluded = new Set([...counts.keys()].filter((n) => n !== human[0]));
}
let rows = all.filter((r) => !userOnly || !excluded.has(r.sender));
let shown = extras;
if (sinceStamp) {
  rows = rows.filter((r) => pad(r.ts) >= sinceStamp);
  shown = shown.filter((r) => localStamp(r.ts) >= sinceStamp);
}

// ---------- output ----------
const cov = {
  channel, target: bareTarget, agents: agentIds,
  since: sinceStamp, shown: rows.length + shown.length,
  files: stats,
  recovered: all.length, withId: all.length, extraWithoutId: extras.length,
  idRange: all.length ? [all[0].id, all[all.length - 1].id] : null,
  missingIds: missing,
  firstTs: all.length ? all[0].ts : null,
  lastTs: all.length ? all[all.length - 1].ts : null,
};

if (format === 'json') {
  console.log(JSON.stringify({ coverage: cov, messages: rows, extras: shown }, null, 1));
} else if (format === 'md') {
  console.log(`# ${channel}:${bareTarget}\n`);
  console.log(`| # | 時間 | 發話者 | 內容 |\n|---|---|---|---|`);
  for (const r of rows) console.log(`| ${r.id} | ${r.ts} | ${r.sender} | ${r.text.replace(/\|/g, '\\|').replace(/\n/g, '<br>')} |`);
  if (shown.length) {
    console.log(`\n## 無 message id（僅存在於 transcript）\n`);
    for (const r of shown) console.log(`- \`${r.iso}\` **${r.sender}**：${r.text.replace(/\n/g, ' ⏎ ')}`);
  }
} else {
  for (const r of rows) console.log(`#${r.id}\t${r.ts}\t${r.sender}\t${JSON.stringify(r.text)}`);
  if (shown.length) {
    console.log('\n=== extra (transcript only, no message id) ===');
    for (const r of shown) console.log(`${r.iso}\t${r.sender}\t${JSON.stringify(r.text)}`);
  }
}
console.error('[coverage] ' + JSON.stringify(cov));
