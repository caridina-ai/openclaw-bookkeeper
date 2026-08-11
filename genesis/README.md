# genesis／這個 skill 是怎麼長出來的

不是文件備份區，是**工作流程的產物**。這一頁是索引：照順序列出每一輪，
一行一句話，細節點進去看。

---

## 歷程

**1. [FIATLUX](FIATLUX.md)** — 把舊版 `skill-bookkeeping` 改寫成
openclaw-chat-interceptor 版本：能寫死格式的都由 script 直接處理，
只有自然語言和「這筆算哪個科目」才叫醒 AI。分兩階段，每階段先過目再往下。

- [SURVEY-input-formats](SURVEY-input-formats.md) —
  從頻道撈出開台以來所有記帳輸入詞，分成「有固定格式、script 接得住」
  與「接不住」兩章，附錄回答「script 裡到底哪些地方非叫 AI 不可」。
- [FEEDBACK-input-formats](FEEDBACK-input-formats.md) —
  本尊裁示：更正一律走「以 #編號指定」、學習型快取要想好「同一品項前後
  矛盾」怎麼辦、關鍵字寫死在程式裡會不會太糟。最後一題的結論寫在
  SURVEY 的〈階段一定案〉§0：**這是自用工具不是產品，一律寫死**，
  設計重點放在「弄錯時看得出來」而不是「不可能弄錯」。

**2. 上線頭三小時的微修正** — 交辦是用講的，沒有 FIAT 檔。
正式版 commit 後幾分鐘內就看得出有輸入詞沒被接住、甚至被處理錯。

- [ISSUE-first-hours-live](ISSUE-first-hours-live.md) —
  四筆失敗：兩筆「接到了但做錯事」（`{編號} 時間 …` 掉進改品項名），
  兩筆是問模型判科目時暫時性失敗、整則被推給 AI。
- [FIXLOG-first-hours-live](FIXLOG-first-hours-live.md) —
  `edit --when`、分類呼叫失敗重試（48 秒預算）、log 的 `pass` 一律附 `why`。

---

## 命名

檔名說明它在流程裡的位置：`{角色}-{slug}.md`，同一輪共用同一個 slug。

| 角色 | 是什麼 | 誰寫的 |
|---|---|---|
| `FIATLUX` | 種子。從無到有的那一份規格書，只會有一份 | 本尊 |
| `FIAT-{slug}` | 之後每一輪的交辦：加 feature，或叫我去 debug | 本尊 |
| `SURVEY` | 調查現況的產出，還沒動手改 | AI |
| `ISSUE` | 診斷：哪裡壞了、證據是什麼 | AI |
| `FEEDBACK` | 對上一份的回覆與裁示 | 本尊 |
| `FIXLOG` | 修理：改了什麼、怎麼驗的 | AI |

**`FIAT` 開頭的是輸入（本尊交辦），其餘是輸出（AI 產出）。**
一輪從一份 FIAT 開始：

```
FIATLUX.md
  └─ SURVEY-input-formats.md   →  FEEDBACK-input-formats.md    建置期：調查 → 裁示

FIAT-{slug}.md
  └─ ISSUE-{slug}.md           →  FIXLOG-{slug}.md             營運期：診斷 → 修理
```

交辦用講的也算數，只是那一輪就沒有 FIAT 檔可回頭查。

`.tap` 一度改名叫 `.ops`，想把 repo／test／deploy 也收進去。後來 `/tap`
skill 成形，那些欄位它一個也不讀，名字就轉回 `.tap`、內容縮回三行。
歷史文件維持原文不動，那是當時的紀錄。

---

## `.tap`／頻道在哪

檢討要從哪個房間撈對話，全部就這三行，`/tap` skill 讀的也是它：

```
channel: telegram
target:  -100XXXXXXXXXX
viewed:  2026-08-11 15:58
```

`viewed` 是**接力棒不是紀念碑**——每輪改完程式才把它推到當下，
下一輪就只撈新的。留空＝看全部。

> **`.tap` 絕不進版控。** 裡面是頻道 id；位置洩漏出去，有心的人就能跑來
> 那個房間騙機器人做事。`.gitignore` 用不帶路徑的 `.tap` 擋，
> 所以放在哪一層都擋得掉。

---

## 營運期的一輪長什麼樣

0. 本尊交辦：**`FIAT-{slug}.md`**（或直接用講的）
1. `/tap` —— `viewed` 自動生效，只給上次檢討之後的：使用者當場打的
   ISSUE、被放行給 AI 的輸入、出錯的、以及標好下場的逐字稿
2. 對照 `~/.openclaw/logs/bookkeeping.log` 同一時段的 `why`
3. 產出 **`ISSUE-{slug}.md`**，本尊確認後才動手
4. 修 → `uv run --with pytest python -m pytest tests/ -q` → 複製到部署位置
5. 產出 **`FIXLOG-{slug}.md`**，把 `.tap` 的 `viewed` 推到當下，
   回頭在本頁「歷程」補一段

**兩份證據都要讀。** 被 script 攔下、沒驚動 AI 的訊息完全不會進 session，
只在 log 裡；而「接到了但做錯事」在 log 裡長得跟成功一模一樣，只有讀對話、
看到使用者下一句在抱怨才抓得到。第一輪四筆失敗裡，這兩種各佔一半。
