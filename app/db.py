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
    "finishing": "后工艺部",
    "production": "生产部",
    "manage": "管理层",
}

# 八个环节。dept = 这一环由谁做
# 塑胶产品（眼镜 / 手办类）注塑完不等于成品：喷油、电镀、移印、组装都是必经工序，
# 且经常是拖交期的那一段，所以单独成一环，堵在谁那里看板能直接看出来。
STAGES = [
    {"no": 1, "key": "intake",    "name": "接单",        "dept": "sales",       "do": "客户需求、做色要求、交期"},
    {"no": 2, "key": "concept",   "name": "外观设计",    "dept": "design",      "do": "效果图、CMF、客户确认"},
    {"no": 3, "key": "structure", "name": "结构设计",    "dept": "structure",   "do": "3D 建模、分模、2D 出图"},
    {"no": 4, "key": "moldprep",  "name": "开模准备",    "dept": "engineering", "do": "开模信息表、开模图、装配图"},
    {"no": 5, "key": "mold",      "name": "开模",        "dept": "tooling",     "do": "报价、采购下单、T0 试模"},
    {"no": 6, "key": "finishing", "name": "后工艺·组装",  "dept": "finishing",   "do": "喷油电镀移印、组装、色板确认"},
    {"no": 7, "key": "release",   "name": "工程图发布",   "dept": "engineering", "do": "正式工程图、BOM 定版"},
    {"no": 8, "key": "massprod",  "name": "量产",        "dept": "production",  "do": "按图生产大货"},
]
STAGE_BY_NO = {s["no"]: s for s in STAGES}

# 最终环节号（多处判定用，别再写死 7）
LAST_STAGE = STAGES[-1]["no"]

# 塑胶件常见的后工艺 / 表面处理。做成字典，BOM 与齐套清单都用它。
FINISHING_KINDS = [
    {"k": "paint",    "name": "喷油（手工喷漆）"},
    {"k": "plating",  "name": "电镀 / 真空镀"},
    {"k": "printing", "name": "移印 / 丝印"},
    {"k": "hotstamp", "name": "烫金"},
    {"k": "water",    "name": "水贴 / 转印"},
    {"k": "uv",       "name": "UV 喷涂"},
    {"k": "polish",   "name": "抛光 / 拉丝"},
    {"k": "texture",  "name": "咬花 / 晒纹"},
    {"k": "none",     "name": "素材直接使用"},
]
FINISHING_BY_KEY = {f["k"]: f for f in FINISHING_KINDS}

# 部件来源：决定要不要挂我方图档
PART_SOURCES = {
    "made":  {"k": "made",  "name": "自制件",   "need_doc": True,
              "desc": "我方设计并注塑，必须有 3D 与 2D 图档"},
    "buy":   {"k": "buy",   "name": "外购件",   "need_doc": False,
              "desc": "镜片、螺丝、铰链等供应商件，要规格书与到货确认，不要求我方 3D"},
    "std":   {"k": "std",   "name": "标准件",   "need_doc": False,
              "desc": "国标件，只登记型号与数量"},
    "assy":  {"k": "assy",  "name": "装配体",   "need_doc": False,
              "desc": "由下级件组成,自身不单独出图"},
}
PART_SOURCE_BY_KEY = PART_SOURCES

# 需求来源：自主开发 / 客户来案
ORDER_SOURCES = {
    "own":     {"key": "own",     "name": "自主",  "short": "自主",
                "desc": "我司自主立项开发，外观与结构由我司主导"},
    "client":  {"key": "client",  "name": "客案",  "short": "客案",
                "desc": "客户来图 / 来样，依客户既定方案转换落地"},
}
SOURCE_BY_KEY = ORDER_SOURCES
DEFAULT_SOURCE = "own"

