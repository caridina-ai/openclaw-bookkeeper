#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
#
# 記帳 route：openclaw-chat-interceptor script
# Claude Opus 5.0
#
# 認得的固定格式直接呼叫 book.py 的引擎（0 token）；認不得的 exit 20 交還 AI。
#
# 協定（見 openclaw-chat-interceptor/docs/AUTHORING.md）：
#   stdin  ← JSON { body, channel, target, senderId, sessionKey, agentId }
#   stdout → 回給使用者的純文字（空字串＝處理掉但不回話）
#   exit 0 → 處理掉了 / exit 20 → 我不會，交給 AI / 其他 → 出錯
#
# 環境變數（正常安裝**一個都不用設**）：
#   BOOKKEEPING_DEBUG  1/true 時把每一輪寫進 log
#   OPENCLAW_NODE      node 執行檔（預設 "node"，走 PATH）
#   OPENCLAW_MJS       openclaw.mjs 路徑。平常自己在 PATH 上找得到；
#                      找不到就不分類（exit 20 交給 AI），不會壞
#
# 自己的檔案寫死，別人的才需要找：
#   book.py  → 固定在本檔旁邊（同一個 skill 安裝單元）
#   book.db  → 固定在 ~/.openclaw/workspace/bookkeeping/（見 book.py DB_PATH）
#   openclaw → 別人裝的，位置因機器而異，所以自動偵測 ＋ 保留覆寫逃生口
#
# 鐵則（AUTHORING.md §6）：有副作用的動作一旦做了就絕對不可以 exit 20。
# 所以流程嚴格分成兩段：先把整則訊息全部解析＋科目全部決定，
# 任何一步不確定就在動手前 exit 20；進入執行段之後只會 exit 0 或非 0。

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

EXIT_OK = 0
EXIT_PASS = 20

try:
    # newline="" 關掉 Windows 的 \n → \r\n 轉換：回覆是要送進聊天室的字串，
    # 不是主控台輸出，夾帶 \r 沒有意義。
    sys.stdout.reconfigure(encoding="utf-8", newline="")
except Exception:
    pass


class Decline(Exception):
    """還沒動手，交給 AI。"""


def fail(message):
    sys.stderr.write(str(message).rstrip() + "\n")
    sys.exit(1)


# ---------------------------------------------------------------- book.py

