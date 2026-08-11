#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
#
# 記帳 skill 核心程式（帳本引擎）
# 本次功能與弱模型路由改善：GPT-5.0
# 科目小計與「說明」自然語言對照：Claude Opus 5.0
# 帳目 ID、item_map 學習型快取、chat-interceptor 化：Claude Opus 5.0
#
# 設計原則：每個子指令都直接印出「可原樣顯示」的結果，
# AI 模型只需判斷該呼叫哪個子指令、填哪些參數，不必再自行組字。
#
# 本檔同時是兩條路徑的唯一引擎：
#   1. intercept.py（chat-interceptor script）→ import 本檔並呼叫 main()
#   2. AI（讀 SKILL.md）→ uv run book.py <子指令>
# 兩條路徑走同一份程式碼，輸出必然一致。
#
# 上面的 PEP 723 區塊讓本檔成為 self-contained script：只用標準庫，
# 所以 dependencies 是空的，`uv run book.py` 會自備直譯器在隔離環境執行，
# 機器上不需要另外安裝 Python。要加套件時就寫進 dependencies，別另開
# requirements 檔——安裝單元只有 SKILL.md 與 scripts/，其他檔案不會被裝進去。

import argparse
import csv
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

# Windows 主控台預設 cp950，強制 UTF-8 才能正確輸出中文
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 全新帳本開箱即用的預設科目（順序也是初始報表順序）
DEFAULT_CATEGORIES = [
    "外食費", "買菜金", "民俗節日", "居住費", "管理費", "交通費",
    "車稅險", "保健費", "治裝費", "精進金", "旅遊金", "公關費",
    "孝親費", "奉獻", "歸墊", "雜費",
]

# 帳本位置是**固定的**，沒有給使用者設定的旋鈕。
#
# 放在 skill 安裝目錄「之外」：安裝／升級是整個安裝目錄換掉
# （`openclaw skills install --force` 會刪掉來源目錄沒有的東西），
# 帳本放在裡面會被連帶清空。
#
# 之所以不開放設定：這支程式有兩個呼叫端——intercept.py（拿得到 route 的 env）
# 與 AI（只拿得到 gateway 的 env）。任何用 env 指定路徑的做法，只要設在
# route 那一側，就會變成 script 記一本、AI 記另一本，而且完全不會報錯。
# 少一個旋鈕就少一種帳本分裂的方式。要搬家就搬檔案（或做 symlink）。
#
# 唯一的例外是自動測試：它必須寫在暫存目錄，絕不能碰到真的帳本。
DB_PATH = Path(os.environ.get(
    "BOOKKEEPING_TEST_DB",
    Path.home() / ".openclaw" / "workspace" / "bookkeeping" / "book.db",
))

SEP = "-------"  # 合計前的分隔線

# 「錢變多」的固化關鍵字。這是刻意寫死的：它是規則不是資料，
# 而且 intercept.py 靠它在完全不查 DB 的情況下就認出入金。
# 比對方式是「品項以此結尾」，所以「某銀行提款」命中、「提款手續費」不命中
# （後者不以「提款」結尾），舊版的手續費特例規則因此可以刪掉。
INCOME_WORDS = [
    "提款", "提領", "領錢", "領款", "入金", "匯款", "薪水", "薪資",
    "酬勞", "獎金", "退款", "退稅", "中獎", "撿到", "紅包",
]

# 全形數字轉半形：手機輸入法打出來的「提款３０００」要能命中
FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# 分類提示。intercept.py 用它組分類 prompt，SKILL.md 有一份給 AI 讀的同表。
# **兩邊改一邊就要改另一邊。** 只有目前仍存在的科目才會被用到。
CATEGORY_HINTS = {
    "外食費": "早餐、午餐、晚餐、便當、飲料、冰茶、咖啡、餐廳、外送",
    "買菜金": "買菜、蔬菜、水果、全聯、超市、菜市場、食材、雞蛋、肉",
    "民俗節日": "紅包、拜拜、金紙、粽子、月餅、年貨",
    "居住費": "房租、水費、電費、瓦斯費、網路、電話、修繕、家用品、毛巾、鍋碗",
    "管理費": "社區管理費、大樓管理費",
    "交通費": "捷運、公車、火車、高鐵、計程車、加油、停車、悠遊卡加值、機車配件",
    "車稅險": "牌照稅、燃料稅、汽機車保險、驗車、公路使用養護管理費",
    "保健費": "藥、看診、掛號、牙科、醫院、診所、維他命、健檢、牙刷、牙間刷",
    "治裝費": "衣服、褲子、鞋子、包包、飾品、理髮",
    "精進金": "書、課程、學費、訂閱、軟體、線上課、文具、電腦周邊",
    "旅遊金": "旅遊、住宿、飯店、門票、機票、伴手禮",
    "公關費": "送禮、請客、聚餐分攤、禮金、伴手禮送人",
    "孝親費": "給父母、孝親",
    "奉獻": "捐款、香油錢、奉獻、樂捐",
    "歸墊": "代墊、還錢、歸還、墊付",
    "雜費": "使用者明說雜費，或確定不屬於其他現有科目的雜項",
}


def normalize_note_key(note):
    """item_map 的鍵。正規化後**精確相等**比對，不做模糊或子字串。"""
    return " ".join(str(note).translate(FULLWIDTH_DIGITS).split())


def income_word_hit(note):
    """品項是否以某個固化入金詞結尾；回傳命中的詞（最長者）或 None。"""
    key = normalize_note_key(note)
    hits = [w for w in INCOME_WORDS if key.endswith(w)]
    return max(hits, key=len) if hits else None


