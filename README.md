# openclaw-bookkeeper

`skill-bookkeeping` 的 chat-interceptor 版本。同一本帳、同一個引擎，但把
固定格式的輸入從 agent turn 搬到 script，**絕大多數記帳不再呼叫模型**。

專案叫 bookkeeper，裡面的 skill 仍叫 `bookkeeping`——bookkeeper 是人，
bookkeeping 才是技能。

> AI model used: Claude Opus 5.0（本次改寫）；帳本引擎原始版本 GPT-5.0

---

## 它怎麼運作

```
telegram 訊息
   ↓
intercept.py            ← chat-interceptor script
   ├─ 認得的固定格式 → 直接呼叫 book.py → exit 0    0 token
   └─ 認不得        → exit 20                     照原本流程給 AI
                                                    ↓
                                              AI 讀 SKILL.md
                                              一樣呼叫 book.py
```

兩條路徑共用 `book.py` 這一個引擎——`intercept.py` 是 import 它並呼叫
`main()`，不是另寫一套，所以輸出逐字相同，不可能漂移。

### 三層資料，各住各的

| | 例子 | 住哪 |
|---|---|---|
| 語法規則 | `{item}{number}`、`後/完/剩`、贅詞白名單、入金關鍵字 | **程式**（刻意寫死） |
| 學來的資料 | 科目清單、品項→科目、品項是不是收入 | **`book.db`** |
| 部署設定 | channel／target／command／env | **`openclaw.json`** |

入金關鍵字與 16 個預設科目是**固化**的，不是 config。這是自用工具，
改一行陣列重啟 gateway 就好，不必為了避免「改版」去蓋設定管線。

### item_map：省 token 的主力

`book.py` 每次寫入都會把「品項 → 科目／收入」記進 `item_map`，
不管這次是 script 認出來的、使用者明講的，還是 AI 判的。下次同一個品項
就直接命中，**完全不動模型**。

- 現有帳本升級時會自動從歷史種入（實測 78 筆帳 → 39 個品項）
- 使用者用 `358 居住費` 更正科目時，快取跟著改，並回報一行
  `往後「毛巾」預設記為 居住費（原為 雜費）`
- 後寫的贏，但**不追溯**既有帳目——否則已對過帳的月份報表會變動
- 科目被刪除或更名後的孤兒條目，查詢時自動清掉

### 沒學過的品項怎麼辦

`intercept.py` 會問一次模型，走 **`openclaw infer model run`**：

```
openclaw infer model run --gateway --json --prompt "..."
```

**不指定 `--model`**，吃的就是設定裡 `agents.defaults.model` 的
`primary` + `fallbacks`，primary 掛掉會自動換備援，跟 agent turn 享受
同一套 routing。`--gateway` 是必要的——本機模式要自行初始化 provider，
實測 30 秒，走已經開著的 gateway 只要 9 秒。

一則訊息裡多個未知品項會**合併成一次呼叫**。模型有任何一個判不出來，
整則 exit 20 交給 AI——寧可多花 token，也不要記錯科目。
`OPENCLAW_MJS` 沒設就直接 exit 20，功能自動降級不會壞。

**呼叫失敗會再問一次。** 上線第一天四筆漏接裡有兩筆是這裡一次不成就整則
放棄，其中一筆還是「四行只有一個新品項」被單一未知品項拖垮，AI 接手後把
科目記錯。重問一次比叫醒 AI 便宜得多，而且這條路還沒有副作用可言。

重試的時間預算是**從整支 script 開機算起的 48 秒**（route 的 `timeoutMs`
是 60 秒，到點直接 kill，那時連 exit 20 都來不及）。所以快速失敗一定重試
得到，慢到 40 秒的那次則自然不重試——跟沒有重試時一樣。

---

## 安裝