def load_book():
    """book.py 就在本檔旁邊——它們是同一個 skill 安裝單元，一起被複製。

    路徑寫死，**沒有設定項**。這是我們自己的檔案，位置由我們決定；
    開成 config 只是讓安裝者有機會填錯，不是彈性。
    （對照 openclaw.mjs：那是別人的檔案，裝在哪真的因機器而異，才需要找。）
    """
    path = Path(__file__).resolve().parent / "book.py"
    if not path.is_file():
        fail(f"book.py not found next to intercept.py: {path}")
    spec = importlib.util.spec_from_file_location("book", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_book(book, argv):
    """呼叫 book.main()，攔下它印給使用者的字。

    走 main() 而不是內部函式，是為了讓 script 這條路和 AI 那條路
    （AI 直接 `uv run book.py ...`）產出逐字相同的輸出。
    """
    buffer = io.StringIO()
    saved = sys.argv
    sys.argv = ["book.py"] + [str(x) for x in argv]
    try:
        with contextlib.redirect_stdout(buffer):
            book.main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            fail(buffer.getvalue() or f"book.py exited {exc.code}")
    finally:
        sys.argv = saved
    return buffer.getvalue().rstrip("\n")


# ---------------------------------------------------------------- 正規化

PUNCT = "，,。；;！!？?　"


def normalize(text):
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(str.maketrans("０１２３４５６７８９：／", "0123456789:/"))
    return text


def squeeze(line):
    return " ".join(line.split())


# ---------------------------------------------------------------- 尾巴掃描

# 贅詞白名單。缺一個詞的代價只是那句話 exit 20 交給 AI（結果仍正確、只是花
# token），所以可以慢慢長；這跟入金關鍵字的漏詞後果不同，見 book.INCOME_WORDS。
FILLERS = [
    "時間", "幫我", "記在", "記做", "記於", "記", "改在", "改", "歸在", "歸",
    "算在", "算", "列在", "放在", "放", "是", "在", "的", "了", "好", "於", "喔",
]

# 刻意不要求前面有空白：「記在19:30」「記在8/5」正是要吃下來的講法。
# 誤剝的代價很小——剩下的核心會比對失敗，整句 exit 20 交給 AI。
RE_TIME = re.compile(r"(\d{1,2}):(\d{2})$")
RE_YMD = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
RE_MD = re.compile(r"(\d{1,2})[-/](\d{1,2})$")
RELATIVE_DAYS = {"今天": 0, "昨天": -1, "前天": -2, "大前天": -3, "明天": 1}


def strip_tail(line, category_names):
    """從尾端逐一剝掉 時間／日期／科目／贅詞，回傳 (剩下的核心, 資訊)。

    剝到剝不動為止；剩下的核心必須自己站得住（是一筆帳），否則呼叫端 exit 20。
    白名單外的字剝不掉，自然就會讓核心比對失敗——這正是我們要的保守行為。
    """
    info = {"date": None, "time": None, "category": None}
    names = sorted(category_names, key=len, reverse=True)
    for _ in range(24):
        before = line
        line = line.rstrip().rstrip(PUNCT).rstrip()

        match = RE_TIME.search(line)
        if match and info["time"] is None:
            info["time"] = f"{int(match.group(1)):02d}:{match.group(2)}"
            line = line[: match.start()]
            continue

        match = RE_YMD.search(line)
        if match and info["date"] is None:
            info["date"] = "%04d-%02d-%02d" % tuple(int(g) for g in match.groups())
            line = line[: match.start()]
            continue

        match = RE_MD.search(line)
        if match and info["date"] is None:
            month, day = int(match.group(1)), int(match.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                info["date"] = f"{datetime.now().year:04d}-{month:02d}-{day:02d}"
                line = line[: match.start()]
                continue

        hit = next((w for w in RELATIVE_DAYS if line.endswith(w)), None)
        if hit and info["date"] is None:
            info["date"] = (
                datetime.now() + timedelta(days=RELATIVE_DAYS[hit])
            ).strftime("%Y-%m-%d")
            line = line[: -len(hit)]
            continue

        hit = next((n for n in names if line.endswith(n)), None)
        if hit and info["category"] is None:
            line = line[: -len(hit)]
            info["category"] = hit
            continue

        hit = next((w for w in FILLERS if line.endswith(w)), None)
        if hit:
            line = line[: -len(hit)]
            continue

        if line == before:
            break
    return line.strip(), info


def when(info):
    """把剝出來的日期時間組成 book.py 的 --date 參數；都沒有就回 None。"""
    date, time = info["date"], info["time"]
    if date and time:
        return f"{date} {time}"
    if date:
        return date
    if time:
        return f"{datetime.now():%Y-%m-%d} {time}"
    return None


# ---------------------------------------------------------------- 期間

CN_MONTHS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12, "元": 1,
}
RE_CN_MONTH = re.compile(r"^(元|十[一二]?|[一二三四五六七八九])月(?:份)?$")
RE_NUM_MONTH = re.compile(r"^(\d{1,2})月(?:份)?$")
RE_YM = re.compile(r"^(\d{4})[-/](\d{1,2})$")
SPLIT_PERIODS = re.compile(r"[和跟與、,，]+")


def parse_period(token):
    """一個期間 token → ('--month','YYYY-MM') 或 ('--date','YYYY-MM-DD')。"""
    token = token.strip().rstrip("的").strip()
    now = datetime.now()
    if not token or token in ("本月", "這個月", "當月", "這月"):
        return None
    if token in RELATIVE_DAYS:
        day = now + timedelta(days=RELATIVE_DAYS[token])
        return ("--date", day.strftime("%Y-%m-%d"))
    match = RE_CN_MONTH.match(token)
    if match:
        return ("--month", f"{now.year:04d}-{CN_MONTHS[match.group(1)]:02d}")
    match = RE_NUM_MONTH.match(token)
    if match and 1 <= int(match.group(1)) <= 12:
        return ("--month", f"{now.year:04d}-{int(match.group(1)):02d}")
    match = RE_YM.match(token)
    if match and 1 <= int(match.group(2)) <= 12:
        return ("--month", f"{int(match.group(1)):04d}-{int(match.group(2)):02d}")
    match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", token)
    if match:
        return ("--date", "%04d-%02d-%02d" % tuple(int(g) for g in match.groups()))
    match = re.match(r"^(\d{1,2})[-/](\d{1,2})$", token)
    if match and 1 <= int(match.group(1)) <= 12:
        return ("--date", f"{now.year:04d}-{int(match.group(1)):02d}-{int(match.group(2)):02d}")
    raise Decline()


def parse_periods(text):
    """期間字串 → book.py 旗標清單。空字串＝不指定（book.py 用當月）。"""
    text = text.strip().rstrip("的").strip()
    if not text:
        return []
    flags = []
    for token in SPLIT_PERIODS.split(text):
        if not token.strip():
            continue
        parsed = parse_period(token)
        if parsed is None:
            continue
        flags.extend(parsed)
    if flags and len({flags[i] for i in range(0, len(flags), 2)}) > 1:
        raise Decline()  # 同一次查詢不得混用月份與日期
    return flags


# ---------------------------------------------------------------- 指令

REPORT_NAMES = [
    ("依科目明細", "category-detail"), ("分類明細", "category-detail"),
    ("分帳明細", "category-detail"), ("科目明細", "category-detail"),
    ("分類統計", "report"), ("科目統計", "report"), ("支出統計", "report"),
    ("分類帳", "report"), ("分帳", "report"), ("月報", "report"), ("結算", "report"),
    ("流水帳", "detail"), ("依日期", "detail"), ("流水", "detail"), ("明細", "detail"),
]
REPORT_NAMES.sort(key=lambda pair: len(pair[0]), reverse=True)

RE_GUIDE = re.compile(r"^(說明|幫助|help|怎麼用|有哪些功能|可以說什麼)[?？]?$", re.I)
# 「上一筆」＝剛剛記的那一筆（依記入順序，不是帳目日期）。
CN_NUM = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
RE_LAST = re.compile(
    r"^(?:上|最後|最近|剛剛)(?:面)?\s*(?:那)?\s*"
    r"(\d{1,3}|[一二兩三四五六七八九十])?\s*筆(?:的)?(?:內容|帳|資料)?"
    r"(?:是什麼|是啥|長怎樣|呢)?[?？。]?$"
)
RE_ADJUST = re.compile(r"^(?:起始餘額|期初餘額|期初現金|餘額校正)\s*(\d{1,9})$")
RE_CATEGORIES = re.compile(r"^(列出所有科目|列出科目|所有科目|科目清單|科目)$")
RE_CAT_ADD = re.compile(r"^新增科目\s*(.+)$")
RE_CAT_CLEAR = re.compile(r"^(刪除全部科目|清空全部科目|清空科目|刪除所有科目)$")
RE_CAT_DELETE = re.compile(r"^刪除科目\s*(.+)$")
RE_CAT_RENAME = re.compile(r"^把\s*(.+?)\s*(?:更名|改名)為\s*(.+)$")
# 整批替換是「一段」而不是「一行」：標題行之後的每一行都是新科目名。
RE_CAT_REPLACE_HEAD = re.compile(
    r"^(?:捨棄原本的科目[，,]?\s*改用我的|全部科目改成|科目改成|科目改為"
    r"|重設科目|替換科目)\s*[：:]?\s*(.*)$"
)
SPLIT_LIST = re.compile(r"[、,，]+")
RE_IDS = re.compile(r"^(\d{1,9}(?:\s+\d{1,9})*)\s*(.*)$")
RE_DELETE_FIRST = re.compile(r"^(?:刪除|刪掉)\s*(\d{1,9}(?:\s+\d{1,9})*)$")
RE_CORE_BALANCE = re.compile(r"^(?P<item>.*?\D)\s*(?:後|完)\s*(?:剩(?:下)?)?\s*(?P<value>\d{1,9})$")
RE_CORE_AMOUNT = re.compile(r"^(?P<item>.*?\D)\s*(?P<value>\d{1,9})$")


def levenshtein(a, b):
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)
            ))
        previous = current
    return previous[-1]