def lookup_item(con, note):
    """查 item_map。回傳 (kind, category) 或 None。

    順序：固化入金詞 → 精確命中 → kind='in' 的字尾命中。
    科目那側不做任何模糊比對，查不到就是查不到（呼叫端 exit 20 交給 AI）。
    """
    if income_word_hit(note):
        return ("in", None)
    key = normalize_note_key(note)
    if not key:
        return None
    row = con.execute(
        "SELECT kind, category FROM item_map WHERE note_key=?", (key,)
    ).fetchone()
    if row:
        kind, category = row[0], row[1]
        if kind == "out":
            # 科目可能已被刪除或更名，孤兒條目當作 miss 並清掉
            if not category or category not in categories(con):
                con.execute("DELETE FROM item_map WHERE note_key=?", (key,))
                con.commit()
                return None
        return (kind, category)
    suffixes = [
        r[0] for r in con.execute(
            "SELECT note_key FROM item_map WHERE kind='in' AND length(note_key)>=2"
        ).fetchall() if key.endswith(r[0]) and key != r[0]
    ]
    return ("in", None) if suffixes else None


def record_item(con, note, kind, category):
    """把這次的決定記進 item_map。回傳被覆寫掉的舊科目名（沒有就是 None）。

    呼叫端必須已經在交易中。固化入金詞不入表——它們是規則，不是學來的。
    """
    key = normalize_note_key(note)
    if not key or income_word_hit(key):
        return None
    row = con.execute(
        "SELECT kind, category FROM item_map WHERE note_key=?", (key,)
    ).fetchone()
    previous = row[1] if row and row[0] == "out" else None
    con.execute(
        "INSERT INTO item_map(note_key,kind,category,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(note_key) DO UPDATE SET kind=excluded.kind, "
        "category=excluded.category, updated_at=excluded.updated_at",
        (key, kind, category, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return previous if previous and previous != category else None


def learned_line(note, category, previous):
    """覆寫既有快取時多印的那一行；沒覆寫就回空字串。"""
    if not previous:
        return ""
    return f"\n往後「{normalize_note_key(note)}」預設記為 {category}（原為 {previous}）"


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    had_categories = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='categories'"
    ).fetchone() is not None
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS entries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dt TEXT NOT NULL,        -- 'YYYY-MM-DD HH:MM:SS'
                kind TEXT NOT NULL,      -- in=入金 out=支出 adj=校正
                category TEXT,           -- out 用科目名；其餘為 NULL
                note TEXT NOT NULL,      -- 品項/說明
                amount INTEGER NOT NULL) -- out/in 為正；adj 可為負"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS categories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                position INTEGER NOT NULL)"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS bookkeeping_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL)"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS item_map(
                note_key TEXT PRIMARY KEY,   -- normalize_note_key(品項)
                kind TEXT NOT NULL,          -- in=入金 out=支出
                category TEXT,               -- out 的科目；in 為 NULL
                updated_at TEXT NOT NULL)"""
        )

        initialized = con.execute(
            "SELECT 1 FROM bookkeeping_meta WHERE key='categories_initialized'"
        ).fetchone() is not None
        category_count = int(con.execute(
            "SELECT COUNT(*) FROM categories"
        ).fetchone()[0])
        if not initialized:
            # 全新 DB 種入原本 16 科。舊版若已有空的 categories 且沒有
            # 支出，視為使用者刻意清空；若仍有支出則視為中斷遷移並修復。
            has_out_entries = con.execute(
                "SELECT 1 FROM entries WHERE kind='out' LIMIT 1"
            ).fetchone() is not None
            if not had_categories or (category_count == 0 and has_out_entries):
                con.executemany(
                    "INSERT OR IGNORE INTO categories(name,position) VALUES(?,?)",
                    [(name, index) for index, name in enumerate(DEFAULT_CATEGORIES)],
                )
            con.execute(
                "INSERT INTO bookkeeping_meta(key,value) VALUES('categories_initialized','1')"
            )

        # 修復舊版或中斷遷移留下的孤兒科目，絕不遺失既有支出。
        next_position = int(con.execute(
            "SELECT COALESCE(MAX(position),-1)+1 FROM categories"
        ).fetchone()[0])
        orphan_names = [row[0] for row in con.execute(
            "SELECT DISTINCT e.category FROM entries e "
            "LEFT JOIN categories c ON c.name=e.category "
            "WHERE e.kind='out' AND e.category IS NOT NULL AND c.name IS NULL "
            "ORDER BY e.id"
        ).fetchall()]
        con.executemany(
            "INSERT INTO categories(name,position) VALUES(?,?)",
            [(name, next_position + index) for index, name in enumerate(orphan_names)],
        )

        # 既有帳本一次性種入 item_map：每個品項取「最後一次」用過的科目。
        # 這讓升級後的第一天就有命中率，不必從零學起。只跑一次。
        seeded = con.execute(
            "SELECT 1 FROM bookkeeping_meta WHERE key='item_map_seeded'"
        ).fetchone() is not None
        if not seeded:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            history = con.execute(
                "SELECT note, category FROM entries e WHERE kind='out' "
                "AND category IS NOT NULL AND id=("
                "  SELECT MAX(id) FROM entries x WHERE x.kind='out' AND x.note=e.note)"
            ).fetchall()
            con.executemany(
                "INSERT OR IGNORE INTO item_map(note_key,kind,category,updated_at) "
                "VALUES(?,'out',?,?)",
                [
                    (normalize_note_key(note), category, now)
                    for note, category in history
                    if normalize_note_key(note) and not income_word_hit(note)
                ],
            )
            con.execute(
                "INSERT INTO bookkeeping_meta(key,value) VALUES('item_map_seeded','1')"
            )

        con.execute(
            """CREATE TRIGGER IF NOT EXISTS entries_require_category_insert
            BEFORE INSERT ON entries
            WHEN NEW.kind='out' AND (
                NEW.category IS NULL OR
                NOT EXISTS(SELECT 1 FROM categories WHERE name=NEW.category)
            )
            BEGIN
                SELECT RAISE(ABORT, '支出科目不存在');
            END"""
        )
        con.execute(
            """CREATE TRIGGER IF NOT EXISTS entries_require_category_update
            BEFORE UPDATE OF kind,category ON entries
            WHEN NEW.kind='out' AND (
                NEW.category IS NULL OR
                NOT EXISTS(SELECT 1 FROM categories WHERE name=NEW.category)
            )
            BEGIN
                SELECT RAISE(ABORT, '支出科目不存在');
            END"""
        )
        con.execute(
            """CREATE TRIGGER IF NOT EXISTS categories_restrict_delete
            BEFORE DELETE ON categories
            WHEN EXISTS(
                SELECT 1 FROM entries WHERE kind='out' AND category=OLD.name
            )
            BEGIN
                SELECT RAISE(ABORT, '科目仍有帳目使用');
            END"""
        )
        con.execute(
            """CREATE TRIGGER IF NOT EXISTS categories_restrict_name_update
            BEFORE UPDATE OF name ON categories
            WHEN NEW.name<>OLD.name AND EXISTS(
                SELECT 1 FROM entries WHERE kind='out' AND category=OLD.name
            )
            BEGIN
                SELECT RAISE(ABORT, '請同步更名帳目');
            END"""
        )
        con.commit()
    except Exception:
        con.rollback()
        con.close()
        raise
    return con


@contextmanager
def write_transaction(con):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
        con.commit()
    except BaseException:
        con.rollback()
        raise


@contextmanager
def read_transaction(con):
    """讓一個多查詢報表全程看到同一份 SQLite 快照。"""
    con.execute("BEGIN")
    try:
        yield
    finally:
        con.rollback()


def categories(con):
    return [row[0] for row in con.execute(
        "SELECT name FROM categories ORDER BY position,id"
    ).fetchall()]


def clean_category_name(value):
    name = str(value).strip()
    if not name:
        print("錯誤：科目名稱不可為空")
        sys.exit(0)
    return name


def balance(con):
    # out 扣錢，其餘（in、adj）加錢
    row = con.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind='out' THEN -amount ELSE amount END),0) FROM entries"
    ).fetchone()
    return int(row[0])


def today():
    return datetime.now().strftime("%Y-%m-%d")


def normalize_date(value):
    raw = str(value).strip()
    for fmt, output in (
        ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M"),
        ("%Y-%m-%d", "%Y-%m-%d"),
    ):
        try:
            return datetime.strptime(raw, fmt).strftime(output)
        except ValueError:
            pass
    print(f"錯誤：日期時間必須是 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（收到「{value}」）")
    sys.exit(0)


def build_dt(date):
    # 指定日期可精確到 HH:MM；只有日期時補 00:00。
    if date:
        normalized = normalize_date(date)
        return normalized + (":00" if len(normalized) == 16 else " 00:00:00")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def input_prefix(dt, date):
    # 沒指定日期時間時維持簡潔；有指定就照精度顯示。
    if not date:
        return ""
    normalized = normalize_date(date)
    return (dt[:16] if len(normalized) == 16 else dt[:10]) + " "


def matched_prefix(dt, date, force_time=False):
    # 修改／刪除只有在使用者指定日期時顯示日期；保留原紀錄的 HH:MM。
    if not date and not force_time:
        return ""
    explicit_time = bool(date) and len(str(date).strip()) == 16
    return (
        dt[:16] if force_time or explicit_time or dt[11:16] != "00:00" else dt[:10]
    ) + " "


def entry_prefix(dt):
    """用編號操作時的日期前綴：今天的帳不顯示，改到舊帳才顯示是哪一天。"""
    if dt[:10] == today():
        return ""
    return (dt[:16] if dt[11:16] != "00:00" else dt[:10]) + " "


def prefix_for(a, dt):
    """用編號指定時走 entry_prefix；用 --find 搜尋時維持舊行為。"""
    if getattr(a, "rowid", None):
        return entry_prefix(dt)
    return matched_prefix(dt, a.date, False)


def label_of(kind, category):
    if kind == "in":
        return "入金"
    if kind == "adj":
        return "校正"
    return category


def parse_int(s, name):
    try:
        return int(str(s).replace(",", "").strip())
    except ValueError:
        print(f"錯誤：{name}必須是整數（收到「{s}」）")
        sys.exit(0)


def parse_positive(s, name):
    value = parse_int(s, name)
    if value <= 0:
        print(f"錯誤：{name}必須是正整數（收到「{s}」）")
        sys.exit(0)
    return value


def month_range(month):
    # '2026-07' -> ('2026-07-01', '2026-08-01')；字串比較即可涵蓋整月
    try:
        normalized = datetime.strptime(str(month).strip(), "%Y-%m").strftime("%Y-%m")
    except ValueError:
        print(f"錯誤：月份必須是 YYYY-MM（收到「{month}」）")
        sys.exit(0)
    y, m = (int(x) for x in normalized.split("-"))
    start = f"{y:04d}-{m:02d}-01"
    end = f"{y+1:04d}-01-01" if m == 12 else f"{y:04d}-{m+1:02d}-01"
    return start, end


def selected_months(values):
    raw = values or [datetime.now().strftime("%Y-%m")]
    result = []
    for value in raw:
        start, _ = month_range(value)
        month = start[:7]
        if month not in result:
            result.append(month)
    return result


def selected_periods(months, dates, label):
    if months and dates:
        print(f"錯誤：{label}查詢請選日期或月份，不要混用")
        return None
    if dates:
        result = []
        seen = set()
        for raw_date in dates:
            date = normalize_date(raw_date)
            if len(date) != 10:
                print(f"錯誤：{label}的日期必須是 YYYY-MM-DD")
                return None
            if date in seen:
                continue
            seen.add(date)
            end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime(
                "%Y-%m-%d"
            )
            result.append((date, date, end))
        return result
    return [
        (month, *month_range(month)) for month in selected_months(months)
    ]


def dwidth(s):
    # 顯示寬度：中日文字元算 2 欄，其餘算 1 欄
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(s))


def rpad(s, width):
    return str(s) + " " * max(0, width - dwidth(s))


def clean_find_terms(values):
    terms = []
    for value in values:
        term = str(value).strip()
        if not term:
            print("錯誤：查找文字不可為空")
            sys.exit(0)
        if term not in terms:
            terms.append(term)
    return terms


def find_entries(con, finds, date, amount):
    terms = clean_find_terms(finds)
    alternatives = " OR ".join("instr(note, ?) > 0" for _ in terms)
    q = (
        "SELECT id,dt,kind,category,note,amount FROM entries "
        f"WHERE ({alternatives})"
    )
    params = list(terms)
    if date:
        normalized = normalize_date(date)
        length = 16 if len(normalized) == 16 else 10
        q += f" AND substr(dt,1,{length})=?"
        params.append(normalized)
    if amount is not None:
        q += " AND amount=?"
        params.append(parse_positive(amount, "金額"))
    q += " ORDER BY dt DESC, id DESC"
    return con.execute(q, params).fetchall()


def rowid_entries(con, rowids):
    """依使用者給的順序取回帳目；任何一個不存在就整批不動作。"""
    ids = [parse_positive(value, "帳目編號") for value in rowids]
    seen = set()
    ordered = [i for i in ids if not (i in seen or seen.add(i))]
    found = {
        row[0]: row for row in con.execute(
            "SELECT id,dt,kind,category,note,amount FROM entries WHERE id IN ("
            + ",".join("?" for _ in ordered) + ")", ordered
        ).fetchall()
    }
    missing = [i for i in ordered if i not in found]
    if missing:
        joined = "、".join(f"#{i}" for i in missing)
        print(f"找不到 {joined} 的紀錄，未變更任何帳目")
        return None
    return [found[i] for i in ordered]


def selected_rows(con, a, action):
    has_finds = bool(a.find)
    has_rowid = bool(a.rowid)
    if has_finds == has_rowid:
        print("錯誤：--find（可重複）與 --rowid（可重複）請擇一提供")
        return None
    if has_rowid:
        return rowid_entries(con, a.rowid)
    rows = find_entries(con, a.find, a.date, getattr(a, "amount", None))
    terms = "、".join(clean_find_terms(a.find))
    if not rows:
        print(f"找不到符合「{terms}」的紀錄，請確認")
        return None
    if len(rows) > 1:
        verb = "修改" if action == "edit" else "刪除"
        out = [f"找到 {len(rows)} 筆符合「{terms}」的紀錄，未{verb}任何帳目："]
        for eid, dt, kind, category, note, amount in rows:
            out.append(
                f"#{eid} | {dt[:16]} | {label_of(kind, category)} | {note} | {int(amount)}"
            )
        out.append(f"請依上述內容選定編號，再執行{verb}。")
        print("\n".join(out))
        return None
    return rows


# ---- 記帳 ----

def cmd_in(con, a):
    amount = parse_positive(a.amount, "金額")
    dt = build_dt(a.date)
    with write_transaction(con):
        cur = con.execute(
            "INSERT INTO entries(dt,kind,category,note,amount) VALUES(?,?,?,?,?)",
            (dt, "in", None, a.note, amount))
        eid = cur.lastrowid
        # 不管是誰決定的（script 認得，或 AI 判斷後呼叫），都學起來
        record_item(con, a.note, "in", None)
        current = balance(con)
    print(f"{input_prefix(dt, a.date)}#{eid} 入金 ({a.note}) {amount} 餘額 {current}")


def cmd_out(con, a):
    has_amount = a.amount is not None
    has_balance = a.balance is not None
    if has_amount == has_balance:
        print("錯誤：--amount（花費金額）與 --balance（剩餘餘額）請擇一提供")
        return
    if has_balance and a.date:
        print(
            "倒推不支援指定日期或時間。請告訴我這次實際花了多少錢，"
            "或移除日期時間後再用目前餘額倒推。"
        )
        return
    dt = build_dt(a.date)
    with write_transaction(con):
        if a.category not in categories(con):
            print(f"錯誤：沒有「{a.category}」這個科目，請確認科目名稱")
            return
        if has_balance:
            target = parse_int(a.balance, "餘額")
            current = balance(con)
            amount = current - target
            if amount <= 0:
                print(f"錯誤：目前餘額 {current}，剩餘 {target} 無法倒推出花費，請確認")
                return
        else:
            amount = parse_positive(a.amount, "金額")
        cur = con.execute(
            "INSERT INTO entries(dt,kind,category,note,amount) VALUES(?,?,?,?,?)",
            (dt, "out", a.category, a.note, amount))
        eid = cur.lastrowid
        previous = record_item(con, a.note, "out", a.category)
        current = balance(con)
    print(
        f"{input_prefix(dt, a.date)}#{eid} {a.category} ({a.note}) {amount} 餘額 {current}"
        + learned_line(a.note, a.category, previous)
    )


def cmd_edit(con, a):
    fields = [f for f in (a.to, a.category, a.note) if f is not None]
    if len(fields) != 1:
        print("錯誤：--to（金額）、--category（科目）、--note（品項）請擇一提供")
        return
    lines = []
    with write_transaction(con):
        rows = selected_rows(con, a, "edit")
        if not rows:
            return
        if a.category is not None:
            new_category = clean_category_name(a.category)
            if new_category not in categories(con):
                print(f"錯誤：沒有「{new_category}」這個科目，請確認科目名稱")
                return
            wrong = [f"#{r[0]}" for r in rows if r[2] != "out"]
            if wrong:
                print(f"錯誤：{'、'.join(wrong)} 不是支出，沒有科目可改")
                return
            for eid, dt, kind, category, note, amount in rows:
                con.execute(
                    "UPDATE entries SET category=? WHERE id=?", (new_category, eid)
                )
                previous = record_item(con, note, "out", new_category)
                lines.append(
                    f"{prefix_for(a, dt)}#{eid} {new_category} "
                    f"({note}) {int(amount)} 餘額 {balance(con)}"
                    + learned_line(note, new_category, previous)
                )
        elif a.note is not None:
            new_note = str(a.note).strip()
            if not new_note:
                print("錯誤：品項不可為空")
                return
            for eid, dt, kind, category, note, amount in rows:
                con.execute("UPDATE entries SET note=? WHERE id=?", (new_note, eid))
                if kind == "out":
                    record_item(con, new_note, "out", category)
                lines.append(
                    f"{prefix_for(a, dt)}#{eid} {label_of(kind, category)} "
                    f"({new_note}) {int(amount)} 餘額 {balance(con)}"
                )
        else:
            new = parse_int(a.to, "金額")
            for eid, dt, kind, category, note, old in rows:
                if kind in ("in", "out") and new <= 0:
                    print(f"錯誤：金額必須是正整數（收到「{a.to}」）")
                    return
                b0 = balance(con)
                con.execute("UPDATE entries SET amount=? WHERE id=?", (new, eid))
                b1 = balance(con)
                lines.append(
                    f"{prefix_for(a, dt)}#{eid} "
                    f"{label_of(kind, category)} ({note}) {int(old)} -> {new} "
                    f"餘額 {b0} -> {b1}"
                )
    print("\n".join(lines))


def cmd_delete(con, a):
    lines = []
    with write_transaction(con):
        rows = selected_rows(con, a, "delete")
        if not rows:
            return
        for eid, dt, kind, category, note, amount in rows:
            b0 = balance(con)
            con.execute("DELETE FROM entries WHERE id=?", (eid,))
            b1 = balance(con)
            lines.append(
                f"{prefix_for(a, dt)}刪除 #{eid} "
                f"{label_of(kind, category)} ({note}) {int(amount)} 餘額 {b0} -> {b1}"
            )
    print("\n".join(lines))


# ---- 餘額 ----

def cmd_balance(con, a):
    print(f"餘額 {balance(con)}")


def cmd_adjust(con, a):
    # 絕對校正：直接把餘額平移到目標值，不動已記帳的相對金額
    target = parse_int(a.balance, "餘額")
    with write_transaction(con):
        cur = balance(con)
        diff = target - cur
        row = con.execute(
            "INSERT INTO entries(dt,kind,category,note,amount) VALUES(?,?,?,?,?)",
            (build_dt(None), "adj", None, "餘額校正", diff))
        eid = row.lastrowid
    print(f"#{eid} 餘額校正 {cur} -> {target}")


# ---- 報表 ----

def cmd_categories(con, a):
    names = categories(con)
    print("\n".join(names) if names else "目前沒有科目")


def cmd_last(con, a):
    """最近記入的 N 筆，依**記入順序**（id）而不是帳目日期。

    「上一筆」指的是剛剛記的那一筆。補記舊帳時（`豆包55 昨天18:00`）帳目日期
    會比前一筆早，依 dt 排序就會答錯人——那是使用者問這句話時最不想要的答案。
    """
    count = parse_positive(a.count, "筆數") if a.count else 1
    rows = con.execute(
        "SELECT id,dt,kind,category,note,amount FROM entries ORDER BY id DESC LIMIT ?",
        (count,),
    ).fetchall()
    if not rows:
        print("帳本目前沒有任何紀錄")
        return
    print("\n".join(
        f"{dt[:16]} #{eid} {label_of(kind, category)} ({note}) {int(amount)}"
        for eid, dt, kind, category, note, amount in rows
    ))


def cmd_item_map(con, a):
    # 除錯用：看看它到目前為止學到什麼。不列在 guide 裡。
    rows = con.execute(
        "SELECT note_key, kind, category FROM item_map ORDER BY kind, category, note_key"
    ).fetchall()
    if not rows:
        print("目前沒有學到任何品項對應")
        return
    print("\n".join(
        f"{note_key} -> {'入金' if kind == 'in' else category}"
        for note_key, kind, category in rows
    ))


def cmd_category_add(con, a):
    name = clean_category_name(a.name)
    with write_transaction(con):
        if name in categories(con):
            print(f"錯誤：科目「{name}」已存在")
            return
        position = con.execute(
            "SELECT COALESCE(MAX(position),-1)+1 FROM categories"
        ).fetchone()[0]
        con.execute(
            "INSERT INTO categories(name,position) VALUES(?,?)", (name, position)
        )
    print(f"新增科目 {name}")


def cmd_category_delete(con, a):
    name = clean_category_name(a.name)
    with write_transaction(con):
        if name not in categories(con):
            print(f"錯誤：找不到科目「{name}」")
            return
        used = int(con.execute(
            "SELECT COUNT(*) FROM entries WHERE kind='out' AND category=?", (name,)
        ).fetchone()[0])
        if used:
            print(f"無法刪除科目「{name}」：仍有 {used} 筆帳目使用此科目，請先刪除這些帳目")
            return
        con.execute("DELETE FROM categories WHERE name=?", (name,))
    print(f"刪除科目 {name}")


def cmd_category_clear(con, a):
    with write_transaction(con):
        used = int(con.execute(
            "SELECT COUNT(*) FROM entries WHERE kind='out' AND category IS NOT NULL"
        ).fetchone()[0])
        if used:
            print(f"無法刪除全部科目：仍有 {used} 筆帳目使用科目，請先刪除這些帳目")
            return
        con.execute("DELETE FROM categories")
    print("已刪除全部科目")


def cmd_category_replace(con, a):
    names = [clean_category_name(name) for name in a.name]
    duplicate = next((name for name in names if names.count(name) > 1), None)
    if duplicate:
        print(f"錯誤：科目「{duplicate}」重複，未變更任何科目")
        return
    with write_transaction(con):
        used = [row[0] for row in con.execute(
            "SELECT DISTINCT category FROM entries "
            "WHERE kind='out' AND category IS NOT NULL ORDER BY category"
        ).fetchall() if row[0] not in names]
        if used:
            joined = "、".join(used)
            print(f"無法替換科目：帳目仍在使用「{joined}」，請先刪除這些帳目")
            return
        for position, name in enumerate(names):
            con.execute(
                "INSERT OR IGNORE INTO categories(name,position) VALUES(?,?)",
                (name, position),
            )
            con.execute(
                "UPDATE categories SET position=? WHERE name=?", (position, name)
            )
        placeholders = ",".join("?" for _ in names)
        con.execute(
            f"DELETE FROM categories WHERE name NOT IN ({placeholders})", names
        )
    print(f"已替換科目（共 {len(names)} 個）")


def cmd_category_rename(con, a):
    old = clean_category_name(a.old)
    new = clean_category_name(a.new)
    with write_transaction(con):
        names = categories(con)
        if old not in names:
            print(f"錯誤：找不到科目「{old}」")
            return
        if new != old and new in names:
            print(f"錯誤：科目「{new}」已存在")
            return
        if new != old:
            # 先建立新科目，再搬帳、刪舊科目，讓完整性觸發器全程成立。
            position = con.execute(
                "SELECT position FROM categories WHERE name=?", (old,)
            ).fetchone()[0]
            con.execute(
                "INSERT INTO categories(name,position) VALUES(?,?)", (new, position)
            )
            con.execute(
                "UPDATE entries SET category=? WHERE kind='out' AND category=?",
                (new, old),
            )
            # 快取跟著更名，否則舊條目會變孤兒、白白讓 AI 重判一次
            con.execute(
                "UPDATE item_map SET category=? WHERE kind='out' AND category=?",
                (new, old),
            )
            con.execute("DELETE FROM categories WHERE name=?", (old,))
    print(f"科目 {old} -> {new}")


def _report_block(con, title, start, end):
    totals = dict(con.execute(
        "SELECT category, SUM(amount) FROM entries "
        "WHERE kind='out' AND dt>=? AND dt<? GROUP BY category",
        (start, end)).fetchall())
    names = [c for c in categories(con) if totals.get(c)]
    grand = sum(int(totals[n]) for n in names)
    namew = max([dwidth(n) for n in names] + [dwidth("合計")])
    amtw = max([len(str(int(totals[n]))) for n in names] + [len(str(grand))])
    out = [title, "=" * dwidth(title)]
    for n in names:
        out.append(f"{rpad(n, namew)} {str(int(totals[n])).rjust(amtw)}")
    out.append(SEP)
    out.append(f"{rpad('合計', namew)} {str(grand).rjust(amtw)}")
    return "\n".join(out)


def cmd_report(con, a):
    periods = selected_periods(a.month, a.date, "分帳")
    if periods is None:
        return
    with read_transaction(con):
        output = "\n\n".join(
            _report_block(con, title, start, end) for title, start, end in periods
        )
    print(output)


def _cat_lines(con, category, start, end):
    rows = con.execute(
        "SELECT dt, note, amount, id FROM entries "
        "WHERE kind='out' AND category=? AND dt>=? AND dt<? ORDER BY dt, id",
        (category, start, end)).fetchall()
    lines = [f"{dt[:16]} #{eid} {note} {int(amount)}" for dt, note, amount, eid in rows]
    total = sum(int(row[2]) for row in rows)
    return lines, total


def _by_category(con, cats, title, start, end):
    # 依科目分段輸出（category-detail 與 expand 共用）
    out = [title, "=" * dwidth(title)]
    grand = 0
    for index, c in enumerate(cats):
        lines, total = _cat_lines(con, c, start, end)
        grand += total
        if index:
            out.append("")
        # 科目小計直接印在科目行，AI 模型不必（也不該）自己加總明細
        out.append(f"{c} {total}")
        out.extend(lines)
    out.append(SEP)
    out.append(f"合計 {grand}")
    return "\n".join(out)


def cmd_category_detail(con, a):
    periods = selected_periods(a.month, a.date, "分帳明細")
    if periods is None:
        return
    blocks = []
    with read_transaction(con):
        for title, start, end in periods:
            # 只列出該期間有支出的科目
            have = {r[0] for r in con.execute(
                "SELECT DISTINCT category FROM entries WHERE kind='out' AND dt>=? AND dt<?",
                (start, end)).fetchall()}
            blocks.append(_by_category(
                con, [c for c in categories(con) if c in have], title, start, end
            ))
    print("\n\n".join(blocks))


def cmd_expand(con, a):
    periods = selected_periods(a.month, a.date, "展開")
    if periods is None:
        return
    with read_transaction(con):
        names = categories(con)
        for c in a.category:
            if c not in names:
                print(f"錯誤：沒有「{c}」這個科目，請確認科目名稱")
                return
        blocks = [
            _by_category(con, a.category, title, start, end)
            for title, start, end in periods
        ]
    print("\n\n".join(blocks))  # 保留使用者指定的科目與月份順序


def _detail_date(con, raw_date):
    date = normalize_date(raw_date)
    if len(date) != 10:
        print("錯誤：單日明細的日期必須是 YYYY-MM-DD")
        sys.exit(0)
    rows = con.execute(
        "SELECT dt, category, note, amount, id FROM entries "
        "WHERE kind='out' AND substr(dt,1,10)=? ORDER BY dt, id", (date,)).fetchall()
    out = [date, "=" * dwidth(date)]
    grand = 0
    for dt, cat, note, amount, eid in rows:
        out.append(f"{dt[11:16]} #{eid} {cat} ({note}) {int(amount)}")
        grand += int(amount)
    out.append(SEP)
    out.append(f"合計 {grand}")
    return "\n".join(out)


def _detail_month(con, month):
    # 整月依日期排列
    start, end = month_range(month)
    rows = con.execute(
        "SELECT dt, category, note, amount, id FROM entries "
        "WHERE kind='out' AND dt>=? AND dt<? ORDER BY dt, id", (start, end)).fetchall()
    out = [month, "=" * dwidth(month)]
    grand = 0
    cur_day = None
    for dt, cat, note, amount, eid in rows:
        day = dt[:10]
        if day != cur_day:
            if cur_day is not None:
                out.append("")
            out.append(day)
            cur_day = day
        out.append(f"{dt[11:16]} #{eid} {cat} ({note}) {int(amount)}")
        grand += int(amount)
    out.append(SEP)
    out.append(f"合計 {grand}")
    return "\n".join(out)


def cmd_detail(con, a):
    if a.date and a.month:
        print("錯誤：明細查詢請選日期或月份，不要混用")
        return
    if a.date:
        dates = []
        for raw_date in a.date:
            date = normalize_date(raw_date)
            if len(date) != 10:
                print("錯誤：單日明細的日期必須是 YYYY-MM-DD")
                return
            if date not in dates:
                dates.append(date)
        with read_transaction(con):
            output = "\n\n".join(_detail_date(con, date) for date in dates)
        print(output)
        return
    months = selected_months(a.month)
    with read_transaction(con):
        output = "\n\n".join(_detail_month(con, month) for month in months)
    print(output)


def cmd_archive(con, a):
    cutoff = normalize_date(a.before)
    if len(cutoff) != 10:
        print("錯誤：封存截止日必須是 YYYY-MM-DD")
        return
    csv_path = None
    try:
        # 從讀取、寫 CSV 到補期初餘額全程鎖住同一本帳，避免重複封存。
        with write_transaction(con):
            rows = con.execute(
                "SELECT dt, kind, category, note, amount FROM entries WHERE dt < ? "
                "ORDER BY dt, CASE WHEN kind='adj' AND note='期初餘額' THEN 0 ELSE 1 END, id",
                (cutoff,)).fetchall()
            if not rows:
                print(f"沒有 {cutoff} 之前的資料可備份")
                return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            for suffix in range(1000):
                tail = "" if suffix == 0 else f"_{suffix}"
                candidate = DB_PATH.parent / f"backup_{stamp}{tail}.csv"
                try:
                    backup_file = open(
                        candidate, "x", encoding="utf-8-sig", newline=""
                    )
                    csv_path = candidate
                    break
                except FileExistsError:
                    continue
            else:
                raise RuntimeError("無法建立不重複的備份檔名")

            running = 0
            with backup_file:
                w = csv.writer(backup_file)
                w.writerow(["日期", "時間", "科目", "品項", "金額", "餘額"])
                for dt, kind, category, note, amount in rows:
                    running += (-amount if kind == "out" else amount)
                    w.writerow([
                        dt[:10], dt[11:16], label_of(kind, category), note,
                        amount, running,
                    ])
            carry = running  # 被搬走資料的淨額 = 應保留的期初餘額
            con.execute("DELETE FROM entries WHERE dt < ?", (cutoff,))
            if carry != 0:
                con.execute(
                    "INSERT INTO entries(dt,kind,category,note,amount) VALUES(?,?,?,?,?)",
                    (f"{cutoff} 00:00:00", "adj", None, "期初餘額", carry),
                )
            current = balance(con)
    except BaseException:
        # DB 交易若失敗，不留下看似成功但不對應資料庫狀態的 CSV。
        if csv_path is not None and csv_path.exists():
            csv_path.unlink()
        raise
    try:
        shown_path = csv_path.relative_to(Path(__file__).resolve().parent.parent).as_posix()
    except ValueError:
        shown_path = str(csv_path)
    print(f"已備份 {len(rows)} 筆到 {shown_path}，清空後餘額 {current}")


# ---- 說明 ----

# `-h` 告訴 AI 模型有哪些子指令；`guide` 告訴使用者該怎麼開口。兩者是同一份
# 路由規則的正反面：SKILL.md 把說法對應到子指令，這裡把說法列給使用者看，
# 讓人下的提示詞一開始就落在模型認得的說法上。
GUIDE_TITLE = "記帳說明：這樣打最快"

# (說法, 這句話會得到什麼)；說明留空表示該行本身已經講完了。
# 這份清單就是 intercept.py 認得的固定格式，兩邊必須一致——它同時是
# 「教使用者怎麼開口才不會驚動模型」的契約。
GUIDE_SECTIONS = [
    ("【記帳】一次貼多行就是記多筆", [
        ("冰茶40", "科目我自己判斷，判過一次就記住了"),
        ("冰茶40 外食費", "直接指定科目，最準"),
        ("豆包55 記在 8/5 18:00", "指定日期時間，也可寫 昨天18:00 或 19:30"),
        ("買菜後3229", "用剩下的現金倒推這次花了多少"),
        ("提款3000", "提款、薪水、退款、匯款都算入金"),
    ]),
    ("【改帳】每筆都會給一個 #編號，用編號改最快", [
        ("358 居住費", "改科目"),
        ("358 359 360 居住費", "一次改好幾筆"),
        ("424 名稱 巷口饅頭", "改品項名稱"),
        ("358 金額 80", "改金額"),
        ("358 刪除", "刪掉這筆，也可以打「刪除 358」"),
    ]),
    ("【餘額與回顧】", [
        ("餘額", "查現在手上有多少"),
        ("上一筆", "剛剛記的是什麼，也可以問「最近三筆」"),
        ("起始餘額2000", "把目前餘額校正成 2000"),
    ]),
    ("【報表】期間可寫 七月／8月／7/9／六月和七月；不寫就是本月", [
        ("七月分類統計", "每個科目一列合計"),
        ("七月分類明細", "依科目分組，每筆都有編號"),
        ("七月流水帳", "依日期時間排列每一筆"),
        ("展開七月的外食費", "只看指定科目，可一次指定多個"),
    ]),
    ("【科目】新增和刪除也可以一次貼多行", [
        ("列出所有科目", ""),
        ("新增科目 寵物費", ""),
        ("刪除科目 毛孩費", "科目還有帳目在用就不會刪"),
        ("把寵物費更名為毛孩費", "更名會連舊帳一起更新"),
        ("刪除全部科目", "同樣，還有帳目在用就不會刪"),
    ]),
    ("【換掉整組科目】剛開始用的時候會需要，標題後面一行一個", [
        ("科目改成", ""),
        ("餐飲", ""),
        ("買菜", ""),
        ("交通", ""),
        ("也可以擠成一行：科目改成 餐飲、買菜、交通", ""),
        ("整組一次換掉，不會中途變成沒有科目；有一科還在用就整組不換。", ""),
    ]),
    ("【其他】", [
        ("說明", "再看一次這份說明"),
        ("封存 2026 年的舊帳", "匯出 CSV 後清空舊帳，餘額不變"),
    ]),
    ("上面這些是固定格式，我自己就處理得掉。", []),
    ("其他講法（「剛剛那筆冰茶記錯了」之類）我也聽得懂，只是要多繞一圈。", []),
]


def cmd_guide(con, a):
    width = max(
        dwidth(phrase)
        for _, entries in GUIDE_SECTIONS for phrase, desc in entries if desc
    )
    out = [GUIDE_TITLE, "=" * dwidth(GUIDE_TITLE)]
    for index, (section, entries) in enumerate(GUIDE_SECTIONS):
        if index:
            out.append("")
        out.append(section)
        for phrase, desc in entries:
            out.append(f"{rpad(phrase, width)}  {desc}" if desc else phrase)
    print("\n".join(out))


def main():
    p = argparse.ArgumentParser(description="記帳")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("guide")               # 給使用者看的自然語言說法一覽
    sub.add_parser("categories")
    sub.add_parser("balance")             # 查目前餘額

    pca = sub.add_parser("category-add")  # 新增科目
    pca.add_argument("--name", required=True)

    pcd = sub.add_parser("category-delete")  # 刪除未使用的科目
    pcd.add_argument("--name", required=True)

    sub.add_parser("category-clear")       # 清空未使用的所有科目

    pcp = sub.add_parser("category-replace")  # 原子替換整份科目清單
    pcp.add_argument("--name", required=True, action="append")

    pcr = sub.add_parser("category-rename")  # 更名並同步歷史帳目
    pcr.add_argument("--from", dest="old", required=True)
    pcr.add_argument("--to", dest="new", required=True)

    pi = sub.add_parser("in")             # 錢變多
    pi.add_argument("--note", required=True)
    pi.add_argument("--amount", required=True)
    pi.add_argument("--date")

    po = sub.add_parser("out")            # 錢變少
    po.add_argument("--category", required=True)
    po.add_argument("--note", required=True)
    po.add_argument("--amount")           # 花費金額
    po.add_argument("--balance")          # 倒推：剩餘餘額
    po.add_argument("--date")

    pe = sub.add_parser("edit")           # 修改金額／科目／品項
    pe.add_argument("--find", action="append")   # 多個關鍵字為 OR
    pe.add_argument("--rowid", action="append")  # 帳目編號，可重複＝批次
    pe.add_argument("--amount")           # 搜尋時可用原金額縮小範圍
    pe.add_argument("--to")               # 新金額
    pe.add_argument("--category")         # 新科目
    pe.add_argument("--note")             # 新品項名
    pe.add_argument("--date")

    pd = sub.add_parser("delete")         # 刪除
    pd.add_argument("--find", action="append")   # 多個關鍵字為 OR
    pd.add_argument("--rowid", action="append")  # 帳目編號，可重複＝批次
    pd.add_argument("--date")
    pd.add_argument("--amount")

    pl = sub.add_parser("last")           # 最近記入的 N 筆（依 id）
    pl.add_argument("--count")

    sub.add_parser("item-map")            # 檢視學到的品項→科目對應（除錯用）

    pj = sub.add_parser("adjust")         # 絕對校正餘額
    pj.add_argument("--balance", required=True)

    pr = sub.add_parser("report")         # 科目統計
    pr.add_argument("--month", action="append")
    pr.add_argument("--date", action="append")

    pcd = sub.add_parser("category-detail")  # 分帳明細（依科目）
    pcd.add_argument("--month", action="append")
    pcd.add_argument("--date", action="append")

    px = sub.add_parser("expand")         # 展開科目（可多個）
    px.add_argument("--category", required=True, action="append")
    px.add_argument("--month", action="append")
    px.add_argument("--date", action="append")

    pdt = sub.add_parser("detail")        # 明細（依日期排列）
    pdt.add_argument("--month", action="append")
    pdt.add_argument("--date", action="append")

    pa = sub.add_parser("archive")        # 舊資料截斷備份
    pa.add_argument("--before", required=True)

    a = p.parse_args()
    con = connect()
    {
        "guide": cmd_guide,
        "categories": cmd_categories,
        "category-add": cmd_category_add,
        "category-delete": cmd_category_delete,
        "category-clear": cmd_category_clear,
        "category-replace": cmd_category_replace,
        "category-rename": cmd_category_rename,
        "balance": cmd_balance,
        "in": cmd_in,
        "out": cmd_out,
        "edit": cmd_edit,
        "delete": cmd_delete,
        "adjust": cmd_adjust,
        "report": cmd_report,
        "category-detail": cmd_category_detail,
        "expand": cmd_expand,
        "detail": cmd_detail,
        "archive": cmd_archive,
        "last": cmd_last,
        "item-map": cmd_item_map,
    }[a.cmd](con, a)
    con.close()


if __name__ == "__main__":
    main()