需要 [`uv`](https://docs.astral.sh/uv/)（`book.py` 與 `intercept.py` 都是
PEP 723 self-contained script，機器上不必先裝 Python）。

```bash
openclaw skills install ./bookkeeping --agent <agent-id> --force
```

然後在 `~/.openclaw/openclaw.json` 的 chat-interceptor plugin 設定加一個 route：

```json
{
  "name": "bookkeeping",
  "channel": "telegram",
  "target": "<你的聊天室 id>",
  "command": ["uv", "run", "<workspace>/skills/bookkeeping/scripts/intercept.py"],
  "env": { "BOOKKEEPING_DEBUG": "1" },
  "timeoutMs": 60000,
  "onError": "handle"
}
```

**只有一個要填的東西：`target`。** 其餘都不必設——`book.py` 在 intercept.py
旁邊、帳本位置固定、`openclaw` 從 PATH 上自己找。

`onError` 必須是 `handle`——這支 script 會寫帳，失敗時交還 AI 會重複入帳。
`timeoutMs` 放寬到 60000，因為遇到沒學過的品項要問一次模型（約 9 秒）。

> `command` 裡的 `uv` 走 PATH。gateway 是排程工作／服務啟動的，拿到的是
> **登錄檔裡持久化的 PATH**，不是你互動式 shell 的 PATH——`uv` 若是剛裝好
> 還沒重新登入，可能不在裡面。真的找不到就改成絕對路徑。

改完設定 `openclaw gateway restart`，再確認：

```bash
openclaw plugins inspect openclaw-chat-interceptor --runtime --json
# 要看到 typedHooks 有 before_agent_reply，diagnostics 是空的
```

### 環境變數

| 變數 | 說明 |
|---|---|
| `BOOKKEEPING_DEBUG` | `1` 時把每一輪寫進 `~/.openclaw/logs/bookkeeping.log` |
| `OPENCLAW_NODE` | 逃生口。預設 `node`，走 PATH |
| `OPENCLAW_MJS` | 逃生口。平常自己找得到；找不到就不分類，未知品項交給 AI |
| `BOOKKEEPING_TEST_DB` | **測試專用**，把帳本與 log 導到暫存目錄。正常使用不要設 |

**正常安裝只會用到第一個。** 設定項只在「使用者真的會去改」時才存在——
其餘全部寫死或自動偵測，因為開成 config 只是多給一個填錯的機會。

log 每行一則訊息：原文、`handled` 還是 `pass`、回了什麼。**`pass` 一定附
`why`**，因為「格式沒認得」（`unparsed`）和「問模型當掉」（`classify:rc=…`、
`classify:timeout`）看起來一模一樣，修法卻完全不同——前者要改 parser，
後者不必動 parser。被 script 攔下來的輸入不會出現在 session 裡，
要回頭檢討「哪些講法沒接到」只有這份 log 查得到。

| 誰的檔案 | 怎麼決定位置 |
|---|---|
| `book.py`（我們的） | 寫死在 `intercept.py` 旁邊——同一個安裝單元，一起被複製 |
| `book.db`（我們的） | 寫死在 `~/.openclaw/workspace/bookkeeping/` |
| `openclaw.mjs`（別人的） | 從 PATH 上的 `openclaw` 回推；找不到就降級，不會壞 |

### 帳本位置是固定的，沒有設定項

`~/.openclaw/workspace/bookkeeping/book.db`，寫死在 `book.py`。
放在 skill 安裝目錄之外，是因為 `openclaw skills install --force`
會刪掉來源沒有的檔案，帳本放進去會被連帶清空。

**刻意不開放設定。** 這支引擎有兩個呼叫端：`intercept.py` 拿得到 route 的
`env`，AI 只拿得到 gateway 的 `env`（plugin 是
`{...process.env, ...route.env}`，route env 只給 spawn 出來的 script）。
任何用 env 指定路徑的旋鈕，只要設在 route 那一側，就會變成
**script 記一本、AI 記另一本，而且完全不會報錯**。少一個旋鈕就少一種分裂方式。

要搬家就搬檔案，或做 symlink。自動測試用 `BOOKKEEPING_TEST_DB` 指到暫存目錄——
那是給測試用的，正常使用不要設。

---

## script 認得哪些格式

使用者在聊天室打「說明」就會看到這份清單（`book.py guide`）。
它同時是 `intercept.py` 的行為契約，兩邊必須一致。

| 格式 | 範例 |
|---|---|
| `{item}{number}` | `冰茶40`、`牙間刷447`、`手機機車架790` |
| `{item}{number} {科目}` | `維他命飲67 外食費`、`菊花茶15 記在 公關費` |
| `{科目}{number}` | `孝親費1000`、`奉獻250`；品項就是科目名，不必問模型 |
| 多行 | 一行一筆，可混用有無科目 |
| `提款{number}` | `提款3000`、`某銀行提款2000` |
| `{item}後/完{number}` | `買菜後3229`、`買菜完1263`、`買菜後剩10250` |
| `{item}{number} {日期}{時間}` | `豆包55 記在 8/5 18:00`、`理髮150 時間幫我記在19:30` |
| 編號更正 | `358 居住費`、`358 359 360 居住費`、`424 名稱 巷口饅頭`、`358 金額 80`、`358 時間 8/9 12:09`、`358 刪除`、`刪除 358` |
| 餘額 | `餘額`、`顯示餘額`、`起始餘額2000` |
| 報表 | `七月分類統計`、`七月分類明細`、`七月流水帳`、`展開七月的外食費` |
| 科目 | `列出所有科目`、`新增科目 寵物費`、`刪除科目 毛孩費`、`刪除全部科目`、`把寵物費更名為毛孩費`；新增與刪除可一次貼多行 |
| 換掉整組科目 | `科目改成` ＋ 之後一行一個；或 `科目改成 餐飲、買菜、交通` |
| 說明 | `說明` |

「換掉整組科目」是唯一**一整則訊息＝一個動作**的格式，其餘都是一行一個動作。
它一定走 `category-replace` 一次做完——拆成先清空再逐筆新增會在中途留下沒有
科目的帳本，而且只要有一科仍被帳目使用就該整組原地拒絕。

### 尾巴掃描

`{item}{number}` 後面的東西會被逐一剝掉：時間、日期、科目名、贅詞白名單
（`時間`、`幫我`、`記在`、`是`、`的`…）。所以下面全部走同一支 parser：

```
理髮150 19:30
理髮150 時間是19:30
理髮150 時間幫我記在19:30
```

白名單外的字剝不掉，核心比對就會失敗，整句 exit 20 交給 AI——**保守失敗**
是刻意的。品項本身不接受空白，這道關卡專門擋「剝過頭」留下的殘骸
（例如剝掉時間後剩 `理髮150 時間幫我記在1`），不讓它變成一筆爛帳。

---

## 測試

```bash
uv run --with pytest python -m pytest tests/ -q
```

`tests/test_book.py` 測帳本引擎，`tests/test_intercept.py` 測 parser、
快取與安全性。測試不連網——分類那條路是用「`OPENCLAW_MJS` 沒設」來驗證
它會正確降級成 exit 20。

最重要的一條是 `test_a_single_bad_line_rolls_back_the_whole_message`：
多行訊息只要有一行看不懂，整則 exit 20 且**一筆都不能寫進去**。
AUTHORING.md 的鐵則是「有副作用的動作一旦做了就絕對不可以 exit 20」，
所以 `intercept.py` 嚴格分成兩段——先把所有行解析完、所有科目決定完，
才進入執行段。

---

## 已知限制

- **被攔截的訊息不會進 session 歷史。** 使用者事後問 AI「我剛剛記了什麼」，
  AI 不知道。`book.db` 是唯一真相。開 `BOOKKEEPING_DEBUG` 才有 log 可回溯。
- **語音訊息一律交給 AI**：`cleanedBody` 拿不到轉錄文字。
- **帶空白的品項交給 AI**：`Costco 牛排 500` 不會被 script 吃下來。
- **沒有 per-session 狀態**：「這三筆毛巾都改成居住費」這種要靠上下文的
  講法仍走 AI。請改用編號：`358 359 360 居住費`。