def parse_line(line, names, existing_id):
    """一行 → 一個 action dict。看不懂就 raise Decline（此時還沒有副作用）。"""
    line = squeeze(line)
    if not line:
        raise Decline()

    if RE_GUIDE.match(line):
        return {"do": "book", "argv": ["guide"]}

    stripped = line.strip(PUNCT).strip()
    if "餘額" in stripped and not any(c.isdigit() for c in stripped) and len(stripped) <= 6:
        return {"do": "book", "argv": ["balance"]}

    match = RE_ADJUST.match(line)
    if match:
        return {"do": "book", "argv": ["adjust", "--balance", match.group(1)]}

    match = RE_LAST.match(line)
    if match:
        raw = match.group(1)
        count = CN_NUM.get(raw, raw) if raw else 1
        return {"do": "book", "argv": ["last", "--count", str(count)]}

    if RE_CATEGORIES.match(line):
        return {"do": "book", "argv": ["categories"]}
    if RE_CAT_CLEAR.match(line):
        return {"do": "book", "argv": ["category-clear"]}
    match = RE_CAT_ADD.match(line)
    if match:
        return {"do": "book", "argv": ["category-add", "--name", match.group(1).strip()]}
    match = RE_CAT_DELETE.match(line)
    if match:
        return {"do": "book", "argv": ["category-delete", "--name", match.group(1).strip()]}
    match = RE_CAT_RENAME.match(line)
    if match and match.group(1).strip() in names:
        return {"do": "book", "argv": [
            "category-rename", "--from", match.group(1).strip(),
            "--to", match.group(2).strip(),
        ]}

    action = parse_by_id(line, names, existing_id)
    if action is not None:
        return action

    if line.startswith("展開"):
        return parse_expand(line[2:].strip(), names)

    for name, command in REPORT_NAMES:
        if line.endswith(name):
            return {"do": "book", "argv": [command] + parse_periods(line[: -len(name)])}

    return parse_entry(line, names)


