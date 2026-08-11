# 較省 tokens 的 bookkeeping skill

將 D:\projects\skill-bookkeeping 轉換為符合 openclaw-chat-interceptor 的版本

請參考 D:\projects\skill-bookkeeping\genesis\FIATLUX.md，此為我建立舊版 skill 時的提示詞

本專案名稱為 openclaw-bookkeeper，但它內部的 skill 名稱仍為 bookkeeping，因為 bookkeeper 是一個人，bookkeeping 才是一個 skill

請參考 D:\projects\openclaw-chat-interceptor\docs，是我設計的 plugin，可以將原本 skill 從 agent turn 的使用方式，轉換成 script 優先，或從 script 中呼叫 agent，或攔截 script 能直接處理掉的輸入，不必呼叫 agent，目的是為了要節省 tokens

我們要把舊版 skill-bookkeeping 套用 openclaw-chat-interceptor 的架構，我認為絕大部分的指令都應該由 script 直接處理。只有偶爾出現的自然語言輸入詞，非 script 固定格式時，才轉給 AI 處理。而 script 當中還有「這個項目應該歸到哪個科目」需要 AI 判斷，所以內部應該還有一個「呼叫 AI 判斷科目」的函數

請分成以下幾個階段執行，每階段由我認可、修改後，再進行下一階段

## 階段一，釐清 script 輸入格式與須呼叫 AI 之處

請讀取 .tap 檔，裡面有舊版 bookkeeping 運行的 telegram 頻道，將來套用 openclaw-chat-interceptor 時也會用到

請利用 openclaw session search 的功能列出從 7/9 建立頻道以來，這個頻道中我曾經下過的記帳輸入詞，例如「自助餐120」、「提款3000」等，將所有曾經出現過的輸入詞整理成 .md 檔，分成兩個章節

第一個章節是「具有固定格式，可由 script 直接處理的輸入詞」，要把「具相同格式」的輸入詞辨識出來，愈多 script 能處理的格式，我們就有愈多機會節省 tokens。幫我整理成這樣：

格式 | 範例                                     | 備註
----|------------------------------------------|------
{item}{number} | 自助餐120、理髮 150            | 會處理中間空格
提款{number} | 提款3000、提款 5000              | 關鍵字「提款」
{date}{time} {item}{number} | 昨天18:00 自助餐120、19:30 理髮150 |
{item}{number} {date}{time} | 理髮150 時間是19:30、理髮150 時間幫我記在19:30、理髮150 19:30 | parser 增加辨識彈性

其中最後一個範例，就是有很多的輸入詞可能可以算成同一種格式，由妳來判斷能不能寫出一個 parser 來處理，像「時間是19:30」、「時間幫我記在19:30」，如果做得到的話，就像我上面那樣在備註欄說明。否則的話則備註「請使用者嚴格配合格式」，將來不處理「時間幫我記在19:30」這種自然語言，請使用者在輸入時配合格式，也可以達到節省 tokens 的效果

第二個章節是其它無法辨識成格式的輸入詞，照原文列出。我會 feedback 給妳我認為裡面還有哪些輸入詞也可以辨識，我們互相確認後，就把 script 可辨識的格式定下來

階段一還要產生一個附錄，妳要回覆我：我認為 script 裡面要呼叫 AI 來幫忙處理的函數，可能只有「將項目轉換為科目」這個功能吧？即「自助餐 --> 外食費」、「理髮 --> 治裝費」。妳在整理「頻道中曾經出現過的輸入詞」的時候，應該會發現有哪些格式須要呼叫 AI 來處理的，就幫我補充在附錄

我自己是覺得「昨天18:00」轉換成日期時間應該還是 script 本身就可以做到而不必呼叫 AI 吧，「19:30」代表今天 19:30 也是不必呼叫 AI 吧？

## 階段二，完成全部功能

依據我們剛剛討論出來的結論，及 D:\projects\openclaw-chat-interceptor\docs 的開發指南，完成此記帳功能

先佈署到 openclaw 供我測試

先不要 commit，等我測試滿意了再 commit
