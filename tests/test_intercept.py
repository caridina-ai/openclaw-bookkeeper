"""intercept.py（chat-interceptor script）與 item_map 快取的測試。

分類要問模型的那條路不在這裡測——測試不連網。作法是把 OPENCLAW_MJS 指到
不存在的路徑，驗證未知品項會 exit 20 交還 AI；已知品項則驗證
完全不碰模型就能處理掉。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOK = ROOT / "bookkeeping" / "scripts" / "book.py"
INTERCEPT = ROOT / "bookkeeping" / "scripts" / "intercept.py"
UV = os.environ.get("UV_BIN", "uv")

EXIT_PASS = 20


class InterceptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "book.db"
        self.log = Path(self.tmp.name) / "bookkeeping.log"
        self.addCleanup(self.tmp.cleanup)

    def env(self, **extra):
        env = dict(os.environ)
        env.update({
            "BOOKKEEPING_TEST_DB": str(self.db),
            "PYTHONUTF8": "1",
        })
        # script 平常會自己在 PATH 上找到 openclaw；測試要離線，所以把
        # OPENCLAW_MJS 指到不存在的路徑，明確關掉分類。未知品項就走 exit 20。
        env["OPENCLAW_MJS"] = str(Path(self.tmp.name) / "no-such-openclaw.mjs")
        env.update(extra)
        return env

    def say(self, body, **extra):
        """送一則訊息給 script，回傳 (stdout, exit code)。"""
        done = subprocess.run(
            [UV, "run", str(INTERCEPT)],
            input=json.dumps({
                "body": body, "channel": "telegram", "target": "-100123",
                "senderId": "1", "sessionKey": "agent:a:telegram:group:-100123",
                "agentId": "a",
            }),
            capture_output=True, text=True, encoding="utf-8", env=self.env(**extra),
        )
        return done.stdout.strip(), done.returncode

    def book(self, *args):
        done = subprocess.run(
            [UV, "run", str(BOOK), *args],
            capture_output=True, text=True, encoding="utf-8", env=self.env(),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def handled(self, body, **extra):
        """回傳剝掉程式碼區塊的內容；圍籬本身由 test_multiline_replies_are_fenced 驗。"""
        out, code = self.say(body, **extra)
        self.assertEqual(code, 0, f"{body!r} 應該被 script 處理掉，卻 exit {code}")
        if out.startswith("```") and out.endswith("```"):
            return "\n".join(out.splitlines()[1:-1])
        return out

    def declined(self, body, **extra):
        out, code = self.say(body, **extra)
        self.assertEqual(code, EXIT_PASS, f"{body!r} 應該交還 AI，卻 exit {code}（{out}）")

    # ---------------------------------------------------------------- 基本

    def test_fixed_formats_are_handled_without_the_model(self):
        self.assertEqual(self.handled("餘額"), "餘額 0")
        self.assertEqual(self.handled("起始餘額5000"), "#1 餘額校正 0 -> 5000")
        self.assertEqual(self.handled("冰茶40 外食費"), "#2 外食費 (冰茶) 40 餘額 4960")
        # 第二次同一個品項就靠快取，完全不需要科目也不需要模型
        self.assertEqual(self.handled("冰茶35"), "#3 外食費 (冰茶) 35 餘額 4925")

    def test_income_keywords_are_hardcoded_and_match_by_suffix(self):
        self.handled("起始餘額1000")
        self.assertEqual(self.handled("提款3000"), "#2 入金 (提款) 3000 餘額 4000")
        # 字尾命中：銀行名稱要完整保留在品項裡
        self.assertEqual(
            self.handled("某銀行提款2000"), "#3 入金 (某銀行提款) 2000 餘額 6000"
        )

    def test_withdrawal_fee_is_not_income(self):
        # 「提款手續費」不以「提款」結尾，所以不會被誤判成入金；
        # 它是沒學過的品項，於是交還 AI，而不是把餘額加上去。
        self.declined("提款手續費30")

    def test_multiline_records_one_entry_per_line(self):
        self.handled("起始餘額9000")
        self.handled("開水壺1000 居住費")
        out = self.handled("毛巾79 居住費\n毛巾59 居住費\n毛巾98 居住費")
        self.assertEqual(len(out.splitlines()), 3)
        self.assertIn("#3 居住費 (毛巾) 79", out)
        self.assertIn("#5 居住費 (毛巾) 98", out)

    # ---------------------------------------------------------------- 尾巴

    def test_flexible_tail_accepts_several_ways_of_saying_the_same_time(self):
        self.handled("起始餘額9000")
        self.handled("理髮130 治裝費")
        for phrase in (
            "理髮150 19:30",
            "理髮150 時間是19:30",
            "理髮150 時間幫我記在19:30",
        ):
            out = self.handled(phrase)
            self.assertIn("19:30", out, phrase)
            self.assertIn("治裝費 (理髮) 150", out, phrase)

    def test_tail_accepts_dates_and_explicit_category(self):
        self.handled("起始餘額9000")
        self.assertIn(
            "2026-08-05 18:00", self.handled("豆包55 外食費 記在 2026-08-05 18:00")
        )
        self.assertIn("公關費 (菊花茶) 15", self.handled("菊花茶15 記在 公關費"))

    def test_an_item_that_is_itself_a_category_needs_no_model(self):
        """`孝親費1000`：品項就是科目名，沒什麼好判的。

        測試環境的 OPENCLAW_MJS 指向不存在的檔，所以只要還想問模型就會
        exit 20；能 handled 就證明這趟真的省下來了。
        """
        self.handled("起始餘額9000")
        self.assertIn("孝親費 (孝親費) 1000", self.handled("孝親費1000"))
        self.assertIn("2026-08-11 13:32", self.handled("孝親費1000 8/11 13:32"))

    def test_overstripped_leftovers_never_become_a_bogus_entry(self):
        # 剝掉「19:30」後會剩「理髮150 時間幫我記在1」，品項帶空白 → 交還 AI，
        # 絕不能默默記成一筆金額 1 的爛帳
        self.handled("起始餘額9000")
        self.handled("理髮130 治裝費")
        self.declined("理髮150 時間幫我記在119:30")
        self.assertEqual(self.handled("餘額"), "餘額 8870")

    def test_backfill_from_remaining_cash(self):
        self.handled("起始餘額9000")
        self.handled("買菜100 買菜金")
        self.assertEqual(
            self.handled("買菜後8000"), "#3 買菜金 (買菜) 900 餘額 8000"
        )

    # ---------------------------------------------------------------- 編號

    def test_edit_by_id_changes_category_and_teaches_the_cache(self):
        self.handled("起始餘額9000")
        self.handled("毛巾79 雜費")
        out = self.handled("2 居住費")
        self.assertIn("#2 居住費 (毛巾) 79", out)
        self.assertIn("往後「毛巾」預設記為 居住費（原為 雜費）", out)
        # 快取已改，下次同一個品項直接進居住費
        self.assertIn("居住費 (毛巾)", self.handled("毛巾59"))

    def test_edit_by_id_supports_batch_note_amount_and_delete(self):
        self.handled("起始餘額9000")
        self.handled("毛巾79 雜費\n毛巾59 雜費\n毛巾98 雜費")
        out = self.handled("2 3 4 居住費")
        # 三筆各一行，加上第一筆覆寫快取時的那一行告知（後兩筆已經同值，不再重複告知）
        self.assertEqual(len(out.splitlines()), 4)
        for eid in ("#2", "#3", "#4"):
            self.assertIn(f"{eid} 居住費 (毛巾)", out)
        self.assertEqual(out.count("往後「毛巾」預設記為 居住費"), 1)
        self.assertIn("(巷口饅頭)", self.handled("2 名稱 巷口饅頭"))
        self.assertIn("79 -> 80", self.handled("2 金額 80"))
        self.assertIn("刪除 #4", self.handled("4 刪除"))
        self.assertIn("刪除 #3", self.handled("刪除 3"))

    def test_edit_by_id_changes_the_datetime_at_three_precisions(self):
        """`74 時間 8/9 12:09`。

        修好前這句會掉進「改品項名」，把品項改成「時間 8/9 12:09」而時間
        文風不動——沒接到頂多花 token，做錯事是要人工收拾的，所以釘住。
        """
        self.handled("起始餘額9000")
        self.handled("水果127 買菜金 8/9 22:09")
        out = self.handled("2 時間 8/9 12:09")
        self.assertIn("2026-08-09 12:09 #2 買菜金 (水果) 127", out)
        # 只給時間：留著原本的日期
        self.assertIn("2026-08-09 13:30", self.handled("2 時間 13:30"))
        # 只給日期：留著原本的時間
        self.assertIn("2026-08-10 13:30", self.handled("2 時間 8/10"))
        # 品項自始至終沒被動過
        self.assertIn("(水果)", self.book("detail", "--month", "2026-08"))

    def test_unparsable_datetime_is_left_to_the_ai_instead_of_becoming_a_note(self):
        self.handled("起始餘額9000")
        self.handled("水果127 買菜金")
        self.declined("2 時間 下次再說")
        self.assertIn("(水果)", self.book("detail"))

    def test_typo_in_category_is_caught_instead_of_renaming_the_note(self):
        self.handled("起始餘額9000")
        self.handled("毛巾79 雜費")
        out = self.handled("2 居住廢")
        self.assertIn("不是現有科目", out)
        self.assertIn("居住費", out)
        # 什麼都沒改
        self.assertIn("雜費 (毛巾)", self.book("detail"))

    def test_unknown_id_is_left_to_the_ai(self):
        self.handled("起始餘額9000")
        self.declined("9999 居住費")

    # ---------------------------------------------------------------- 報表

    def test_report_names_longest_match_wins(self):
        self.handled("起始餘額9000")
        self.handled("冰茶40 外食費")
        for phrase, marker in (
            ("八月分類統計", "外食費"),
            ("八月分類明細", "#2"),
            ("八月分帳明細", "#2"),
            ("八月依科目明細", "#2"),
            ("八月流水帳", "#2"),
            ("展開八月的外食費", "#2"),
        ):
            self.assertIn(marker, self.handled(phrase), phrase)

    # ---------------------------------------------------------------- 科目

    def test_categories_can_be_added_and_deleted_in_bulk_by_multiline(self):
        self.handled("新增科目 寵物費\n新增科目 貓砂\n新增科目 獸醫費")
        listed = self.handled("列出所有科目").splitlines()
        self.assertEqual(listed[-3:], ["寵物費", "貓砂", "獸醫費"])
        self.handled("刪除科目 貓砂\n刪除科目 獸醫費")
        self.assertNotIn("貓砂", self.handled("列出所有科目"))

    def test_replacing_the_whole_category_list_is_one_atomic_action(self):
        out = self.handled("捨棄原本的科目，改用我的\n餐飲\n買菜\n交通\n醫療")
        self.assertEqual(out, "已替換科目（共 4 個）")
        self.assertEqual(
            self.handled("列出所有科目").splitlines(), ["餐飲", "買菜", "交通", "醫療"]
        )
        # 同行頓號分隔也算同一個格式
        self.handled("科目改成 甲、乙、丙")
        self.assertEqual(self.handled("列出所有科目").splitlines(), ["甲", "乙", "丙"])

    def test_categories_in_use_block_replace_and_clear_without_partial_damage(self):
        self.handled("起始餘額5000")
        self.handled("早餐60 外食費")
        self.assertIn("無法替換科目", self.handled("科目改成 甲、乙"))
        self.assertIn("無法刪除全部科目", self.handled("刪除全部科目"))
        # 原本那 16 科一個都沒動
        self.assertIn("外食費", self.handled("列出所有科目"))
        self.assertEqual(self.handled("餘額"), "餘額 4940")

    def test_replace_header_without_a_list_goes_to_the_ai(self):
        self.declined("科目改成")

    def test_last_answers_by_insertion_order_not_by_entry_date(self):
        """補記舊帳之後問「上一筆」，要的是剛剛那筆，不是日期最新的那筆。"""
        self.handled("起始餘額5000")
        self.handled("毛巾80 居住費")               # 今天
        self.handled("豆包55 外食費 昨天18:00")      # 補記到昨天
        for phrase in ("上一筆", "上一筆內容是什麼", "剛剛那筆", "最後一筆"):
            self.assertIn("(豆包)", self.handled(phrase), phrase)
        recent = self.handled("最近2筆").splitlines()
        self.assertEqual(len(recent), 2)
        self.assertIn("(豆包)", recent[0])
        self.assertIn("(毛巾)", recent[1])
        self.assertEqual(len(self.handled("最後三筆").splitlines()), 3)
        # 帶其他內容的句子不是這個指令
        self.declined("上一筆 買菜金 是 550")

    def test_balance_query_needs_a_bare_phrase(self):
        self.handled("起始餘額1263")
        self.assertEqual(self.handled("餘額"), "餘額 1263")
        self.assertEqual(self.handled("顯示餘額"), "餘額 1263")
        # 帶數字的長句不是查餘額，交給 AI
        self.declined("剛剛餘額 1263，扣完 60 怎麼會是 1023")

    def test_multiline_replies_are_fenced_but_single_lines_are_not(self):
        """送出的文字會過 CommonMark：`=======` 會被當成 setext 底線整行吃掉，
        而且聊天室是比例字型。多行輸出一律包 fence，單行不包。"""
        self.handled("起始餘額5000")
        single, _ = self.say("餘額")
        self.assertEqual(single, "餘額 5000")

        report, _ = self.say("八月分類統計")
        self.assertTrue(report.startswith("```\n"), report)
        self.assertTrue(report.endswith("\n```"), report)
        self.assertIn("=======", report)  # 分隔線活著

        guide, _ = self.say("說明")
        self.assertTrue(guide.startswith("```\n"))
        self.assertIn("====", guide)

    # ---------------------------------------------------------------- 安全

    def test_a_single_bad_line_rolls_back_the_whole_message(self):
        """有副作用的動作一旦做了就不可以 exit 20（AUTHORING.md 鐵則）。"""
        self.handled("起始餘額5000")
        self.handled("冰茶40 外食費")
        before = self.handled("餘額")
        self.declined("冰茶40\n這句話我看不懂")
        self.assertEqual(self.handled("餘額"), before)

    def test_natural_language_is_declined_without_touching_the_ledger(self):
        self.handled("起始餘額5000")
        for phrase in (
            "剛剛那筆冰茶記錯了",
            "上一筆 買菜金 是 550",
            "妳把所有科目列給我選",
            "",
            "   ",
        ):
            self.declined(phrase)
        self.assertEqual(self.handled("餘額"), "餘額 5000")

    def test_debug_log_is_off_by_default_and_records_both_outcomes_when_on(self):
        self.handled("起始餘額5000")
        self.assertFalse(self.log.exists())
        extra = {"BOOKKEEPING_DEBUG": "1"}  # log 會自動落在測試帳本旁邊
        self.handled("餘額", **extra)
        self.declined("這句話我看不懂", **extra)
        rows = [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["result"] for r in rows], ["handled", "pass"])
        # 交還 AI 一定要寫原因：光看 "pass" 分不出「格式沒認得」和「模型當掉」，
        # 而這兩件事的修法完全不同。
        self.assertEqual(rows[1]["why"], "unparsed")

    def test_declining_because_the_model_is_unreachable_says_so_in_the_log(self):
        extra = {"BOOKKEEPING_DEBUG": "1"}
        self.handled("起始餘額5000", **extra)
        self.declined("沒看過的品項123", **extra)   # OPENCLAW_MJS 指向不存在的檔
        rows = [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[-1]["why"], "classify:no-openclaw")

    def test_missing_book_py_is_an_error_not_a_decline(self):
        """引擎不見了要當成錯誤（非 0），不能 exit 20 讓 AI 以為只是不認得。"""
        orphan = Path(self.tmp.name) / "orphan"
        orphan.mkdir()
        copy = orphan / "intercept.py"          # 故意不把 book.py 一起複製
        copy.write_bytes(INTERCEPT.read_bytes())
        done = subprocess.run(
            [UV, "run", str(copy)], input='{"body":"餘額"}',
            capture_output=True, text=True, encoding="utf-8", env=self.env(),
        )
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.strip(), "")
        self.assertIn("book.py not found", done.stderr)


if __name__ == "__main__":
    unittest.main()