def parse_category_replace(lines):
    """整批替換科目：標題行 ＋ 之後每行一個科目名（也接受同行頓號分隔）。

        捨棄原本的科目，改用我的        科目改成 餐飲、買菜、交通
        餐飲
        買菜

    這是唯一「一整則訊息＝一個動作」的格式。必須交給 `category-replace`
    一次做完：拆成先清空再逐筆新增會在中途留下沒有科目的帳本，
    而且只要有一科仍被帳目使用，整批就該原地拒絕。
    """
    match = RE_CAT_REPLACE_HEAD.match(squeeze(lines[0]))
    if not match:
        return None
    names = [n.strip() for n in SPLIT_LIST.split(match.group(1)) if n.strip()]
    for line in lines[1:]:
        names += [n.strip() for n in SPLIT_LIST.split(squeeze(line)) if n.strip()]
    if not names:
        raise Decline()  # 只有標題沒有清單，讓 AI 去問使用者
    flags = []
    for name in names:
        flags += ["--name", name]
    return {"do": "book", "argv": ["category-replace"] + flags}


def parse_by_id(line, names, existing_id):
    """`358 居住費`、`358 359 金額 80`、`286 刪除`、`刪除 286`。"""
    match = RE_DELETE_FIRST.match(line)
    if match:
        ids, rest = match.group(1).split(), ""
    else:
        match = RE_IDS.match(line)
        if not match:
            return None
        ids, rest = match.group(1).split(), match.group(2).strip()
        if not rest:
            return None  # 純數字一行，看不出意圖，交給 AI

    if not any(existing_id(int(i)) for i in ids):
        return None  # 一個都不存在，多半根本不是編號語法

    flags = []
    for value in ids:
        flags += ["--rowid", value]

    if rest in ("刪除", "刪掉", ""):
        return {"do": "book", "argv": ["delete"] + flags}
    for word, flag in (("名稱", "--note"), ("品項", "--note"), ("金額", "--to")):
        if rest.startswith(word):
            value = rest[len(word):].strip()
            if not value:
                raise Decline()
            if flag == "--to" and not value.isdigit():
                raise Decline()
            return {"do": "book", "argv": ["edit"] + flags + [flag, value]}
    if rest in names:
        return {"do": "book", "argv": ["edit"] + flags + ["--category", rest]}
    near = [n for n in names if levenshtein(n, rest) <= 1]
    if near:
        # 打錯字的科目名絕不能被默默當成改品項名
        return {"do": "say", "text": (
            f"「{rest}」不是現有科目，你是不是要指「{near[0]}」？\n"
            f"若要改品項名請打：{' '.join(ids)} 名稱 {rest}"
        )}
    return {"do": "book", "argv": ["edit"] + flags + ["--note", rest]}


