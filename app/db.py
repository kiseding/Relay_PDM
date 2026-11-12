"""Relay PDM —— 数据库层：建表、初始化、演示数据"""
import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data"))
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "plm.db"

TZ8 = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 部门 / 环节

DEPTS = {
    "sales": "业务部",
    "design": "设计部·外观",
    "structure": "设计部·结构",
    "engineering": "工程部",
    "tooling": "模具部",
    "production": "生产部",
    "manage": "管理层",
}

# 七个环节。dept = 这一环由谁做
STAGES = [
    {"no": 1, "key": "intake",    "name": "接单",      "dept": "sales",       "do": "客户需求、做色要求、交期"},
    {"no": 2, "key": "concept",   "name": "外观设计",  "dept": "design",      "do": "效果图、CMF、客户确认"},
    {"no": 3, "key": "structure", "name": "结构设计",  "dept": "structure",   "do": "3D 建模、分模、2D 出图"},
    {"no": 4, "key": "moldprep",  "name": "开模准备",  "dept": "engineering", "do": "开模信息表、开模图、装配图"},
    {"no": 5, "key": "mold",      "name": "开模",      "dept": "tooling",     "do": "报价、采购下单、T0 试模"},
    {"no": 6, "key": "release",   "name": "工程图发布", "dept": "engineering", "do": "正式工程图、BOM 定版"},
    {"no": 7, "key": "massprod",  "name": "量产",      "dept": "production",  "do": "按图生产大货"},
]
STAGE_BY_NO = {s["no"]: s for s in STAGES}

# 每环节转出前必须具备的条件（齐套清单）。
# 依据：微信群反馈的返工点——做色要求没拿到就发 RP、图没更新就通知开模、
# 没有装配图就报价、采购单没下却按口头交期排产。
STAGE_GATES = {
    1: [
        {"k": "req",     "label": "客户需求说明",       "kind": "field", "field": "requirements",
         "why": "需求不落字，后面全靠口头补"},
        {"k": "color",   "label": "做色 / 表面处理要求", "kind": "field", "field": "color_req",
         "why": "缺做色要求会导致 RP 打回来颜色不对"},
        {"k": "due",     "label": "客户交期",           "kind": "field", "field": "due_date",
         "why": "没有交期就无法判断哪一单被延误"},
    ],
    2: [
        {"k": "concept", "label": "外观效果图",         "kind": "doc",   "dtype": "外观效果图",
         "why": "外观结论要能落到图档上"},
        {"k": "custok",  "label": "客户确认外观",       "kind": "field", "field": "customer_ok_at",
         "why": "客户口头说可以不算确认，需记录渠道与日期"},
    ],
    3: [
        {"k": "3d",      "label": "结构 3D 图",         "kind": "doc",   "dtype": "3D图",
         "why": "3D 是开模的唯一依据"},
        {"k": "2d",      "label": "2D 图纸",           "kind": "doc",   "dtype": "2D图纸",
         "why": "尺寸与公差需 2D 表达"},
    ],
    4: [
        {"k": "info",    "label": "开模信息表",         "kind": "doc",   "dtype": "开模信息",
         "why": "模具厂无开模信息表不能报价"},
        {"k": "mold3d",  "label": "开模图",             "kind": "doc",   "dtype": "开模图",
         "why": "开模图与产品图分离，避免拿错版本"},
        {"k": "asm",     "label": "装配图",             "kind": "doc",   "dtype": "装配图",
         "why": "装配关系不清会被模具厂反复追问"},
        {"k": "moldno",  "label": "开模编号已编",       "kind": "flag",  "field": "mold_no_done",
         "why": "开模编号由工程部统一编，未编不能进模具部"},
    ],
    5: [
        {"k": "po",      "label": "模具采购单号",       "kind": "field", "field": "po_no",
         "why": "没有采购单，模具厂无法领料开工"},
        {"k": "podate",  "label": "下单日期",           "kind": "field", "field": "po_date",
         "why": "交期从下单日起算，否则延期责任说不清"},
        {"k": "t0",      "label": "T0 试模已完成",      "kind": "flag",  "field": "t0_done",
         "why": "未试模就发工程图，量产必然改图"},
    ],
    6: [
        {"k": "eng",     "label": "正式工程图",         "kind": "doc",   "dtype": "工程图",
         "why": "量产与检验只认工程图"},
    ],
    7: [],
}