# 每环节转出前必须具备的条件（齐套清单）。
# 依据：微信群反馈的返工点——做色要求没拿到就发 RP、图没更新就通知开模、
# 没有装配图就报价、采购单没下却按口头交期排产。
# only = 仅该来源需满足；skip = 该来源不需满足
STAGE_GATES = {
    1: [
        {"k": "req",     "label": "客户需求说明",       "kind": "field", "field": "requirements",
         "why": "不落字就全靠口头"},
        {"k": "color",   "label": "做色 / 表面处理要求", "kind": "field", "field": "color_req",
         "why": "缺了 RP 打回来颜色不对"},
        {"k": "custok",  "label": "客案原始资料已收",   "kind": "flag",  "field": "client_ref_done",
         "only": "client", "why": "来图来样要留档，否则后面各说各的"},
        {"k": "due",     "label": "客户交期",           "kind": "field", "field": "due_date",
         "why": "没交期说不清谁延误"},
    ],
    2: [
        {"k": "concept", "label": "外观效果图",         "kind": "doc",   "dtype": "外观效果图",
         "why": "结论要落在图档上"},
        {"k": "cmf",     "label": "色板 / 色号已定",    "kind": "field", "field": "color_std",
         "why": "颜色靠文字描述必有争议，要有色号或色板编号"},
        {"k": "custok",  "label": "客户确认外观",       "kind": "field", "field": "customer_ok_at",
         "skip": "client", "why": "口头点头不算，要渠道与日期"},
    ],
    3: [
        {"k": "3d",      "label": "结构 3D 图",         "kind": "doc",   "dtype": "3D图",
         "why": "开模唯一依据"},
        {"k": "2d",      "label": "2D 图纸",           "kind": "doc",   "dtype": "2D图纸",
         "why": "尺寸公差靠 2D"},
        {"k": "shrink",  "label": "缩水与变形已评估",   "kind": "flag",  "field": "shrink_ok",
         "why": "眼镜细长件、手办薄壁件不改缩水，出来就是翘的"},
    ],
    4: [
        {"k": "info",    "label": "开模信息表",         "kind": "doc",   "dtype": "开模信息",
         "why": "没这张表模具厂不报价"},
        {"k": "mold3d",  "label": "开模图",             "kind": "doc",   "dtype": "开模图",
         "why": "与产品图分开，避免拿错版"},
        {"k": "asm",     "label": "装配图",             "kind": "doc",   "dtype": "装配图",
         "why": "关系不清会被反复追问"},
        {"k": "moldno",  "label": "开模编号已编",       "kind": "flag",  "field": "mold_no_done",
         "why": "工程部统一编，未编不进模具部"},
        {"k": "fit",     "label": "配镜 / 组装干涉已查", "kind": "flag",  "field": "fit_ok",
         "why": "镜片沟槽、铰链封胶位这类配合，不查就是试模后返工"},
    ],
    5: [
        {"k": "po",      "label": "模具采购单号",       "kind": "field", "field": "po_no",
         "why": "没单子模具厂不领料"},
        {"k": "podate",  "label": "下单日期",           "kind": "field", "field": "po_date",
         "why": "交期从下单日算"},
        {"k": "t0",      "label": "T0 试模已完成",      "kind": "flag",  "field": "t0_done",
         "why": "未试模就发图，量产必改"},
        {"k": "t0ok",    "label": "试模样已确认",       "kind": "field", "field": "t0_ok_at",
         "why": "试出来不等于客户认，要有确认日期"},
    ],
    6: [
        {"k": "fin",     "label": "后工艺方式已定",     "kind": "field", "field": "finishing_kind",
         "why": "喷油还是电镀、移印还是烫金，决定工序与成本"},
        {"k": "finsam",  "label": "后工艺首件已确认",   "kind": "field", "field": "finishing_ok_at",
         "why": "批量喷油前不对首件，整批颜色跑偏就是整批报废"},
        {"k": "asmok",   "label": "组装样品已确认",     "kind": "flag",  "field": "assy_ok",
         "why": "眼镜组装后才知道铰链顺不顺、手办拼缝大不大"},
    ],
    7: [
        {"k": "eng",     "label": "正式工程图",         "kind": "doc",   "dtype": "工程图",
         "why": "量产与检验只认它"},
    ],
    8: [],
}


def order_source(key: str | None) -> dict:
    return ORDER_SOURCES.get(key or DEFAULT_SOURCE, ORDER_SOURCES[DEFAULT_SOURCE])


def _gate_applies(g: dict, src: str) -> bool:
    if g.get("only") and g["only"] != src:
        return False
    if g.get("skip") and g["skip"] == src:
        return False
    return True


# 未走完的环节里，哪些条件还没满足
def gate_check(conn, order: dict, stage_no: int) -> list[dict]:
    src = order.get("source") or DEFAULT_SOURCE
    out = []
    for g in STAGE_GATES.get(int(stage_no), []):
        if not _gate_applies(g, src):
            continue
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