def parse_expand(rest, names):
    """`展開七月的外食費`、`展開交通費和保健費`、`展開六月和七月的交通費、保健費`。

    把科目名從字串裡依出現順序挖掉（最長優先），挖剩的就是期間。
    科目與期間的順序都照使用者說法保留。
    """
    ordered = sorted(names, key=len, reverse=True)
    chosen, remainder = [], rest
    while True:
        best = None
        for name in ordered:
            position = remainder.find(name)
            if position >= 0 and (best is None or position < best[0]):
                best = (position, name)
        if best is None:
            break
        position, name = best
        if name not in chosen:
            chosen.append(name)
        remainder = remainder[:position] + " " + remainder[position + len(name):]
    if not chosen:
        raise Decline()
    flags = []
    for name in chosen:
        flags += ["--category", name]
    # remainder 仍保留 和／、 等分隔字，parse_periods 要靠它們切多期間
    return {"do": "book", "argv": ["expand"] + flags + parse_periods(remainder)}


def parse_entry(line, names):
    """一筆帳。科目留到後面統一決定（可能要查快取或問 AI）。"""
    core, info = strip_tail(line, names)
    if not core:
        raise Decline()
    match = RE_CORE_BALANCE.match(core)
    mode = "balance"
    if not match:
        match = RE_CORE_AMOUNT.match(core)
        mode = "amount"
    if not match:
        raise Decline()
    item = match.group("item").strip().strip(PUNCT).strip()
    if not item or item.isdigit():
        raise Decline()
    # 品項不含空白。這道關卡擋掉「剝過頭」的殘骸：例如剝掉時間後剩下
    # 「理髮150 時間幫我記在1」，品項會是「理髮150 時間幫我記在」帶空白，
    # 就會被擋下來交給 AI，而不是默默記成一筆金額 1 的爛帳。
    # 代價是「Costco 牛排 500」這種帶空白的品項也走 AI，可以接受。
    if any(ch.isspace() for ch in item):
        raise Decline()
    return {
        "do": "entry", "item": item, "mode": mode,
        "value": match.group("value"), "date": when(info),
        "category": info["category"],
    }


# ---------------------------------------------------------------- 分類

