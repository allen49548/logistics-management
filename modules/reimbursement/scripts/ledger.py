#!/usr/bin/env python3
"""幂等台账：保证同一笔报销只入账一次。

用法：
  ledger.py check --message-id om_xxx [--image /path/to.png] [--business-key "driver|date|amount|category"] [--phash HEX]
                 [--driver 王博成 --receipt-date 2026-11-16 --category 油费 --order-no XXX]
  ledger.py check-batch --json '[{...}, {...}]'   # 一次进程批量去重 N 条，省 N 次进程启动
  ledger.py index-add --json '[{...}]'            # 把群里观察到的凭证写入历史池（含跳过/待确认）
  ledger.py index-stats                           # 历史池规模与状态分布
  ledger.py add  --json '{"message_id":...}'     # 也可从 stdin 读 JSON
  ledger.py list --limit 20
  ledger.py stats                                # 含已报销/未报销汇总
  ledger.py set-task --ids 1,2 --task-id tguid --task-url https://...   # 批次绑定群任务
  ledger.py mark-reimbursed --task-id tguid | --ids 1,2                 # 负责人点完成后标记已真实报销
  ledger.py mark-task-status --task-id tguid --status pending|done|invalid
  ledger.py unreimbursed                         # 列出未真实报销的台账条目

输出统一为 JSON。check 命中返回 {"duplicate": true, "layer": "L1", "entry": {...}}。
去重池三层（2026-11-17 用户定案）：台账 entries + 群历史池 voucher_seen + 报表「报销明细」既有行；
命中任一池一律判重复（history_hit_policy=skip_always），并回传 original_source 与是否已入账供清单留痕。
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "..", "config.json")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from runtime_config import get as rc_get  # 配置优先、原值兜底


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def db_path(cfg):
    state = cfg.get("state_dir") or os.path.join(HERE, "..", "state")
    os.makedirs(state, exist_ok=True)
    return os.path.join(state, "ledger.sqlite")


def connect(cfg):
    conn = sqlite3.connect(db_path(cfg))
    conn.row_factory = sqlite3.Row
    # 性能：schema 已就绪时跳过建表/迁移/建索引的 DDL（此前每次 check/add 都全跑一遍）。
    # 用 PRAGMA user_version 做一次性标记，仅在首次建库或结构缺列时执行迁移。
    if _schema_ready(conn):
        return conn
    conn.execute("""CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT UNIQUE,
        chat_id TEXT,
        driver TEXT,
        receipt_date TEXT,
        amount REAL,
        category TEXT,
        image_sha256 TEXT,
        phash TEXT,
        business_key TEXT,
        report_file TEXT,
        sheet TEXT,
        cell TEXT,
        old_value REAL,
        new_value REAL,
        task_id TEXT,
        task_url TEXT,
        task_status TEXT DEFAULT 'none',
        reimbursed INTEGER DEFAULT 0,
        reimbursed_at TEXT,
        no_report INTEGER DEFAULT 0,
        run_id TEXT,
        created_at TEXT
    )""")
    # 旧库迁移：补齐群任务闭环字段
    existing = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    for col, ddl in [
        ("task_id", "TEXT"), ("task_url", "TEXT"),
        ("task_status", "TEXT DEFAULT 'none'"),
        ("reimbursed", "INTEGER DEFAULT 0"), ("reimbursed_at", "TEXT"),
        ("no_report", "INTEGER DEFAULT 0"),
        ("order_no", "TEXT"), ("pay_time", "TEXT"),
        ("voided", "INTEGER DEFAULT 0"), ("voided_at", "TEXT"), ("void_reason", "TEXT"),
    ]:
        if col not in existing:
            conn.execute("ALTER TABLE entries ADD COLUMN %s %s" % (col, ddl))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg ON entries(message_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sha ON entries(image_sha256)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_biz ON entries(business_key)")
    _ensure_history_table(conn)
    conn.commit()
    _mark_schema_ready(conn)
    return conn


def _ensure_history_table(conn):
    """群历史凭证池：记录群里出现过的每一张凭证（含跳过/待确认/未入账）。

    与 entries 的区别：entries 只登记「已入账」的笔，本表登记「出现过」的笔。
    去重因此可以覆盖台账漏记、历史被跳过、以及台账重建前的历史数据。
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS voucher_seen (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT UNIQUE,
        chat_id TEXT,
        msg_time TEXT,
        driver TEXT,
        receipt_date TEXT,
        amount REAL,
        category TEXT,
        order_no TEXT,
        image_sha256 TEXT,
        phash TEXT,
        status TEXT,
        report_written INTEGER DEFAULT 0,
        note TEXT,
        run_id TEXT,
        created_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_order ON voucher_seen(order_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_sha ON voucher_seen(image_sha256)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_driver ON voucher_seen(driver, receipt_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_phash ON voucher_seen(phash)")


# 迁移后的结构指纹：新增列时改这里即可触发一次迁移
_SCHEMA_VERSION = 3
_EXPECTED_COLS = {
    "id", "message_id", "chat_id", "driver", "receipt_date", "amount", "category",
    "image_sha256", "phash", "business_key", "report_file", "sheet", "cell",
    "old_value", "new_value", "task_id", "task_url", "task_status", "reimbursed",
    "reimbursed_at", "no_report", "run_id", "created_at", "order_no", "pay_time",
    "voided", "voided_at", "void_reason",
}


def _schema_ready(conn):
    """表结构完整且 user_version 匹配时返回 True，跳过重复 DDL。"""
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
            return False
        cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
        has_hist = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='voucher_seen'"
        ).fetchone()
        return _EXPECTED_COLS <= cols and bool(has_hist)
    except sqlite3.Error:
        return False


def _mark_schema_ready(conn):
    try:
        conn.execute("PRAGMA user_version = %d" % _SCHEMA_VERSION)
        conn.commit()
    except sqlite3.Error:
        pass


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def dhash_hex(path):
    """32x32 灰度 -> 8x8 差分感知哈希（16 hex chars）。PIL 不可用时返回 None。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(path).convert("L").resize((9, 8))
        px = list(img.getdata())
        bits = 0
        for r in range(8):
            for c in range(8):
                bits = (bits << 1) | (1 if px[r * 9 + c] > px[r * 9 + c + 1] else 0)
        return "%016x" % bits
    except Exception:
        return None


def hamming(a, b):
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except (ValueError, TypeError):
        return 999


def row_dict(row):
    return dict(row) if row is not None else None


def _dup(layer, reason, r):
    """构造『重复命中』返回，并附上原登记时间与当前报销状态，便于通报注明。"""
    e = row_dict(r)
    e_reimbursed = (e.get("reimbursed") or 0)
    return {
        "duplicate": True, "layer": layer, "reason": reason, "entry": e,
        "original_created_at": e.get("created_at"),
        "original_status": "已报销" if e_reimbursed else "待报销（未真实报销）",
        "original_amount": e.get("amount"),
        "original_receipt_date": e.get("receipt_date"),
        "original_message_id": e.get("message_id"),
    }


def _check_record(conn, cfg, message_id=None, image=None, phash=None,
                  business_key=None, order_no=None, amount=None,
                  driver=None, receipt_date=None, category=None,
                  report_cache=None, image_sha256=None):
    """单条凭证去重核心（cmd_check 与 check-batch 共用）：L1-L4 台账 + L5 群历史池 + L6 报表明细。

    返回同 cmd_check：命中返回 {duplicate, layer, entry, ...}；未命中返回
    {duplicate:False, image_sha256, phash}。逻辑与改造前逐字一致。
    """
    cur = conn.cursor()
    if message_id:
        r = cur.execute("SELECT * FROM entries WHERE message_id=?", (message_id,)).fetchone()
        if r:
            return _dup("L1", "同一条消息已处理（含已作废）", r)

    sha = image_sha256
    ph = None
    if image and os.path.isfile(image):
        sha = sha or file_sha256(image)
        ph = dhash_hex(image)
    elif phash:
        ph = phash

    if sha:
        r = cur.execute("SELECT * FROM entries WHERE image_sha256=? LIMIT 1", (sha,)).fetchone()
        if r:
            return _dup("L2", "同一张图片已入账", r)

    if ph:
        threshold = cfg.get("dedup", {}).get("phash_hamming_threshold", 6)
        lookback = int(rc_get(cfg, "phash_lookback_days", 90))
        since = (datetime.now() - timedelta(days=lookback)).isoformat()
        # 只拉必要列：phash 比对 + L3 订单号/金额交叉校验 + 原登记信息，
        # 避免 SELECT * 把 28 列全拖进内存逐条扫描。
        rows = cur.execute(
            "SELECT phash, order_no, amount, message_id, created_at, reimbursed, receipt_date "
            "FROM entries WHERE phash IS NOT NULL AND created_at>=?", (since,)
        ).fetchall()
        best = None
        for r in rows:
            d = hamming(ph, r["phash"])
            if d <= threshold and (best is None or d < best[0]):
                best = (d, r)
        if best:
            matched = best[1]
            # 同版式截图（如微信账单）导致 dHash 相近并非真重复：
            # 订单号不同 → 不同交易；金额不同 → 不同交易。二者皆非重复。
            if order_no and matched["order_no"] and matched["order_no"] != order_no:
                pass
            elif amount is not None and matched["amount"] is not None \
                    and abs(float(matched["amount"]) - float(amount)) > 0.001:
                pass
            else:
                return _dup("L3", "感知哈希相似（疑似同一张票的不同截图），距离=%d" % best[0], matched)

    if order_no:
        r = cur.execute("SELECT * FROM entries WHERE order_no=?", (order_no,)).fetchone()
        if r:
            return _dup("L4", "同一订单号已入账", r)

    if business_key:
        r = cur.execute("SELECT * FROM entries WHERE business_key=? LIMIT 1", (business_key,)).fetchone()
        if r:
            return _dup("L4", "同一司机+日期+金额+类别已入账", r)

    # ---- L5 群历史池：群里出现过就算重复（2026-11-17 用户定案：一律跳过） ----
    if _source_on(cfg, "group_history"):
        layer, row = _history_lookup(conn, cfg, message_id, driver, receipt_date,
                                     amount, category, order_no, sha, ph)
        if row:
            return _dup_external(layer, "群里历史已出现过该凭证", row, "群历史")

    # ---- L6 报表明细：报表里已有同一笔（台账漏记时的兜底） ----
    if _source_on(cfg, "report_detail"):
        hit = _report_detail_lookup(cfg, driver, receipt_date, amount, category,
                                    order_no, message_id, report_cache)
        if hit:
            return _dup_external("L6", "报表「报销明细」已有该笔", hit, "报表明细")

    return {"duplicate": False, "image_sha256": sha, "phash": ph}


def _source_on(cfg, name):
    """去重池开关：config.dedup.sources 未配置时，group_history 与 report_detail 默认开启。"""
    src = (cfg.get("dedup") or {}).get("sources")
    if not src:
        return True
    return name in src


def _dup_external(layer, reason, row, source):
    """群历史池 / 报表明细命中：给出原出现时间、原消息、是否已入账。"""
    e = dict(row)
    entered = bool(e.get("report_written")) or e.get("status") == "entered" \
        or bool(e.get("in_report"))
    if source == "本批":
        status_text = "本轮已出现·不重复计算"
    else:
        status_text = "已入账" if entered else "群里出现过·未入账"
    return {
        "duplicate": True, "layer": layer, "reason": reason, "entry": e,
        "original_source": source,
        "original_created_at": e.get("created_at") or e.get("msg_time") or e.get("seen_at"),
        "original_status": status_text,
        "original_amount": e.get("amount"),
        "original_receipt_date": e.get("receipt_date"),
        "original_message_id": e.get("message_id"),
        "history_entered": entered,
        "policy": "skip_always",
    }


def _history_lookup(conn, cfg, message_id, driver, receipt_date, amount,
                    category, order_no, sha, ph):
    """在群历史池里按 订单号 → 同图 → 感知哈希 → 司机+日期+金额 找同笔。"""
    def same_txn(row):
        """订单号都存在且不同 = 不同交易；金额不同 = 不同交易。"""
        ro = (row.get("order_no") or "").strip() if isinstance(row, dict) else (row["order_no"] or "")
        if order_no and ro and ro != order_no:
            return False
        if amount is not None and row["amount"] is not None \
                and abs(float(row["amount"]) - float(amount)) > 0.001:
            return False
        return True

    excl = " AND (message_id IS NULL OR message_id<>?)"
    tail = (message_id or "",)

    if order_no:
        r = conn.execute("SELECT * FROM voucher_seen WHERE order_no=?" + excl + " LIMIT 1",
                         (order_no,) + tail).fetchone()
        if r:
            return ("L5-订单号", r)

    if sha:
        r = conn.execute("SELECT * FROM voucher_seen WHERE image_sha256=?" + excl + " LIMIT 1",
                         (sha,) + tail).fetchone()
        if r:
            return ("L5-同图", r)

    if ph:
        threshold = (cfg.get("dedup") or {}).get("phash_hamming_threshold", 6)
        lookback = int(rc_get(cfg, "phash_lookback_days", 90))
        since = (datetime.now() - timedelta(days=lookback)).isoformat()
        rows = conn.execute(
            "SELECT * FROM voucher_seen WHERE phash IS NOT NULL AND created_at>=?" + excl,
            (since,) + tail).fetchall()
        best = None
        for r in rows:
            d = hamming(ph, r["phash"])
            if d <= threshold and (best is None or d < best[0]):
                best = (d, r)
        if best and same_txn(best[1]):
            return ("L5-感知哈希", best[1])

    if driver and receipt_date and amount is not None:
        sql = "SELECT * FROM voucher_seen WHERE driver=? AND receipt_date=? AND ABS(amount-?)<0.001"
        params = [driver, receipt_date, float(amount)]
        if category:
            sql += " AND category=?"
            params.append(category)
        for r in conn.execute(sql + excl, tuple(params) + tail).fetchall():
            if same_txn(r):
                return ("L5-司机日期金额", r)

    return (None, None)


def _report_path(cfg, driver, year):
    root = cfg.get("report_root") or ""
    sub = cfg.get("report_subdir") or "{year}年/司机工资"
    fn = cfg.get("report_filename") or "{driver}{year}.xlsx"
    return os.path.join(root, sub.format(year=year), fn.format(driver=driver, year=year))


def _report_records(cfg, driver, receipt_date, cache):
    """读该司机报表「报销明细」既有行（含历史月份），供 L6 交叉校验。按 (driver, year) 缓存。"""
    if not cfg.get("report_root") or not driver:
        return []
    year = (receipt_date or "")[:4] or str(datetime.now().year)
    key = (driver, year)
    if cache is not None and key in cache:
        return cache[key]
    recs = []
    path = _report_path(cfg, driver, year)
    if os.path.isfile(path):
        try:
            import reimb_detail as rd
            rd.apply_layout_config(cfg)
            parts = rd.read_parts(path)
            part, _rid = rd.find_detail_sheet(parts)
            if part:
                xml = next((d.decode("utf-8") for i, d in parts if i.filename == part), "")
                shared = rd.read_shared_strings(parts)
                rows, _lay, _cnt = rd.parse_existing_records(xml, shared)
                for r in rows:
                    recs.append({
                        "message_id": r.get("key") or "",
                        "receipt_date": (r.get("date") or "")[:10],
                        "order_no": (r.get("order_no") or "").strip(),
                        "category": (r.get("category") or "").strip(),
                        "amount": r.get("amount"),
                        "report_file": path,
                        "in_report": True,
                        "status": "entered",
                    })
        except Exception:
            recs = []
    if cache is not None:
        cache[key] = recs
    return recs


def _report_detail_lookup(cfg, driver, receipt_date, amount, category,
                          order_no, message_id, cache):
    for r in _report_records(cfg, driver, receipt_date, cache):
        if message_id and r["message_id"] == message_id:
            return r
        if order_no and r["order_no"] and r["order_no"] == order_no:
            return r
        if order_no and r["order_no"] and r["order_no"] != order_no:
            continue
        if r["receipt_date"] and receipt_date and r["receipt_date"] == receipt_date[:10] \
                and amount is not None and r["amount"] is not None \
                and abs(float(r["amount"]) - float(amount)) < 0.001 \
                and (not category or not r["category"] or r["category"] == category):
            return r
    return None


def cmd_check(args, cfg, conn):
    return _check_record(conn, cfg, args.message_id, args.image, args.phash,
                         args.business_key, args.order_no, args.amount,
                         args.driver, args.receipt_date, args.category, {})


def cmd_check_batch(args, cfg, conn):
    """一次进程批量去重 N 条凭证，省掉 N 次进程启动 + N 次 config 解析 + N 次 SQL 连接。

    输入 JSON（--json 或 stdin）：列表或 {"items":[...]}；每条字段与 check 一致：
    message_id / image / phash / business_key / order_no / amount /
    driver / receipt_date / category（后三项用于群历史池与报表明细比对）。
    输出 {"ok", "count", "results":[...]}，results 每条与单次 check 完全同构。
    """
    raw = args.json
    if raw is None or raw == "-":
        raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": "batch JSON 解析失败：%s" % e}
    items = data if isinstance(data, list) else data.get("items", [])
    results = []
    report_cache = {}   # 每轮只读一次各司机报表明细
    threshold = (cfg.get("dedup") or {}).get("phash_hamming_threshold", 6)
    accepted = []       # 本批已通过（非重复）的凭证，供批内去重
    for it in items:
        if not isinstance(it, dict):
            results.append({"duplicate": False, "error": "非对象条目，已跳过"})
            continue
        sha = it.get("image_sha256")
        ph = it.get("phash")
        img = it.get("image")
        if img and os.path.isfile(img):
            sha = sha or file_sha256(img)
            ph = ph or dhash_hex(img)
        # 批内去重（新增数据之间）：同订单号/同图/同司机日期金额/感知哈希近似
        layer, prev = _batch_peek(accepted, it.get("driver"), it.get("receipt_date"),
                                  it.get("amount"), it.get("category"),
                                  it.get("order_no"), sha, ph, threshold)
        if prev is not None:
            r = _dup_external(layer, "本轮新增数据内部重复", prev, "本批")
            r["message_id"] = it.get("message_id")
            results.append(r)
            continue
        r = _check_record(conn, cfg,
                          it.get("message_id"), None, ph,
                          it.get("business_key"), it.get("order_no"), it.get("amount"),
                          it.get("driver"), it.get("receipt_date"), it.get("category"),
                          report_cache, image_sha256=sha)
        if it.get("message_id"):
            r["message_id"] = it["message_id"]
        results.append(r)
        if not r.get("duplicate"):
            accepted.append({"message_id": it.get("message_id"), "driver": it.get("driver"),
                             "receipt_date": it.get("receipt_date"), "amount": it.get("amount"),
                             "category": it.get("category"), "order_no": it.get("order_no"),
                             "image_sha256": sha, "phash": ph,
                             "status": "pending", "report_written": 0})
    return {"ok": True, "count": len(results), "results": results}


def _batch_peek(accepted, driver, receipt_date, amount, category, order_no, sha, ph, threshold):
    """批内去重：与本次运行中已通过的凭证比，命中返回 (layer, prev_item)。"""
    def same_txn(a):
        if order_no and a.get("order_no") and a["order_no"] != order_no:
            return False
        if amount is not None and a.get("amount") is not None \
                and abs(float(a["amount"]) - float(amount)) > 0.001:
            return False
        return True

    for a in accepted:
        if order_no and a.get("order_no") == order_no:
            return ("B-订单号", a)
        if sha and a.get("image_sha256") == sha:
            return ("B-同图", a)
        if ph and a.get("phash") and hamming(ph, a["phash"]) <= threshold and same_txn(a):
            return ("B-感知哈希", a)
        if driver and receipt_date and amount is not None \
                and a.get("driver") == driver and a.get("receipt_date") == receipt_date \
                and a.get("amount") is not None and same_txn(a):
            if not category or not a.get("category") or a["category"] == category:
                return ("B-司机日期金额", a)
    return (None, None)


def _upsert_seen(conn, it, run_id=None, now=None):
    """写入/更新群历史池一条（幂等键 message_id）。字段缺省用 COALESCE 保留旧值。"""
    if not isinstance(it, dict) or not it.get("message_id"):
        return False
    now = now or datetime.now().isoformat(timespec="seconds")
    sha = it.get("image_sha256")
    ph = it.get("phash")
    img = it.get("image")
    if img and os.path.isfile(img):
        sha = sha or file_sha256(img)
        ph = ph or dhash_hex(img)
    conn.execute("""INSERT INTO voucher_seen
        (message_id, chat_id, msg_time, driver, receipt_date, amount, category,
         order_no, image_sha256, phash, status, report_written, note, run_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(message_id) DO UPDATE SET
          chat_id=COALESCE(excluded.chat_id, voucher_seen.chat_id),
          msg_time=COALESCE(excluded.msg_time, voucher_seen.msg_time),
          driver=COALESCE(excluded.driver, voucher_seen.driver),
          receipt_date=COALESCE(excluded.receipt_date, voucher_seen.receipt_date),
          amount=COALESCE(excluded.amount, voucher_seen.amount),
          category=COALESCE(excluded.category, voucher_seen.category),
          order_no=COALESCE(NULLIF(excluded.order_no,''), voucher_seen.order_no),
          image_sha256=COALESCE(excluded.image_sha256, voucher_seen.image_sha256),
          phash=COALESCE(excluded.phash, voucher_seen.phash),
          status=CASE WHEN voucher_seen.status='voided' THEN 'voided'
                      ELSE COALESCE(excluded.status, voucher_seen.status) END,
          report_written=MAX(voucher_seen.report_written, excluded.report_written),
          note=COALESCE(excluded.note, voucher_seen.note)""",
        (it["message_id"], it.get("chat_id"), it.get("msg_time"), it.get("driver"),
         it.get("receipt_date"), it.get("amount"), it.get("category"),
         it.get("order_no") or "", sha, ph, it.get("status") or "seen",
         1 if it.get("report_written") else 0, it.get("note"),
         it.get("run_id") or run_id, now))
    return True


def cmd_index_add(args, cfg, conn):
    """把本轮在群里观察到的凭证写入历史池（幂等：按 message_id upsert）。

    入参字段：message_id / chat_id / msg_time / driver / receipt_date / amount /
    category / order_no / image(或 image_sha256) / phash / status / report_written / note / run_id
    status: entered(已入账) / pending(待确认) / skipped(跳过) / unknown_driver(未识别) / non_voucher(非票据)
    """
    raw = args.json
    if raw is None or raw == "-":
        raw = sys.stdin.read()
    data = json.loads(raw)
    items = data if isinstance(data, list) else data.get("items", [])
    now = datetime.now().isoformat(timespec="seconds")
    ok, skipped = 0, 0
    for it in items:
        if _upsert_seen(conn, it, args.run_id, now):
            ok += 1
        else:
            skipped += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM voucher_seen").fetchone()[0]
    return {"ok": True, "upserted": ok, "skipped": skipped, "total": total}


def cmd_index_backfill(args, cfg, conn):
    """把历史数据回填进群历史池：台账 entries + 各司机报表「报销明细」既有行。

    台账最早登记时间晚于部分凭证日期（11-13 前发出的凭证不在台账），
    报表回填正好补上这段缺口，保证「群里历史出现过的笔」都参与去重。
    幂等：按 message_id upsert（报表无唯一键时用 report:driver:year:date:amount:序号 合成）。
    """
    import glob
    now = datetime.now().isoformat(timespec="seconds")
    stat = {"from_ledger": 0, "from_report": 0, "report_files": 0}
    if args.from_ledger:
        for r in conn.execute("SELECT * FROM entries").fetchall():
            voided = bool(r["voided"])
            _upsert_seen(conn, {
                "message_id": r["message_id"], "chat_id": r["chat_id"],
                "msg_time": r["pay_time"], "driver": r["driver"],
                "receipt_date": r["receipt_date"], "amount": r["amount"],
                "category": r["category"], "order_no": r["order_no"],
                "image_sha256": r["image_sha256"], "phash": r["phash"],
                "status": "voided" if voided else "entered",
                "report_written": 0 if (voided or r["no_report"]) else 1,
                "note": "台账回填" + ("（已作废）" if voided else ""),
                "run_id": r["run_id"],
            }, None, now)
            stat["from_ledger"] += 1
    if args.from_report:
        root = cfg.get("report_root") or ""
        pattern = os.path.join(root, "*年", "司机工资", "*.xlsx")
        try:
            import reimb_detail as rd
            rd.apply_layout_config(cfg)
        except Exception:
            rd = None
        for path in sorted(glob.glob(pattern)) if rd else []:
            fn = os.path.basename(path)
            year = os.path.basename(os.path.dirname(os.path.dirname(path))).rstrip("年")
            driver = fn[:-5]
            if year and driver.endswith(year):
                driver = driver[:-len(year)]
            try:
                parts = rd.read_parts(path)
                part, _rid = rd.find_detail_sheet(parts)
                if not part:
                    continue
                xml = next((d.decode("utf-8") for i, d in parts if i.filename == part), "")
                rows, _lay, _cnt = rd.parse_existing_records(xml, rd.read_shared_strings(parts))
            except Exception:
                continue
            stat["report_files"] += 1
            for i, row in enumerate(rows):
                date = (row.get("date") or "")[:10]
                key = (row.get("key") or "").strip()
                mid = key or "report:%s:%s:%s:%s:%s" % (driver, year, date, row.get("amount"), i)
                _upsert_seen(conn, {
                    "message_id": mid, "driver": driver, "receipt_date": date,
                    "amount": row.get("amount"), "category": row.get("category"),
                    "order_no": row.get("order_no"), "status": "entered",
                    "report_written": 1,
                    "note": "报表明细回填" + ("" if key else "（报表无唯一键）"),
                }, None, now)
                stat["from_report"] += 1
    conn.commit()
    stat["total"] = conn.execute("SELECT COUNT(*) FROM voucher_seen").fetchone()[0]
    return {"ok": True, **stat}


def cmd_index_stats(args, cfg, conn):
    rows = conn.execute(
        "SELECT status, COUNT(*) n, SUM(CASE WHEN report_written=1 THEN 1 ELSE 0 END) written "
        "FROM voucher_seen GROUP BY status ORDER BY n DESC").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM voucher_seen").fetchone()[0]
    with_order = conn.execute(
        "SELECT COUNT(*) FROM voucher_seen WHERE order_no IS NOT NULL AND order_no<>''").fetchone()[0]
    return {"ok": True, "total": total, "with_order_no": with_order,
            "by_status": [dict(r) for r in rows]}


def cmd_add(args, cfg, conn):
    raw = args.json
    if raw is None or raw == "-":
        raw = sys.stdin.read()
    data = json.loads(raw)
    if not data.get("message_id"):
        return {"ok": False, "error": "message_id 必填"}
    # 去重业务键：订单号优先（相同金额不是重复，订单号+支付时间才决定唯一性）
    if not data.get("business_key"):
        order_no = data.get("order_no")
        if order_no:
            data["business_key"] = "%s|%s|%s" % (
                data.get("driver"), data.get("pay_time") or "", order_no)
        else:
            sha16 = (data.get("image_sha256") or "")[:16]
            data["business_key"] = "%s|%s|%s|%s|%s" % (
                data.get("driver"), data.get("receipt_date") or "",
                data.get("amount"), data.get("category") or "", sha16)
    cols = ["message_id", "chat_id", "driver", "receipt_date", "amount", "category",
            "image_sha256", "phash", "business_key", "order_no", "pay_time",
            "report_file", "sheet", "cell", "old_value", "new_value", "no_report", "run_id"]
    vals = [data.get(c) for c in cols]
    try:
        conn.execute(
            "INSERT INTO entries (%s, created_at) VALUES (%s, ?)" %
            (",".join(cols), ",".join(["?"] * len(cols))),
            vals + [datetime.now().isoformat(timespec="seconds")],
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        return {"ok": False, "error": "重复 message_id: %s" % e}
    # 入账即登记进群历史池，保证「群里出现过」与「已入账」两个池永不脱节
    _upsert_seen(conn, {
        "message_id": data.get("message_id"), "chat_id": data.get("chat_id"),
        "msg_time": data.get("pay_time") or data.get("receipt_date"),
        "driver": data.get("driver"), "receipt_date": data.get("receipt_date"),
        "amount": data.get("amount"), "category": data.get("category"),
        "order_no": data.get("order_no"), "image_sha256": data.get("image_sha256"),
        "phash": data.get("phash"), "status": "entered", "report_written": 1,
        "run_id": data.get("run_id"),
    })
    conn.commit()
    return {"ok": True, "message_id": data["message_id"]}


def cmd_list(args, cfg, conn):
    rows = conn.execute("SELECT * FROM entries ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()
    return {"ok": True, "count": len(rows), "entries": [row_dict(r) for r in rows]}


def cmd_void(args, cfg, conn):
    """软删除：把一笔已入账条目标记作废（不物理删除，保留审计）。
    用于用户复核后判定『这张票不该记录』（如手写票）。作废后不再计入统计/批次。"""
    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    if not ids:
        return {"ok": False, "error": "ids 必填"}
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE entries SET voided=1, voided_at=?, void_reason=? WHERE id IN (%s)"
        % ",".join("?" * len(ids)),
        [now, args.reason] + ids,
    )
    conn.commit()
    rows = conn.execute(
        "SELECT id, driver, amount, voided, void_reason FROM entries WHERE id IN (%s)"
        % ",".join("?" * len(ids)), ids
    ).fetchall()
    return {"ok": True, "voided": [row_dict(r) for r in rows], "reason": args.reason}


def cmd_stats(args, cfg, conn):
    r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) total, MAX(created_at) last FROM entries WHERE voided=0").fetchone()
    pend = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(amount),0) total FROM entries WHERE reimbursed=0 AND voided=0"
    ).fetchone()
    done = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(amount),0) total FROM entries WHERE reimbursed=1 AND voided=0"
    ).fetchone()
    vd = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(amount),0) total FROM entries WHERE voided=1"
    ).fetchone()
    return {
        "ok": True, "entries": r["n"], "total_amount": round(r["total"], 2), "last_written": r["last"],
        "reimbursed": {"count": done["n"], "amount": round(done["total"], 2)},
        "unreimbursed": {"count": pend["n"], "amount": round(pend["total"], 2)},
        "voided": {"count": vd["n"], "amount": round(vd["total"], 2)},
    }


def cmd_set_task(args, cfg, conn):
    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    if not ids:
        return {"ok": False, "error": "ids 必填，逗号分隔的台账 id"}
    conn.execute(
        "UPDATE entries SET task_id=?, task_url=?, task_status='pending' WHERE id IN (%s)"
        % ",".join("?" * len(ids)),
        [args.task_id, args.task_url] + ids,
    )
    conn.commit()
    return {"ok": True, "updated": len(ids), "task_id": args.task_id}


def cmd_mark_reimbursed(args, cfg, conn):
    now = datetime.now().isoformat(timespec="seconds")
    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        conn.execute(
            "UPDATE entries SET reimbursed=1, task_status='done', reimbursed_at=? WHERE id IN (%s)"
            % ",".join("?" * len(ids)),
            [now] + ids,
        )
        n = len(ids)
    elif args.task_id:
        cur = conn.execute(
            "UPDATE entries SET reimbursed=1, task_status='done', reimbursed_at=? WHERE task_id=? AND reimbursed=0",
            (now, args.task_id),
        )
        n = cur.rowcount
    else:
        return {"ok": False, "error": "需要 --ids 或 --task-id"}
    conn.commit()
    return {"ok": True, "marked": n, "at": now}


def cmd_mark_task_status(args, cfg, conn):
    """只更新任务状态字段（不改 reimbursed，由确认逻辑决定）。"""
    conn.execute("UPDATE entries SET task_status=? WHERE task_id=?", (args.status, args.task_id))
    conn.commit()
    return {"ok": True, "task_id": args.task_id, "status": args.status}


def cmd_unreimbursed(args, cfg, conn):
    rows = conn.execute(
        "SELECT id, driver, receipt_date, amount, task_id, task_url, task_status, created_at "
        "FROM entries WHERE reimbursed=0 AND voided=0 ORDER BY id"
    ).fetchall()
    return {"ok": True, "count": len(rows), "entries": [row_dict(r) for r in rows]}


def cmd_balance(args, cfg, conn):
    """按司机分组的滚动账本（只读）。

    四态口径（config.status_model）：只有 reimbursed=0 且 voided=0 的已登记条目
    属于「已登记·待打款」，才计入未报销/未结金额；重复提交、需补材料、手写票、
    纯文本从未登记台账，因此天然不在本查询内；voided=1 也被排除。
    供 C 方案的「个人账本」渲染每位司机的『我的未结』。
    """
    where, params = "voided=0", []
    if args.driver:
        where += " AND driver=?"
        params.append(args.driver)
    rows = conn.execute(
        "SELECT id, driver, receipt_date, pay_time, amount, category, reimbursed, "
        "task_url, created_at FROM entries WHERE %s ORDER BY id" % where, params
    ).fetchall()
    by = {}
    for r in rows:
        e = dict(r)
        d = e.get("driver") or rc_get(cfg, "unknown_driver", "未识别司机")
        rec = by.setdefault(d, {
            "driver": d,
            "reimbursed": {"count": 0, "amount": 0.0},
            "pending_payment": {"count": 0, "amount": 0.0},
            "total": {"count": 0, "amount": 0.0},
            "pending_entries": [],
        })
        amt = float(e.get("amount") or 0)
        rec["total"]["count"] += 1
        rec["total"]["amount"] += amt
        if e.get("reimbursed"):
            rec["reimbursed"]["count"] += 1
            rec["reimbursed"]["amount"] += amt
        else:
            rec["pending_payment"]["count"] += 1
            rec["pending_payment"]["amount"] += amt
            rec["pending_entries"].append({
                "id": e.get("id"), "receipt_date": e.get("receipt_date"),
                "pay_time": e.get("pay_time"), "amount": round(amt, 2),
                "category": e.get("category"), "task_url": e.get("task_url"),
                "created_at": e.get("created_at"),
            })

    # 名册中的司机即便 0 笔也要列出，保证「每位司机单独列出来」
    names = set(by)
    if not args.driver:
        try:
            from driver_roster import all_drivers
            names |= set(all_drivers(cfg, datetime.now().year))
        except Exception:
            names |= set((cfg.get("driver_open_ids") or {}).keys())
            names |= set((cfg.get("driver_roster", {}).get("members") or {}).keys())

    drivers = []
    for d in sorted(names):
        rec = by.get(d) or {
            "driver": d, "reimbursed": {"count": 0, "amount": 0.0},
            "pending_payment": {"count": 0, "amount": 0.0},
            "total": {"count": 0, "amount": 0.0}, "pending_entries": [],
        }
        item = {
            "driver": d,
            "reimbursed": {"count": rec["reimbursed"]["count"],
                           "amount": round(rec["reimbursed"]["amount"], 2)},
            "pending_payment": {"count": rec["pending_payment"]["count"],
                                "amount": round(rec["pending_payment"]["amount"], 2)},
            "outstanding": {"count": rec["pending_payment"]["count"],
                            "amount": round(rec["pending_payment"]["amount"], 2)},
            "total": {"count": rec["total"]["count"],
                      "amount": round(rec["total"]["amount"], 2)},
        }
        if args.details:
            item["pending_entries"] = rec["pending_entries"]
        drivers.append(item)

    grand = {}
    for key in ("reimbursed", "pending_payment", "outstanding", "total"):
        grand[key] = {
            "count": sum(x[key]["count"] for x in drivers),
            "amount": round(sum(x[key]["amount"] for x in drivers), 2),
        }
    return {
        "ok": True, "driver": args.driver or None,
        "outstanding_definition": "仅『已登记·待打款』计入；重复提交、需补材料、手写票、纯文本、作废均不计入",
        "drivers": drivers, "grand": grand,
    }


def cmd_update(args, cfg, conn):
    """更正一笔已入账条目（如人工复核后修正金额/类别/日期）。"""
    sets, vals = [], []
    if args.amount is not None:
        sets.append("amount=?"); vals.append(args.amount)
    if args.category is not None:
        sets.append("category=?"); vals.append(args.category)
    if args.receipt_date is not None:
        sets.append("receipt_date=?"); vals.append(args.receipt_date)
    if args.business_key is not None:
        sets.append("business_key=?"); vals.append(args.business_key)
    if not sets:
        return {"ok": False, "error": "至少提供一个更新字段"}
    vals.append(args.id)
    conn.execute("UPDATE entries SET %s WHERE id=?" % ",".join(sets), vals)
    conn.commit()
    r = conn.execute("SELECT * FROM entries WHERE id=?", (args.id,)).fetchone()
    return {"ok": True, "entry": row_dict(r) if r else None}


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--message-id")
    c.add_argument("--image")
    c.add_argument("--business-key")
    c.add_argument("--phash")
    c.add_argument("--order-no")
    c.add_argument("--pay-time")
    c.add_argument("--amount", type=float)
    c.add_argument("--driver")
    c.add_argument("--receipt-date")
    c.add_argument("--category")
    cb = sub.add_parser("check-batch", help="一次进程批量去重 N 条凭证（省 N 次进程启动/连接/配置解析）")
    cb.add_argument("--json", help="批量 JSON（列表或 {\"items\":[...]}），省略则从 stdin 读")
    a = sub.add_parser("add")
    a.add_argument("--json")
    l = sub.add_parser("list")
    l.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("stats")
    st = sub.add_parser("set-task")
    st.add_argument("--ids", required=True, help="逗号分隔的台账 id")
    st.add_argument("--task-id", required=True)
    st.add_argument("--task-url")
    mr = sub.add_parser("mark-reimbursed")
    mr.add_argument("--ids", help="逗号分隔的台账 id")
    mr.add_argument("--task-id")
    ms = sub.add_parser("mark-task-status")
    ms.add_argument("--task-id", required=True)
    ms.add_argument("--status", required=True)
    ur = sub.add_parser("unreimbursed")
    bal = sub.add_parser("balance", help="按司机分组的滚动账本：已报销 / 已登记·待打款（唯一计入未结）")
    bal.add_argument("--driver", help="只查某一位司机")
    bal.add_argument("--details", action="store_true", help="附带未打款条目逐笔明细")
    up = sub.add_parser("update")
    up.add_argument("--id", type=int, required=True, help="要更正的台账 id")
    up.add_argument("--amount", type=float, help="更正金额")
    up.add_argument("--category")
    up.add_argument("--receipt-date")
    up.add_argument("--business-key")
    v = sub.add_parser("void", help="软删除：作废一笔不该记录的已入账条目（如手写票）")
    v.add_argument("--ids", required=True, help="逗号分隔的台账 id")
    v.add_argument("--reason", default="用户复核判定不记录")
    ia = sub.add_parser("index-add", help="把群里观察到的凭证写入历史池（含跳过/待确认，幂等 upsert）")
    ia.add_argument("--json", help="批量 JSON（列表或 {\"items\":[...]}），省略则从 stdin 读")
    ia.add_argument("--run-id")
    sub.add_parser("index-stats", help="历史池规模与状态分布")
    ib = sub.add_parser("index-backfill", help="把台账与报表明细回填进历史池（补台账覆盖不到的历史）")
    ib.add_argument("--from-ledger", action="store_true", default=True)
    ib.add_argument("--from-report", action="store_true", default=True)
    ib.add_argument("--no-ledger", dest="from_ledger", action="store_false")
    ib.add_argument("--no-report", dest="from_report", action="store_false")
    args = p.parse_args()
    cfg = load_config()
    conn = connect(cfg)
    try:
        handlers = {
            "check": cmd_check, "check-batch": cmd_check_batch, "add": cmd_add,
            "list": cmd_list, "stats": cmd_stats, "set-task": cmd_set_task,
            "mark-reimbursed": cmd_mark_reimbursed, "mark-task-status": cmd_mark_task_status,
            "unreimbursed": cmd_unreimbursed, "balance": cmd_balance,
            "update": cmd_update, "void": cmd_void,
            "index-add": cmd_index_add, "index-stats": cmd_index_stats,
            "index-backfill": cmd_index_backfill,
        }
        out = handlers[args.cmd](args, cfg, conn)
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