# 未走完的环节里，哪些条件还没满足
def gate_check(conn, order: dict, stage_no: int) -> list[dict]:
    out = []
    for g in STAGE_GATES.get(int(stage_no), []):
        ok, detail = False, ""
        if g["kind"] == "field":
            ok = bool((order.get(g["field"]) or "").strip())
        elif g["kind"] == "flag":
            ok = bool(order.get(g["field"]))
        elif g["kind"] == "doc":
            row = conn.execute(
                """SELECT COUNT(*) c FROM documents d
                   WHERE d.order_id=? AND d.doc_type=?
                     AND EXISTS(SELECT 1 FROM doc_versions v WHERE v.document_id=d.id)""",
                (order["id"], g["dtype"])).fetchone()
            ok = row["c"] > 0
            detail = f"应有「{g['dtype']}」类型图档"
        out.append({"k": g["k"], "label": g["label"], "ok": ok,
                    "why": g["why"], "detail": detail})
    return out


def stage(no: int | None) -> dict:
    return STAGE_BY_NO.get(int(no or 1), STAGES[0])


def dept_name(key: str | None) -> str:
    return DEPTS.get(key or "", key or "")


# ---------------------------------------------------------------- 字典

HANDOVER_STATUS = {
    "pending":  "待主管审批",
    "rejected": "已驳回",
    "approved": "待对方签收",
    "accepted": "已完成",
}

VER_STATUS = {
    "draft": "草稿",
    "published": "已发布",
    "obsolete": "已作废",
}

CHANGE_KIND = {
    "customer": "客户变更",
    "mold": "改模单",
    "eng": "工程变更",
}

CHANGE_STATUS = {
    "open": "待裁定",
    "accepted": "已采纳",
    "rejected": "不采纳",
    "done": "已改完",
}

DOC_TYPES = ["外观效果图", "渲染图", "3D图", "2D图纸", "装配图", "裁片图",
             "开模信息", "开模图", "模流分析", "试模报告", "工程图", "DFM报告", "工艺文件", "其他"]

RP_STATUS = {
    "open": "待安排",
    "making": "制作中",
    "done": "已交付",
    "cancel": "已取消",
}

BOM_SOURCES = ["装配", "自制", "外购", "标准件"]
BOM_UNITS = ["件", "套", "个", "米", "克", "张"]

ROLE_NAMES = {"admin": "管理员", "manager": "主管", "staff": "员工"}


def now_str() -> str:
    return datetime.now(TZ8).strftime("%Y-%m-%d %H:%M:%S")