def find_openclaw_mjs():
    """從 PATH 上的 openclaw 指令回推 openclaw.mjs 的位置。

    不直接跑 `openclaw`：Windows 上它是 `.cmd`，subprocess 不開 shell 就找不到，
    而開 shell 又要面對引號地獄（prompt 很長）。改成用 node 跑 .mjs——node 是
    一般 exe，PATH 找得到。所以要找的只有 .mjs 的位置，而它跟 npm 的 shim
    是鄰居，從 shim 回推就行。

    `OPENCLAW_MJS` 仍可覆寫。找不到就回 None，呼叫端會 exit 20 交給 AI，
    功能自動降級而不是壞掉。
    """
    override = os.environ.get("OPENCLAW_MJS")
    if override:
        return override if Path(override).is_file() else None
    shim = shutil.which("openclaw")
    candidates = []
    if shim:
        resolved = Path(shim).resolve()
        if resolved.suffix == ".mjs":      # Linux 的 symlink 直接指到本尊
            return str(resolved)
        base = Path(shim).parent
        candidates += [
            base / "node_modules" / "openclaw" / "openclaw.mjs",          # Windows npm
            base.parent / "lib" / "node_modules" / "openclaw" / "openclaw.mjs",
        ]
    candidates += [
        Path.home() / "AppData/Roaming/npm/node_modules/openclaw/openclaw.mjs",
        Path("/usr/local/lib/node_modules/openclaw/openclaw.mjs"),
        Path("/usr/lib/node_modules/openclaw/openclaw.mjs"),
    ]
    return next((str(c) for c in candidates if c.is_file()), None)


def classify(book, items, names):
    """問一次模型，一次判完所有未知品項。回傳 {品項: (kind, category)}。

    用 `openclaw infer model run`（不指定 --model）：吃的就是設定裡的
    agents.defaults.model，primary 掛掉會自動換 fallbacks，跟 agent turn
    享受同一套 routing。--gateway 是必要的——本機模式要自行初始化 provider，
    實測 30 秒，走已經開著的 gateway 只要 9 秒。
    """
    mjs = find_openclaw_mjs()
    if not mjs or not items:
        raise Decline()
    hints = "\n".join(
        f"{name}：{book.CATEGORY_HINTS[name]}"
        for name in names if name in book.CATEGORY_HINTS
    )
    listed = "\n".join(items)
    prompt = (
        "你是記帳分類器。判斷每個品項屬於哪個科目，或它其實是「收入」。\n\n"
        f"可用科目（只能用這些，不可自創）：\n{'、'.join(names)}\n\n"
        f"科目常見品項參考：\n{hints}\n\n"
        "規則：\n"
        "- 每個品項輸出一行，格式為 `品項=科目`\n"
        "- 若該品項是拿到錢（提款、薪水、退款等），輸出 `品項=收入`\n"
        "- 真的無法判斷才輸出 `品項=?`，不要硬塞雜費\n"
        "- 只輸出這些行，不要任何其他文字\n\n"
        f"品項：\n{listed}"
    )
    command = [
        os.environ.get("OPENCLAW_NODE", "node"), mjs,
        "infer", "model", "run", "--gateway", "--json", "--prompt", prompt,
    ]
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", timeout=45
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Decline()
    if done.returncode != 0:
        raise Decline()
    try:
        payload = json.loads(done.stdout)
        text = "\n".join(o.get("text") or "" for o in payload.get("outputs") or [])
    except (ValueError, AttributeError):
        raise Decline()

    resolved = {}
    for row in text.splitlines():
        if "=" not in row:
            continue
        item, _, answer = row.partition("=")
        item, answer = item.strip(), answer.strip()
        if item not in items:
            continue
        if answer in ("收入", "入金"):
            resolved[item] = ("in", None)
        elif answer in names:
            resolved[item] = ("out", answer)
    # 有任何一個沒判出來，整則交給 AI——寧可多花 token，也不要記錯科目
    if any(item not in resolved for item in items):
        raise Decline()
    return resolved