# 操作记录里显示的中文字段名（记录审计时用，别让人看到 color_std 这种键）
ORDER_FIELD_LABELS = {
    "customer_name": "客户", "product_name": "产品名", "due_date": "交期",
    "requirements": "需求说明", "customer_part_no": "客户料号", "note": "备注",
    "color_req": "做色要求", "color_std": "色板色号", "source": "需求来源",
    "customer_ok_at": "客户确认时间", "customer_ok_note": "客户确认备注",
    "t0_ok_at": "试样确认时间", "finishing_kind": "后工艺方式",
    "finishing_ok_at": "后工艺首件确认", "po_no": "模具采购单号", "po_date": "下单日期",
    "mold_no_done": "开模编号已编", "t0_done": "T0 试模完成",
    "client_ref_done": "客案原始资料已收", "shrink_ok": "缩水与变形已评估",
    "fit_ok": "配镜组装干涉已查", "assy_ok": "组装样品已确认",
}

BOM_FIELD_LABELS = {
    "part_no": "零件号", "part_name": "零件名称", "spec": "规格材质", "qty": "数量",
    "unit": "单位", "source": "来源", "parent_id": "上级件", "document_id": "对应图档",
    "note": "备注",
}

# ---------------------------------------------------------------- 修改权限
# 操作失误难免，所以凡是能录进来的都要能改。但不能人人能改，
# 这里集中定义「谁能改什么」，页面只负责按 can() 决定按钮显不显示。
#   scope: own_dept → 本部门的人可以改；mgr → 主管及以上；
#          owner → 建单人自己 + 主管；admin → 仅管理员
EDIT_RULES = {
    # 需求基本信息（客户、产品名、交期、需求说明、料号、备注）
    "order.base":       {"scope": "owner",    "label": "需求基本信息"},
    # 需求来源（自主/客案）
    "order.source":     {"scope": "mgr",      "label": "需求来源"},
    # 交期（影响排产，单独收紧）
    "order.due":        {"scope": "mgr",      "label": "交期"},
    # 做色要求、色号、后工艺这类技术参数
    "order.tech":       {"scope": "own_dept", "dept": ("sales", "design", "engineering"),
                         "label": "技术参数"},
    # 图档属性（名称、类型、别名、备注）
    "doc.meta":         {"scope": "own_dept", "dept": ("design", "structure", "engineering"),
                         "label": "图档信息"},
    # 开模编号（工程部统一编）
    "doc.mold_no":      {"scope": "own_dept", "dept": ("engineering",), "label": "开模编号"},
    # BOM 增删改（工程部）
    "bom.edit":         {"scope": "own_dept", "dept": ("engineering",), "label": "BOM"},
    # 打样单状态
    "rp.edit":          {"scope": "own_dept", "dept": ("engineering", "sales", "design"),
                         "label": "打样单"},
    # 客户资料附件
    "order.files":      {"scope": "owner",    "label": "客户资料"},
    # 结案 / 作废
    "order.close":      {"scope": "mgr",      "label": "结案"},
}


def can(me: dict | None, action: str, order: dict | None = None) -> bool:
    """判断当前用户能否执行某个修改动作。

    管理员永远可以；其余按 EDIT_RULES 的 scope 判定。
    order 用于 owner / own_dept 判定（需要知道建单人与所属部门）。
    """
    if not me:
        return False
    rule = EDIT_RULES.get(action)
    if not rule:
        return False
    if me.get("role") == "admin":
        return True
    scope = rule["scope"]
    if scope == "admin":
        return False
    if scope == "mgr":
        return me.get("role") in ("manager", "admin")
    if scope == "own_dept":
        depts = rule.get("dept") or ()
        return me.get("dept") in depts or me.get("role") == "manager"
    if scope == "owner":
        if me.get("role") == "manager":
            return True
        if order is None:
            return False
        return order.get("created_by") == me.get("id")
    return False


