# genesis／這個 skill 是怎麼長出來的

不是文件備份區，是**工作流程的產物**。每份檔案都是某個階段的產出，
名字就說明它在流程裡的位置：`{角色}-{slug}.md`，同一輪共用同一個 slug。

| 角色 | 是什麼 | 誰寫的 |
|---|---|---|
| `FIATLUX` | 種子。從無到有的那一份規格書，只會有一份 | 本尊 |
| `FIAT-{slug}` | 之後每一輪的交辦：加 feature，或叫我去 debug | 本尊 |
| `SURVEY` | 調查現況的產出，還沒動手改 | AI |
| `ISSUE` | 診斷：哪裡壞了、證據是什麼 | AI |
| `FEEDBACK` | 對上一份的回覆與裁示 | 本尊 |
| `FIXLOG` | 修理：改了什麼、怎麼驗的 | AI |

**`FIAT` 開頭的是輸入（本尊交辦），其餘是輸出（AI 產出）。**
一輪從一份 FIAT 開始，同一輪的檔案共用同一個 slug：

```
FIATLUX.md
  └─ SURVEY-input-formats.md   →  FEEDBACK-input-formats.md    建置期：調查 → 裁示

FIAT-first-hours-live.md
  └─ ISSUE-first-hours-live.md →  FIXLOG-first-hours-live.md   營運期：診斷 → 修理
```

交辦用講的也算數，只是那一輪就沒有 FIAT 檔可回頭查
（`first-hours-live` 這輪就是這樣）。

## 營運期的一輪長什麼樣

`.tap`（不進版控，裡面有頻道 id）記著這條頻道的現狀：讀哪裡、上次讀到哪、
由誰服務、怎麼測、怎麼部署。一輪就是：

0. 本尊交辦：**`FIAT-{slug}.md`**（或直接用講的）
1. `node tools/dump-channel.js --tap .tap --user-only`
   —— `since` 自動生效，只印上次檢討之後的對話
2. 對照 `.tap` 裡 `log:` 指的那份 log，同一時段
3. 產出 **`ISSUE-{slug}.md`**，本尊確認後才動手
4. 修 → 跑 `.tap` 裡 `test:` 那行 → 複製到 `deploy:`
5. 產出 **`FIXLOG-{slug}.md`**，把 `.tap` 的 `since` 推到當下

**兩份證據都要讀。** 被 script 攔下、沒驚動 AI 的訊息完全不會進 session，
只在 log 裡；而「接到了但做錯事」在 log 裡長得跟成功一模一樣，只有讀對話、
看到使用者下一句在抱怨才抓得到。第一輪四筆失敗裡，這兩種各佔一半。