def resolve_entries(book, con, actions, names):
    """把每一筆帳的科目補齊。全部補齊才准動手，否則 Decline。"""
    unknown = []
    for action in actions:
        if action["do"] != "entry":
            continue
        if action["category"]:
            action["kind"] = "out"
            continue
        found = book.lookup_item(con, action["item"])
        if found:
            action["kind"], action["category"] = found
        elif action["item"] not in unknown:
            unknown.append(action["item"])
    if unknown:
        resolved = classify(book, unknown, names)
        for action in actions:
            if action["do"] == "entry" and not action.get("kind"):
                action["kind"], action["category"] = resolved[action["item"]]
    for action in actions:
        if action["do"] != "entry":
            continue
        if action["kind"] == "out" and action["category"] not in names:
            raise Decline()
        if action["kind"] == "in" and action["mode"] == "balance":
            raise Decline()  # 入金沒有倒推這回事


def entry_argv(action):
    if action["kind"] == "in":
        argv = ["in", "--note", action["item"], "--amount", action["value"]]
    else:
        argv = ["out", "--category", action["category"], "--note", action["item"]]
        argv += (["--balance"] if action["mode"] == "balance" else ["--amount"])
        argv += [action["value"]]
    if action["date"]:
        argv += ["--date", action["date"]]
    return argv


# ---------------------------------------------------------------- log

def format_reply(text):
    """多行輸出包成程式碼區塊再送出。

    送出的文字會經過 CommonMark（micromark）解析，所以 book.py report 用的
    `=======` 會被當成 setext 標題底線**整行吃掉**，`-------` 會變成分隔線；
    而且聊天室是比例字型，dwidth／rpad 對齊的欄位也會歪掉。
    包成 fence 一次解決兩件事：分隔線保留，欄位對齊。

    單行確認訊息維持純文字——它沒有對齊需求，等寬框反而礙眼。
    """
    if "\n" not in text or "```" in text:
        return text
    return "```\n" + text + "\n```"


def log(payload):
    flag = str(os.environ.get("BOOKKEEPING_DEBUG", "")).strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return
    # log 路徑沒有設定項：正常就放 gateway 的 log 目錄，跟 chat-interceptor.log
    # 作伴。自動測試會連同帳本一起導到暫存目錄。
    test_db = os.environ.get("BOOKKEEPING_TEST_DB")
    path = (Path(test_db).with_name("bookkeeping.log") if test_db
            else Path.home() / ".openclaw" / "logs" / "bookkeeping.log")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(payload, ts=datetime.now().isoformat(timespec="seconds"))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass  # log 壞掉絕不影響記帳


# ---------------------------------------------------------------- main

def main():
    # PowerShell 手動測試時 pipe 會帶 BOM，gateway 不會，但要能容錯
    raw = sys.stdin.read().lstrip("﻿").strip()
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        fail("stdin is not valid JSON")
    body = str(payload.get("body") or "")

    book = load_book()
    con = book.connect()
    names = book.categories(con)
    known_ids = {
        row[0] for row in con.execute("SELECT id FROM entries").fetchall()
    }

    # ---- 判斷段：完全沒有副作用，隨時可以 exit 20 ----
    try:
        lines = [l for l in normalize(body).split("\n") if l.strip()]
        if not lines or len(lines) > 40:
            raise Decline()
        block = parse_category_replace(lines)
        actions = [block] if block else [
            parse_line(line, names, lambda i: i in known_ids) for line in lines
        ]
        resolve_entries(book, con, actions, names)
    except Decline:
        con.close()
        log({"body": body, "result": "pass"})
        sys.exit(EXIT_PASS)
    except RecursionError:
        con.close()
        log({"body": body, "result": "pass"})
        sys.exit(EXIT_PASS)
    con.close()

    # ---- 執行段：從這裡起只會 exit 0 或非 0，絕不 exit 20 ----
    out = []
    for action in actions:
        if action["do"] == "say":
            out.append(action["text"])
        elif action["do"] == "entry":
            out.append(run_book(book, entry_argv(action)))
        else:
            out.append(run_book(book, action["argv"]))
    text = format_reply("\n".join(part for part in out if part))
    log({"body": body, "result": "handled", "actions": len(actions), "reply": text})
    sys.stdout.write(text)
    sys.exit(EXIT_OK)


main()