def hash_pwd(pwd: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"), 120000)
    return f"{salt}${dk.hex()}"


def check_pwd(pwd: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_pwd(pwd, salt), stored)


# ---------------------------------------------------------------- 表结构

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT UNIQUE NOT NULL,
    name       TEXT NOT NULL,
    dept       TEXT NOT NULL DEFAULT 'structure',
    role       TEXT NOT NULL DEFAULT 'staff',
    pwd_hash   TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE NOT NULL,
    contact    TEXT,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT UNIQUE NOT NULL,
    customer_name TEXT,
    product_name  TEXT NOT NULL,
    sales_name    TEXT,
    requirements  TEXT,
    due_date      TEXT,
    current_stage INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'active',
    bom_rev       INTEGER NOT NULL DEFAULT 0,
    note          TEXT,
    color_req     TEXT,
    customer_part_no TEXT,
    customer_ok_at   TEXT,
    customer_ok_note TEXT,
    mold_no_done  INTEGER NOT NULL DEFAULT 0,
    po_no         TEXT,
    po_date       TEXT,
    t0_done       INTEGER NOT NULL DEFAULT 0,
    created_by    INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 打样单（RP / 手样）。可在一单里多次发起，不占用七环节。
CREATE TABLE IF NOT EXISTS rp_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT UNIQUE,
    order_id       INTEGER NOT NULL,
    document_id    INTEGER,
    version_id     INTEGER,
    purpose        TEXT,
    color_req      TEXT,
    lens_spec      TEXT,
    qty            INTEGER NOT NULL DEFAULT 1,
    due_date       TEXT,
    handler        TEXT,
    status         TEXT NOT NULL DEFAULT 'open',
    note           TEXT,
    created_by     INTEGER,
    created_by_name TEXT,
    created_at     TEXT NOT NULL,
    done_at        TEXT,
    done_by_name   TEXT
);
CREATE INDEX IF NOT EXISTS idx_rp_order ON rp_requests(order_id);
CREATE TABLE IF NOT EXISTS documents (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id           INTEGER,
    design_no          TEXT UNIQUE NOT NULL,
    mold_no            TEXT,
    title              TEXT NOT NULL,
    doc_type           TEXT NOT NULL DEFAULT '3D图',
    aliases            TEXT,
    note               TEXT,
    current_version_id INTEGER,
    created_by         INTEGER,
    created_by_name    TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS doc_versions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id           INTEGER NOT NULL,
    version_no            INTEGER NOT NULL,
    file_name             TEXT NOT NULL,
    stored_name           TEXT NOT NULL,
    size                  INTEGER NOT NULL DEFAULT 0,
    sha256                TEXT,
    change_note           TEXT,
    change_id             INTEGER,
    status                TEXT NOT NULL DEFAULT 'draft',
    customer_confirmed    INTEGER NOT NULL DEFAULT 0,
    customer_confirm_note TEXT,
    customer_confirmed_at TEXT,
    customer_confirmed_by TEXT,
    uploaded_by           INTEGER,
    uploaded_by_name      TEXT,
    uploaded_at           TEXT NOT NULL,
    published_by_name     TEXT,
    published_at          TEXT,
    publish_note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_ver_doc ON doc_versions(document_id);

CREATE TABLE IF NOT EXISTS handovers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT UNIQUE,
    order_id        INTEGER NOT NULL,
    from_stage      INTEGER NOT NULL,
    to_stage        INTEGER NOT NULL,
    note            TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    round           INTEGER NOT NULL DEFAULT 1,
    submitter_id    INTEGER,
    submitter_name  TEXT,
    submitter_dept  TEXT,
    submitted_at    TEXT,
    approver_name   TEXT,
    approved_at     TEXT,
    approve_note    TEXT,
    receiver_name   TEXT,
    received_at     TEXT,
    receive_note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ho_order ON handovers(order_id);
CREATE INDEX IF NOT EXISTS idx_ho_status ON handovers(status);

CREATE TABLE IF NOT EXISTS handover_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id INTEGER NOT NULL,
    document_id INTEGER,
    version_id  INTEGER,
    design_no   TEXT,
    mold_no     TEXT,
    doc_title   TEXT,
    doc_type    TEXT,
    version_no  INTEGER,
    file_name   TEXT,
    stored_name TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_hitem ON handover_items(handover_id);

CREATE TABLE IF NOT EXISTS handover_boms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id INTEGER NOT NULL,
    rev         INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL,
    item_count  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hbom ON handover_boms(handover_id);

CREATE TABLE IF NOT EXISTS bom_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL,
    parent_id       INTEGER,
    seq             INTEGER NOT NULL DEFAULT 0,
    part_no         TEXT,
    part_name       TEXT NOT NULL,
    spec            TEXT,
    qty             REAL NOT NULL DEFAULT 1,
    unit            TEXT NOT NULL DEFAULT '件',
    source          TEXT NOT NULL DEFAULT '自制',
    document_id     INTEGER,
    note            TEXT,
    updated_by_name TEXT,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bom_order ON bom_items(order_id);

CREATE TABLE IF NOT EXISTS changes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT,
    order_id           INTEGER NOT NULL,
    kind               TEXT NOT NULL DEFAULT 'customer',
    title              TEXT NOT NULL,
    detail             TEXT,
    source             TEXT,
    customer_confirmed INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'open',
    decision_note      TEXT,
    decided_by_name    TEXT,
    decided_at         TEXT,
    document_id        INTEGER,
    created_by         INTEGER,
    created_by_name    TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chg_order ON changes(order_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id    INTEGER,
    actor_name  TEXT,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   INTEGER,
    order_id    INTEGER,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_order ON audit_logs(order_id);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# 旧库升级：只补列，不动数据
NEW_ORDER_COLS = [
    ("color_req", "TEXT"), ("customer_part_no", "TEXT"),
    ("customer_ok_at", "TEXT"), ("customer_ok_note", "TEXT"),
    ("mold_no_done", "INTEGER NOT NULL DEFAULT 0"),
    ("po_no", "TEXT"), ("po_date", "TEXT"),
    ("t0_done", "INTEGER NOT NULL DEFAULT 0"),
]


def migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
    for name, decl in NEW_ORDER_COLS:
        if name not in have:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {decl}")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.commit()

        if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
            ts = now_str()
            pwd = os.environ.get("PLM_ADMIN_PASSWORD", "admin123")
            rows = [
                ("admin", "系统管理员", "manage", "admin", pwd),
                ("sales", "李强", "sales", "staff", "123456"),
                ("design", "赵敏", "design", "staff", "123456"),
                ("struct", "周工", "structure", "staff", "123456"),
                ("eng", "陈工", "engineering", "staff", "123456"),
                ("mold", "吴师傅", "tooling", "staff", "123456"),
                ("prod", "郑班长", "production", "staff", "123456"),
                ("boss", "张主管", "manage", "manager", "123456"),
            ]
            for uname, name, dept, role, p in rows:
                conn.execute(
                    "INSERT INTO users(username,name,dept,role,pwd_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (uname, name, dept, role, hash_pwd(p), ts),
                )
            conn.commit()

        if conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 0 \
                and os.environ.get("SEED_DEMO", "1") != "0":
            seed_demo(conn)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 演示数据

def _demo_file(name: str, text: str) -> tuple[str, int, str]:
    stored = f"{secrets.token_hex(8)}_{name}"
    p = FILES_DIR / stored
    p.write_text(text, encoding="utf-8")
    data = p.read_bytes()
    return stored, len(data), hashlib.sha256(data).hexdigest()


def seed_demo(conn: sqlite3.Connection) -> None:
    ts = now_str()
    day = (datetime.now(TZ8) - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")

    for n, c, note in [("宏昌实业", "陈经理 138****6621", "老客户，走月结"),
                       ("宁波优家电器", "刘工 137****8890", "新客户，先打样")]:
        conn.execute("INSERT INTO customers(name,contact,note,created_at) VALUES(?,?,?,?)", (n, c, note, ts))

    def add_order(code, cust, prod, req, due, stage_no, note=""):
        return conn.execute(
            """INSERT INTO orders(code,customer_name,product_name,sales_name,requirements,due_date,
                                  current_stage,status,bom_rev,note,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,'active',0,?,3,?,?)""",
            (code, cust, prod, "李强", req, due, stage_no, note, day, ts),
        ).lastrowid

    def add_doc(oid, dno, mold, title, dtype, aliases, note=""):
        return conn.execute(
            """INSERT INTO documents(order_id,design_no,mold_no,title,doc_type,aliases,note,
                                     created_by,created_by_name,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,4,'周工',?,?)""",
            (oid, dno, mold, title, dtype, aliases, note, day, ts),
        ).lastrowid

    def add_ver(did, vno, fname, note, status, uploaded="周工", chg=None):
        stored, size, sha = _demo_file(fname, f"演示占位文件：{fname}\n实际使用时，此处为 UG 导出的 prt / step 原件。\n")
        return conn.execute(
            """INSERT INTO doc_versions(document_id,version_no,file_name,stored_name,size,sha256,change_note,
                                       change_id,status,uploaded_by,uploaded_by_name,uploaded_at,
                                       published_by_name,published_at,publish_note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (did, vno, fname, stored, size, sha, note, chg, status, 4, uploaded, day,
             "张主管" if status in ("published", "obsolete") else None,
             ts if status in ("published", "obsolete") else None,
             "同意发布。" if status in ("published", "obsolete") else None),
        ).lastrowid

    def add_handover(oid, f, t, status, note, when, items=(), bom=None):
        hid = conn.execute(
            """INSERT INTO handovers(order_id,from_stage,to_stage,note,status,round,submitter_id,
                                     submitter_name,submitter_dept,submitted_at,approver_name,approved_at,
                                     approve_note,receiver_name,received_at,receive_note)
               VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)""",
            (oid, f, t, note, status, 4, "周工", stage(f)["dept"], when,
             "张主管" if status in ("approved", "accepted") else None,
             when if status in ("approved", "accepted") else None,
             "结构无问题，同意放行。" if status in ("approved", "accepted") else None,
             "陈工" if status == "accepted" else None,
             when if status == "accepted" else None,
             "已收到，开始确定开模编号。" if status == "accepted" else None),
        ).lastrowid
        conn.execute("UPDATE handovers SET code=? WHERE id=?", (f"JJ-{hid:04d}", hid))
        for did, vid, dno, mold, title, dtype, vno, fname in items:
            conn.execute(
                """INSERT INTO handover_items(handover_id,document_id,version_id,design_no,mold_no,
                                              doc_title,doc_type,version_no,file_name)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (hid, did, vid, dno, mold, title, dtype, vno, fname),
            )
        if bom:
            conn.execute(
                "INSERT INTO handover_boms(handover_id,rev,payload,item_count,created_at) VALUES(?,?,?,?,?)",
                (hid, bom[0], json.dumps(bom[1], ensure_ascii=False), len(bom[1]), when),
            )
        return hid

    # ============ 订单 1：走到第 4 环（工程部手上）
    o1 = add_order("DD-2026-0113", "宏昌实业", "电饭煲上盖注塑模",
                   "外观件，A 面高光，卡扣结构要能一次脱模；材质 ABS+PC。客户提供 3D 与 2D 各一份。",
                   "2026-12-20", 4, "客户交期较紧，先出上盖本体，装饰圈后续补充。")

    d1 = add_doc(o1, "UG-2601-0113-01", "M-2601-A", "上盖本体", "3D图", "KL-8891, 上盖主体", "客户料号：KL-8891")
    v1 = add_ver(d1, 1, "上盖本体_v1.prt", "首版，按客户提供的 3D 建模。", "obsolete")
    v2 = add_ver(d1, 2, "上盖本体_v2.prt", "按 BG-0001 三个卡扣下移 3mm。", "published", chg=1)
    conn.execute("UPDATE documents SET current_version_id=? WHERE id=?", (v2, d1))

    d2 = add_doc(o1, "UG-2601-0113-02", "M-2601-B", "上盖装饰圈", "2D图纸", "装饰环, KL-8892")
    v3 = add_ver(d2, 1, "上盖装饰圈_v1.dwg", "首版 2D 尺寸图。", "published")
    conn.execute("UPDATE documents SET current_version_id=? WHERE id=?", (v3, d2))

    d3 = add_doc(o1, "UG-2601-0113-05", "M-2601-E", "卡扣镶件", "3D图", "镶件", "模具部要求单独制作镶件。")
    v4 = add_ver(d3, 1, "卡扣镶件_v1.prt", "首版，待审批。", "draft")

    add_handover(o1, 1, 2, "accepted", "外观方案已获客户确认，转结构设计。", day)
    add_handover(o1, 2, 3, "accepted", "结构 3D 已完成，转工程部确定开模编号。", day)
    add_handover(o1, 3, 4, "accepted", "开模图与 BOM 初版一并提交，请核对镶件。", ts,
                 items=[(d1, v2, "UG-2601-0113-01", "M-2601-A", "上盖本体", "3D图", 2, "上盖本体_v2.prt"),
                        (d2, v3, "UG-2601-0113-02", "M-2601-B", "上盖装饰圈", "2D图纸", 1, "上盖装饰圈_v1.dwg")])

    # BOM：三层，其中一行故意黄灯
    bom_rows = [
        (None, 0, "SP-2601-00", "上盖组件", "ABS+PC 本色", 1, "套", "装配", None, ""),
        (None, 0, "UG-2601-0113-01", "上盖本体", "ABS+PC 本色", 1, "件", "自制", d1, ""),
        (None, 0, "UG-2601-0113-02", "上盖装饰圈", "PC 透明", 1, "件", "自制", d2, ""),
        (None, 0, "GB/T 818 M2×5", "十字槽盘头螺钉", "不锈钢", 3, "件", "标准件", None, ""),
        (None, 0, "UG-2601-0113-05", "卡扣镶件", "S136 淬火", 2, "件", "自制", None, "图档尚未关联，待模具部确认结构。"),
    ]
    parent_map = {}
    for i, (_, seq, pno, pname, spec, qty, unit, src, did, note) in enumerate(bom_rows):
        pid = conn.execute(
            """INSERT INTO bom_items(order_id,parent_id,seq,part_no,part_name,spec,qty,unit,source,
                                     document_id,note,updated_by_name,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (o1, None, i, pno, pname, spec, qty, unit, src, did, note, "陈工", ts),
        ).lastrowid
        parent_map[i] = pid
    # 1 是根，2/3/5 挂它下面，4 挂 3 下面
    conn.execute("UPDATE bom_items SET parent_id=? WHERE id=?", (parent_map[0], parent_map[1]))
    conn.execute("UPDATE bom_items SET parent_id=? WHERE id=?", (parent_map[0], parent_map[2]))
    conn.execute("UPDATE bom_items SET parent_id=? WHERE id=?", (parent_map[0], parent_map[4]))
    conn.execute("UPDATE bom_items SET parent_id=? WHERE id=?", (parent_map[2], parent_map[3]))
    conn.execute("UPDATE orders SET bom_rev=3 WHERE id=?", (o1,))

    # 第 4 环：工程部已提交交接单，等主管审批
    add_handover(o1, 4, 5, "pending", "开模编号 M-2601-A/B/E 已确定，请审批 BOM 与开模图。", ts,
                 items=[(d1, v2, "UG-2601-0113-01", "M-2601-A", "上盖本体", "3D图", 2, "上盖本体_v2.prt")],
                 bom=(3, [{"no": "1", "part_no": "SP-2601-00", "part_name": "上盖组件", "spec": "ABS+PC 本色",
                           "qty": 1, "unit": "套", "source": "装配", "doc": "", "lamp": "ok"},
                          {"no": "1.1", "part_no": "UG-2601-0113-01", "part_name": "上盖本体", "spec": "ABS+PC 本色",
                           "qty": 1, "unit": "件", "source": "自制", "doc": "v2 已发布", "lamp": "ok"},
                          {"no": "1.2", "part_no": "UG-2601-0113-02", "part_name": "上盖装饰圈", "spec": "PC 透明",
                           "qty": 1, "unit": "件", "source": "自制", "doc": "v1 已发布", "lamp": "ok"},
                          {"no": "1.2.1", "part_no": "GB/T 818 M2×5", "part_name": "十字槽盘头螺钉", "spec": "不锈钢",
                           "qty": 3, "unit": "件", "source": "标准件", "doc": "", "lamp": "ok"},
                          {"no": "1.3", "part_no": "UG-2601-0113-05", "part_name": "卡扣镶件", "spec": "S136 淬火",
                           "qty": 2, "unit": "件", "source": "自制", "doc": "未挂图档", "lamp": "warn"}]))

    # 变更单
    conn.execute(
        """INSERT INTO changes(code,order_id,kind,title,detail,source,customer_confirmed,status,
                               decision_note,decided_by_name,decided_at,document_id,created_by,created_by_name,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("BG-0001", o1, "customer", "卡扣位置整体下移 3mm",
         "客户装配时发现上盖扣合不紧，要求三个卡扣全部下移 3mm，其余不变，微信已发送截图。",
         "微信", 1, "done", "已采纳，v2 已修改，客户查看渲染图后确认。", "张主管", day, d1, 2, "李强", day),
    )
    conn.execute(
        """INSERT INTO changes(code,order_id,kind,title,detail,source,customer_confirmed,status,
                               decision_note,decided_by_name,decided_at,document_id,created_by,created_by_name,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("BG-0002", o1, "customer", "A 面增加晒纹（细火花纹）",
         "客户反馈外观偏亮，希望改为细纹，纹号尚未提供，要求先行报价。", "电话", 0, "open",
         None, None, None, None, 2, "李强", ts),
    )

    # ============ 订单 2：刚接单
    o2 = add_order("DD-2026-0125", "宁波优家电器", "加湿器底座",
                   "小批量试产，结构简单，主要确认装配孔位。", "2026-12-05", 1)

    # ============ 订单 3：结构手上（你有活干）
    o3 = add_order("DD-2026-0131", "宏昌实业", "咖啡机外壳",
                   "双色外壳，A 面要咬花；内部需走线空间，客户希望减重 10%。", "2027-01-15", 3)
    d4 = add_doc(o3, "UG-2601-0131-01", None, "外壳主体", "3D图", "咖啡机主壳")
    v5 = add_ver(d4, 1, "外壳主体_v1.prt", "首版结构，待客户确认外观纹路。", "draft")
    add_handover(o3, 1, 2, "accepted", "需求已明确，转外观设计。", day)
    add_handover(o3, 2, 3, "accepted", "外观方案已定稿（咬花 PT-1102），转结构设计。", ts)

    # ============ 订单 4 / 5：给总看板看「卡住」和「快完」两种状态
    def ago(n: int) -> str:
        return (datetime.now(TZ8) - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")

    o4 = add_order("DD-2026-0108", "广东顺德美厨", "榨汁机底座",
                   "底座与刀座一体，壁厚 2.2；客户要求能过 1.2m 跌落测试。", "2026-12-10", 5,
                   "模具部反馈前模滑块位置无法加工，待工程部确定改法。")
    d5 = add_doc(o4, "UG-2601-0108-01", "M-2512-C", "底座主体", "3D图", "刀座一体, MC-7712")
    v6 = add_ver(d5, 1, "底座主体_v1.prt", "首版，滑块结构按工程部意见改过一次。", "published")
    conn.execute("UPDATE documents SET current_version_id=? WHERE id=?", (v6, d5))
    for i, (f, t) in enumerate([(1, 2), (2, 3), (3, 4), (4, 5)]):
        add_handover(o4, f, t, "accepted", "按流程转下一环节。", ago(20 - i * 2))
    conn.execute("UPDATE orders SET created_at=?, updated_at=? WHERE id=?", (ago(22), ago(14), o4))

    o5 = add_order("DD-2026-0119", "宏昌实业", "电水壶外壳",
                   "不锈钢外壳配 PP 内衬，A 面拉丝。", "2026-11-30", 7)
    d6 = add_doc(o5, "UG-2601-0119-01", "M-2511-B", "外壳主体", "3D图", "壶身")
    v7 = add_ver(d6, 1, "外壳主体_v1.prt", "定版，已量产。", "published")
    conn.execute("UPDATE documents SET current_version_id=? WHERE id=?", (v7, d6))
    for i, (f, t) in enumerate([(1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7)]):
        add_handover(o5, f, t, "accepted", "按流程转下一环节。", ago(14 - i * 2))
    conn.execute("UPDATE orders SET created_at=?, updated_at=? WHERE id=?", (ago(16), ago(4), o5))

    # ============ 补齐新字段：让演示数据同时呈现「齐套」与「缺口」两种状态
    conn.execute(
        """UPDATE orders SET color_req=?, customer_part_no=?, customer_ok_at=?, customer_ok_note=?,
                             mold_no_done=1 WHERE id=?""",
        ("A 面高光本色注塑；装饰圈 PC 透明，透光率 ≥88%", "KL-8891 / KL-8892",
         "2026-11-09 10:20", "客户验样后在微信群回复「外观 OK」，附 3 张对比图。", o1))
    # o2 刚接单：故意不填做色要求，演示第 1 环被卡住
    conn.execute("UPDATE orders SET color_req='' WHERE id=?", (o2,))
    conn.execute(
        """UPDATE orders SET color_req=?, customer_ok_at=?, customer_ok_note=? WHERE id=?""",
        ("A 面咬花 PT-1102，B 面火花纹；主体雾面深灰", "2026-11-05 14:40",
         "外观方案由客户设计师邮件确认。", o3))
    conn.execute(
        """UPDATE orders SET color_req=?, customer_part_no=?, customer_ok_at=?,
                             mold_no_done=1, po_no=?, po_date=?, t0_done=0 WHERE id=?""",
        ("底座黑色亮面，刀座 PP 本色", "MC-7712",
         "2026-10-12 16:30", "PO-2026-1187", "2026-10-09", o4))
    conn.execute(
        """UPDATE orders SET color_req=?, customer_ok_at=?, mold_no_done=1,
                             po_no=?, po_date=?, t0_done=1 WHERE id=?""",
        ("A 面拉丝不锈钢，内衬 PP 本色", "2026-10-20 09:15",
         "PO-2026-1102", "2026-09-18", o5))

    # 第 5 环的订单补开模信息表与装配图（否则演示数据自身过不了第 4 环门槛）
    for dtype, title, fname in [("开模信息", "底座开模信息表", "MC-7712 开模信息.xlsx"),
                                ("装配图", "底座装配图", "底座装配图.dwg")]:
        d = add_doc(o4, f"UG-2601-0108-{'09' if dtype == '开模信息' else '10'}", "M-2512-C",
                    title, dtype, "")
        v = add_ver(d, 1, fname, "开模前定版。", "published")
        conn.execute("UPDATE documents SET current_version_id=? WHERE id=?", (v, d))

    # 打样单：一单在手，一单已交付
    conn.execute(
        """INSERT INTO rp_requests(code,order_id,document_id,version_id,purpose,color_req,lens_spec,
                                   qty,due_date,handler,status,note,created_by,created_by_name,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("RP-090318", o1, d1, v2, "确认卡扣下移后的装配手感", "砂黑手工喷漆，哑光",
         "1.6mm PC 平片", 2, "2026-11-18", "陈文建", "making",
         "手样做二射前需将脚垫、鼻垫分离建模。", 2, "李强", ts))
    conn.execute(
        """INSERT INTO rp_requests(code,order_id,document_id,version_id,purpose,color_req,lens_spec,
                                   qty,due_date,handler,status,note,created_by,created_by_name,created_at,
                                   done_at,done_by_name)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("RP-090316", o4, d5, v6, "客户拍照样", "黑色亮面", "1.5mm 尼龙",
         1, "2026-10-25", "唐志杰", "done", "已交业务寄客户。", 2, "李强", day,
         "2026-10-23 17:40", "陈文建"))

    for act, detail, oid in [        ("创建订单", "DD-2026-0113 电饭煲上盖注塑模", o1),
        ("上传版本", "UG-2601-0113-01 上盖本体 v1", o1),
        ("审批通过", "交接 01→02 外观设计 → 结构设计", o1),
        ("上传版本", "UG-2601-0113-01 上盖本体 v2", o1),
        ("提交交接", "交接 04→05 开模准备 → 开模", o1),
        ("创建订单", "DD-2026-0131 咖啡机外壳", o3),
    ]:
        conn.execute(
            "INSERT INTO audit_logs(actor_id,actor_name,action,target_type,target_id,order_id,detail,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (4, "周工", act, "order", oid, oid, detail, ts),
        )


def reset_business_data(conn: sqlite3.Connection) -> None:
    for t in ("handover_items", "handover_boms", "handovers", "doc_versions", "documents",
              "bom_items", "changes", "rp_requests", "orders", "customers", "audit_logs"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                 "('handover_items','handover_boms','handovers','doc_versions','documents',"
                 "'bom_items','changes','rp_requests','orders','customers','audit_logs')")
    conn.commit()