def can_edit_note(me: dict | None, action: str, order: dict | None = None) -> str:
    """返回为什么不能改，便于提示。"""
    if not me:
        return "请先登录"
    if me.get("role") == "admin":
        return ""
    rule = EDIT_RULES.get(action) or {}
    if rule.get("scope") == "admin":
        return f"{rule.get('label','该数据')}仅管理员可修改"
    if rule.get("scope") == "mgr" and me.get("role") != "manager":
        return f"{rule.get('label','该数据')}需要主管或管理员修改"
    if rule.get("scope") == "own_dept":
        return f"{rule.get('label','该数据')}由{'、'.join(DEPTS.get(d, d) for d in rule.get('dept', ()))}维护"
    if rule.get("scope") == "owner":
        return f"{rule.get('label','该数据')}仅建单人或主管可修改"
    return "当前账号无修改权限"


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
    created_at TEXT NOT NULL,
    updated_at TEXT
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
    source        TEXT NOT NULL DEFAULT 'own',
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
    client_ref_done  INTEGER NOT NULL DEFAULT 0,
    color_std        TEXT,
    shrink_ok        INTEGER NOT NULL DEFAULT 0,
    fit_ok           INTEGER NOT NULL DEFAULT 0,
    t0_ok_at         TEXT,
    finishing_kind   TEXT,
    finishing_ok_at  TEXT,
    assy_ok          INTEGER NOT NULL DEFAULT 0,
    mold_no_done  INTEGER NOT NULL DEFAULT 0,
    po_no         TEXT,
    po_date       TEXT,
    t0_done       INTEGER NOT NULL DEFAULT 0,
    created_by    INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 需求附件（客户提供的资料：图档、样板照片、邮件截图等，不进主流程）
CREATE TABLE IF NOT EXISTS order_files (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       INTEGER NOT NULL,
    file_name      TEXT NOT NULL,
    stored_name    TEXT NOT NULL,
    size           INTEGER NOT NULL DEFAULT 0,
    sha256         TEXT,
    note           TEXT,
    uploaded_by    INTEGER,
    uploaded_by_name TEXT,
    uploaded_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_of_order ON order_files(order_id);

-- 打样单（RP / 手样）。可在一单里多次发起，不占用主流程。
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
    ("source", "TEXT NOT NULL DEFAULT 'own'"),
    ("color_req", "TEXT"), ("customer_part_no", "TEXT"),
    ("customer_ok_at", "TEXT"), ("customer_ok_note", "TEXT"),
    ("client_ref_done", "INTEGER NOT NULL DEFAULT 0"),
    ("color_std", "TEXT"),
    ("shrink_ok", "INTEGER NOT NULL DEFAULT 0"),
    ("fit_ok", "INTEGER NOT NULL DEFAULT 0"),
    ("t0_ok_at", "TEXT"),
    ("finishing_kind", "TEXT"),
    ("finishing_ok_at", "TEXT"),
    ("assy_ok", "INTEGER NOT NULL DEFAULT 0"),
    ("mold_no_done", "INTEGER NOT NULL DEFAULT 0"),
    ("po_no", "TEXT"), ("po_date", "TEXT"),
    ("t0_done", "INTEGER NOT NULL DEFAULT 0"),
]


def migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
    for name, decl in NEW_ORDER_COLS:
        if name not in have:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {decl}")
    # 账号也允许改（调岗、改角色），留一个更新时间
    uhave = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "updated_at" not in uhave:
        conn.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")


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

    for n, c, note in [("厦门澳捷", "吴经理 138****6621", "眼镜客户，运动镜系列，走月结"),
                       ("东莞手办工坊", "刘工 137****8890", "手办客户，来图代工")]:
        conn.execute("INSERT INTO customers(name,contact,note,created_at) VALUES(?,?,?,?)", (n, c, note, ts))

    def add_order(code, cust, prod, req, due, stage_no, note="", src=DEFAULT_SOURCE):
        return conn.execute(
            """INSERT INTO orders(code,source,customer_name,product_name,sales_name,requirements,due_date,
                                  current_stage,status,bom_rev,note,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,'active',0,?,2,?,?)""",
            (code, src, cust, prod, "李强", req, due, stage_no, note, day, ts),
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

    # ============ 需求 1：走到第 4 环（工程部手上）
    o1 = add_order("DD-2026-0113", "厦门澳捷", "运动眼镜前框 BL2027-61",
                   "PA12 前框，A 面哑光；铰链埋射，两侧封胶位不少于 0.5mm；配 61/61A 两种圈型。",
                   "2026-12-20", 4, "客户交期较紧，先出前框本体，鼻托与脚套后续补充。")

    d1 = add_doc(o1, "UG-2601-0113-01", "M-2601-A", "前框本体", "3D图", "BL2027-61, 前框", "客户料号：BL2027-61")
    v1 = add_ver(d1, 1, "前框本体_v1.prt", "首版，按客户提供的 IGS 建模。", "obsolete")
    v2 = add_ver(d1, 2, "前框本体_v2.prt", "按 BG-0001 铰链封胶位加深 0.2mm。", "published", chg=1)
    conn.execute("UPDATE documents SET current_version_id=? WHERE id=?", (v2, d1))

    d2 = add_doc(o1, "UG-2601-0113-02", "M-2601-B", "前框镜片沟槽图", "2D图纸", "镜片槽, 61/61A")
    v3 = add_ver(d2, 1, "前框沟槽_v1.dwg", "首版 2D 尺寸图，含镜片沟槽与装片间隙。", "published")
    conn.execute("UPDATE documents SET current_version_id=? WHERE id=?", (v3, d2))

    d3 = add_doc(o1, "UG-2601-0113-05", "M-2601-E", "铰链埋射镶件", "3D图", "镶件", "模具部要求单独制作埋射镶件。")
    v4 = add_ver(d3, 1, "埋射镶件_v1.prt", "首版，待审批。", "draft")

    add_handover(o1, 1, 2, "accepted", "圈型已获客户确认，转结构设计。", day)
    add_handover(o1, 2, 3, "accepted", "结构 3D 已完成，转工程部确定开模编号。", day)
    add_handover(o1, 3, 4, "accepted", "开模图与 BOM 初版一并提交，请核对埋射镶件。", ts,
                 items=[(d1, v2, "UG-2601-0113-01", "M-2601-A", "前框本体", "3D图", 2, "前框本体_v2.prt"),
                        (d2, v3, "UG-2601-0113-02", "M-2601-B", "前框沟槽图", "2D图纸", 1, "前框沟槽_v1.dwg")])

    # BOM：三层，其中一行故意黄灯
    bom_rows = [
        (None, 0, "SP-2601-00", "前框组件", "PA12 哑光", 1, "套", "装配", None, ""),
        (None, 0, "UG-2601-0113-01", "前框本体", "PA12", 1, "件", "自制", d1, ""),
        (None, 0, "UG-2601-0113-02", "镜片沟槽镶件", "PC 透明", 1, "件", "自制", d2, ""),
        (None, 0, "GB/T 818 M1.4×3", "铰链螺钉", "不锈钢", 2, "件", "标准件", None, ""),
        (None, 0, "UG-2601-0113-05", "铰链埋射镶件", "S136 淬火", 2, "件", "自制", None, "图档尚未关联，待模具部确认封胶位。"),
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
                 items=[(d1, v2, "UG-2601-0113-01", "M-2601-A", "前框本体", "3D图", 2, "前框本体_v2.prt")],
                 bom=(3, [{"no": "1", "part_no": "SP-2601-00", "part_name": "前框组件", "spec": "PA12 哑光",
                           "qty": 1, "unit": "套", "source": "装配", "doc": "", "lamp": "ok"},
                          {"no": "1.1", "part_no": "UG-2601-0113-01", "part_name": "前框本体", "spec": "PA12",
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

    # ============ 需求 2：刚接单（客案：客户来图）
    o2 = add_order("DD-2026-0125", "东莞手办工坊", "手办人物主体（客案）",
                   "客户已提供完整 3D 与分件图，我司只做分件、缩水补偿与出图，小批量试产。",
                   "2026-12-05", 1, src="client")
    conn.execute("UPDATE orders SET client_ref_done=1 WHERE id=?", (o2,))

    # ============ 需求 3：结构手上（你有活干，自主开发）
    o3 = add_order("DD-2026-0131", "LKM 运动眼镜", "运动镜圈型 LKM-G8A",
                   "圈型改中性款，A 面咬花；镜腿铰链需兼容 G8 系列通用件。", "2027-01-15", 3,
                   src="own")
    d4 = add_doc(o3, "UG-2601-0131-01", None, "镜圈主体", "3D图", "G8-A 圈型")
    v5 = add_ver(d4, 1, "镜圈主体_v1.prt", "首版结构，待客户确认圈型与纹路。", "draft")
    add_handover(o3, 1, 2, "accepted", "需求已明确，转外观设计。", day)
    add_handover(o3, 2, 3, "accepted", "外观方案已定稿（咬花 PT-1102），转结构设计。", ts)

    # ============ 需求 4 / 5：给总看板看「卡住」和「快完」两种状态
    def ago(n: int) -> str:
        return (datetime.now(TZ8) - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")

    o4 = add_order("DD-2026-0108", "厦门澳捷", "运动眼镜镜腿（含包胶）",
                   "PA66+30GF 镜腿，末端包 TPE 软胶；喷哑光黑，铰链处埋射。", "2026-12-10", 6,
                   "包胶试模出来有飞边，素材要先喷漆再包胶，等后工艺确认工艺顺序。")
    d5 = add_doc(o4, "UG-2601-0108-01", "M-2512-C", "镜腿本体", "3D图", "镜腿, AJ-26205")
    v6 = add_ver(d5, 1, "镜腿本体_v1.prt", "首版，铰链封胶位按工程部意见改过一次。", "published")
    conn.execute("UPDATE documents SET current_version_id=? WHERE id=?", (v6, d5))
    for i, (f, t) in enumerate([(1, 2), (2, 3), (3, 4), (4, 5), (5, 6)]):
        add_handover(o4, f, t, "accepted", "按流程转下一环节。", ago(18 - i * 2))
    conn.execute("UPDATE orders SET created_at=?, updated_at=? WHERE id=?", (ago(22), ago(14), o4))

    o5 = add_order("DD-2026-0119", "厦门澳捷", "运动镜镜腿 AJ-26270（已量产）",
                   "TR90 镜腿，末端包 TPE 软胶；喷哑光黑，铰链埋射。", "2026-11-30", 8)
    d6 = add_doc(o5, "UG-2601-0119-01", "M-2511-B", "镜腿本体", "3D图", "镜腿, AJ-26270")
    v7 = add_ver(d6, 1, "镜腿本体_v1.prt", "定版，已量产。", "published")
    conn.execute("UPDATE documents SET current_version_id=? WHERE id=?", (v7, d6))
    for i, (f, t) in enumerate([(1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8)]):
        add_handover(o5, f, t, "accepted", "按流程转下一环节。", ago(15 - i * 2))
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

    # 第 6 环的需求补开模信息表与装配图（否则演示数据自身过不了第 4 环门槛）
    for dtype, title, fname in [("开模信息", "镜腿开模信息表", "AJ-26205 开模信息.xlsx"),
                                ("装配图", "镜腿装配图", "镜腿装配图.dwg")]:
        d = add_doc(o4, f"UG-2601-0108-{'09' if dtype == '开模信息' else '10'}", "M-2512-C",
                    title, dtype, "")
        v = add_ver(d, 1, fname, "开模前定版。", "published")
        conn.execute("UPDATE documents SET current_version_id=? WHERE id=?", (v, d))

    # 补充行业字段：色号、缩水、配合、T0 确认、后工艺 —— 让演示数据自己过得去门
    conn.execute("""UPDATE orders SET color_std='PANTONE 419C / 客户色板 SP-30008',
                    shrink_ok=1, fit_ok=1 WHERE id=?""", (o1,))
    conn.execute("""UPDATE orders SET color_std='PANTONE 419C',
                    shrink_ok=1, fit_ok=1, t0_done=1, t0_ok_at='2026-11-05 14:00',
                    finishing_kind='paint' WHERE id=?""", (o4,))
    conn.execute("""UPDATE orders SET color_std='PANTONE 419C', shrink_ok=1, fit_ok=1,
                    t0_done=1, t0_ok_at='2026-10-28 10:00',
                    finishing_kind='paint', finishing_ok_at='2026-11-02 09:30',
                    assy_ok=1 WHERE id=?""", (o5,))

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

    for act, detail, oid in [        ("创建需求", "DD-2026-0113 电饭煲上盖注塑模", o1),
        ("上传版本", "UG-2601-0113-01 上盖本体 v1", o1),
        ("审批通过", "交接 01→02 外观设计 → 结构设计", o1),
        ("上传版本", "UG-2601-0113-01 上盖本体 v2", o1),
        ("提交交接", "交接 04→05 开模准备 → 开模", o1),
        ("创建需求", "DD-2026-0131 咖啡机外壳", o3),
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
