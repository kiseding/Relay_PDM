"""Relay PDM —— Web 主程序（FastAPI + SQLite）"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = db.DATA_DIR
FILES_DIR = db.FILES_DIR

app = FastAPI(title="Relay PDM", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------------- 会话签名

_SECRET_FILE = DATA_DIR / "secret.txt"


def _secret() -> bytes:
    if not _SECRET_FILE.exists():
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
    return _SECRET_FILE.read_text(encoding="utf-8").strip().encode()


def make_token(uid: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"u": uid, "e": int(time.time()) + 86400 * 30}).encode()).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def read_token(tok: str | None) -> Optional[int]:
    if not tok or "." not in tok:
        return None
    body, sig = tok.rsplit(".", 1)
    if not hmac.compare_digest(hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32], sig):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception:
        return None
    if data.get("e", 0) < time.time():
        return None
    return int(data.get("u"))


def current_user(request: Request):
    uid = read_token(request.cookies.get("plm_s"))
    if not uid:
        return None
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------- 小工具

def go(url: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if ok:
        url = f"{url}{sep}ok={quote(ok)}"
    elif err:
        url = f"{url}{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)


def log(conn, me, action, target_type, target_id, order_id, detail):
    conn.execute(
        "INSERT INTO audit_logs(actor_id,actor_name,action,target_type,target_id,order_id,detail,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (me["id"], me["name"], action, target_type, target_id, order_id, detail, db.now_str()),
    )


# ---------------------------------------------------------------- BOM 计算

def bom_view(conn, order_id: int):
    """返回带序号、层级、图档状态、灯的 BOM 行"""
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM bom_items WHERE order_id=? ORDER BY seq, id", (order_id,)).fetchall()]
    ids = {it["id"] for it in items}
    children: dict = {}
    for it in items:
        pid = it["parent_id"] if it["parent_id"] in ids else None
        children.setdefault(pid, []).append(it)

    doc_map, ver_map = {}, {}
    for it in items:
        did = it["document_id"]
        if did and did not in doc_map:
            doc_map[did] = conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
            doc_map[did] = dict(doc_map[did]) if doc_map[did] else None
            dv = (doc_map[did] or {}).get("current_version_id")
            ver_map[did] = dict(conn.execute("SELECT * FROM doc_versions WHERE id=?", (dv,)).fetchone()) if dv else None

    out = []

    def walk(pid, prefix, depth):
        for i, it in enumerate(children.get(pid, []), start=1):
            no = f"{prefix}{i}" if not prefix else f"{prefix}{i}"
            doc = doc_map.get(it["document_id"]) if it["document_id"] else None
            ver = ver_map.get(it["document_id"]) if it["document_id"] else None
            if it["source"] in ("外购", "标准件"):
                lamp, lamp_txt = "ok", "无需图档"
            elif it["source"] == "装配":
                kids = children.get(it["id"], [])
                lamp, lamp_txt = ("ok", f"{len(kids)} 个子件") if kids else ("warn", "空装配")
            elif not it["document_id"]:
                lamp, lamp_txt = "warn", "未挂图档"
            elif not doc:
                lamp, lamp_txt = "bad", "图档已删除"
            elif not ver:
                lamp, lamp_txt = "bad", "没有当前版本"
            elif ver["status"] != "published":
                lamp, lamp_txt = "bad", f"v{ver['version_no']} {db.VER_STATUS.get(ver['status'], '')}"
            else:
                lamp, lamp_txt = "ok", f"v{ver['version_no']} 已发布"
            row = dict(it)
            row.update({"no": no, "depth": depth, "lamp": lamp, "lamp_txt": lamp_txt,
                        "doc": doc, "ver": ver})
            out.append(row)
            walk(it["id"], no + ".", depth + 1)

    walk(None, "", 0)
    return out


def bom_payload(rows) -> list:
    return [{"no": r["no"], "part_no": r["part_no"] or "", "part_name": r["part_name"],
             "spec": r["spec"] or "", "qty": r["qty"], "unit": r["unit"],
             "source": r["source"], "doc": r["lamp_txt"], "lamp": r["lamp"]} for r in rows]


# ---------------------------------------------------------------- 模板上下文

db.init_db()


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.url.path
    open_paths = ("/login", "/static", "/favicon.ico")
    if not path.startswith(open_paths):
        if not current_user(request):
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)


def nav_counts(me) -> dict:
    if not me:
        return {"handover": 0, "changes": 0, "rp": 0}
    conn = db.connect()
    try:
        if me["role"] in ("manager", "admin"):
            n_ho = conn.execute("SELECT COUNT(*) c FROM handovers WHERE status='pending'").fetchone()["c"]
        else:
            n_ho = conn.execute("SELECT COUNT(*) c FROM handovers WHERE status='approved'").fetchone()["c"]
        n_ch = conn.execute("SELECT COUNT(*) c FROM changes WHERE status='open'").fetchone()["c"]
        n_rp = conn.execute(
            "SELECT COUNT(*) c FROM rp_requests WHERE status IN ('open','making')").fetchone()["c"]
        return {"handover": n_ho, "changes": n_ch, "rp": n_rp}
    finally:
        conn.close()


def render(request: Request, name: str, **ctx):
    me = current_user(request)
    base = {
        "request": request,
        "me": me,
        "path": request.url.path,
        "ok": request.query_params.get("ok"),
        "err": request.query_params.get("err"),
        "DEPTS": db.DEPTS,
        "STAGES": db.STAGES,
        "stage": db.stage,
        "dept_name": db.dept_name,
        "HO_STATUS": db.HANDOVER_STATUS,
        "VER_STATUS": db.VER_STATUS,
        "CHANGE_KIND": db.CHANGE_KIND,
        "CHANGE_STATUS": db.CHANGE_STATUS,
        "RP_STATUS": db.RP_STATUS,
        "STAGE_GATES": db.STAGE_GATES,
        "DOC_TYPES": db.DOC_TYPES,
        "BOM_SOURCES": db.BOM_SOURCES,
        "BOM_UNITS": db.BOM_UNITS,
        "ROLE_NAMES": db.ROLE_NAMES,
        "nav_counts": nav_counts(me),
    }
    base.update(ctx)
    return templates.TemplateResponse(request, name, base)


# ---------------------------------------------------------------- 登录

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, err: str | None = None):
    return render(request, "login.html", err=err)


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    conn = db.connect()
    try:
        u = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (username.strip(),)).fetchone()
        if not u or not db.check_pwd(password, u["pwd_hash"]):
            return go("/login", err="账号或密码错误")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("plm_s", make_token(u["id"]), httponly=True, max_age=86400 * 30, samesite="lax")
        return resp
    finally:
        conn.close()


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("plm_s")
    return resp


# ---------------------------------------------------------------- 工作台

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    me = current_user(request)
    conn = db.connect()
    try:
        is_mgr = me["role"] in ("manager", "admin")
        rows = conn.execute("SELECT * FROM orders WHERE status='active' ORDER BY updated_at DESC").fetchall()
        todo_submit, todo_accept = [], []
        for r in rows:
            st = db.stage(r["current_stage"])
            ho = conn.execute(
                "SELECT * FROM handovers WHERE order_id=? AND status IN ('pending','approved','rejected')"
                " ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            d = dict(r)
            d["stage_info"] = st
            d["ho"] = dict(ho) if ho else None
            # 第 7 环是最后一环，没有下一环节可交，不进待办
            if not ho and st["no"] < 7 and (st["dept"] == me["dept"] or me["role"] == "admin"):
                todo_submit.append(d)
            if ho and ho["status"] == "approved" and (db.stage(ho["to_stage"])["dept"] == me["dept"] or is_mgr):
                todo_accept.append(d)

        todo_approve = [dict(r) for r in conn.execute(
            "SELECT h.*, o.code AS order_code, o.product_name FROM handovers h JOIN orders o ON o.id=h.order_id "
            "WHERE h.status='pending' ORDER BY h.id DESC").fetchall()] if is_mgr else []

        stats = {
            "orders": conn.execute("SELECT COUNT(*) c FROM orders WHERE status='active'").fetchone()["c"],
            "docs": conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"],
            "pending": conn.execute("SELECT COUNT(*) c FROM handovers WHERE status='pending'").fetchone()["c"],
            "draft": conn.execute("SELECT COUNT(*) c FROM doc_versions WHERE status='draft'").fetchone()["c"],
        }
        recent = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT 12").fetchall()]
        return render(request, "dashboard.html", stats=stats, todo_submit=todo_submit,
                      todo_accept=todo_accept, todo_approve=todo_approve, recent=recent)
    finally:
        conn.close()


# ---------------------------------------------------------------- 总看板（admin / 主管）

def _parse_ts(s: str | None):
    try:
        return datetime.strptime(s or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=db.TZ8)
    except ValueError:
        return None


@app.get("/board", response_class=HTMLResponse)
def board(request: Request):
    me = current_user(request)
    if me["role"] not in ("admin", "manager"):
        return go("/", err="总看板仅对管理员与主管开放")
    conn = db.connect()
    try:
        now = datetime.now(db.TZ8)
        cards = []
        for r in conn.execute("SELECT * FROM orders WHERE status='active' ORDER BY id").fetchall():
            o = dict(r)
            st = db.stage(o["current_stage"])
            # 到达本环节的时间 = 最近一次被签收的时间；一次没交接过就按建单时间算
            arrived = _parse_ts(o["created_at"])
            last = conn.execute(
                "SELECT * FROM handovers WHERE order_id=? AND status='accepted' "
                "ORDER BY id DESC LIMIT 1", (o["id"],)).fetchone()
            if last:
                arrived = _parse_ts(last["received_at"]) or arrived
            days = max(0, (now - arrived).days) if arrived else 0

            ho = conn.execute(
                "SELECT * FROM handovers WHERE order_id=? AND status IN "
                "('pending','approved','rejected') ORDER BY id DESC LIMIT 1", (o["id"],)).fetchone()
            gmiss = [g for g in db.gate_check(conn, o, st["no"]) if not g["ok"]]
            if ho:
                flag, fcls = {
                    "pending":  ("待审批", "pend"),
                    "approved": ("待签收", "appr"),
                    "rejected": ("被驳回", "rej"),
                }[ho["status"]]
                # 卡住的不是部门，是具体的这一关
                blocker = {"pending": "待主管审批", "approved": "待下一环节签收",
                           "rejected": "待重新提交"}[ho["status"]]
            else:
                flag, fcls = ("量产中" if st["no"] == 7 else "待提交", "todo")
                blocker = f"{db.dept_name(st['dept'])}处理中"
                if gmiss:
                    blocker += f" · 齐套缺 {len(gmiss)} 项"

            items = bom_view(conn, o["id"])
            cards.append({
                "o": o, "st": st, "days": days,
                "level": "bad" if days >= 14 else ("warn" if days >= 7 else "ok"),
                "arrived": arrived.strftime("%m-%d") if arrived else "—",
                "flag": flag, "fcls": fcls,
                "ho_id": ho["id"] if ho else None,
                "holder": blocker,
                "gate": len(gmiss),
                "gate_names": [g["label"] for g in gmiss][:3],
                "docs": conn.execute("SELECT COUNT(*) c FROM documents WHERE order_id=?",
                                     (o["id"],)).fetchone()["c"],
                "ok": sum(1 for i in items if i["lamp"] == "ok"),
                "warn": sum(1 for i in items if i["lamp"] == "warn"),
                "bad": sum(1 for i in items if i["lamp"] == "bad"),
            })

        cols = []
        for s in db.STAGES:
            cs = sorted([c for c in cards if c["st"]["no"] == s["no"]], key=lambda c: -c["days"])
            cols.append({"info": s, "cards": cs, "n": len(cs),
                         "oldest": max([c["days"] for c in cs], default=0)})

        stats = {
            "active": len(cards),
            "stuck": sum(1 for c in cards if c["days"] >= 7),
            "pending": conn.execute("SELECT COUNT(*) c FROM handovers WHERE status='pending'").fetchone()["c"],
            "unconfirmed": conn.execute(
                "SELECT COUNT(*) c FROM doc_versions v JOIN documents d ON d.id=v.document_id "
                "JOIN orders o ON o.id=d.order_id "
                "WHERE v.status='published' AND v.customer_confirmed=0 AND o.status='active'"
            ).fetchone()["c"],
            "nodrawing": sum(c["warn"] for c in cards),
            "gate_blocked": sum(1 for c in cards if c["gate"]),
            "from_sales": len({c["o"]["customer_name"] for c in cards if c["o"]["customer_name"]}),
        }

        slow = sorted(cards, key=lambda c: -c["days"])[:5]

        unconfirmed = [dict(r) for r in conn.execute(
            """SELECT v.id, v.version_no, v.published_at, v.publish_note,
                      d.id AS doc_id, d.design_no, d.title, o.code AS order_code, o.id AS order_id
               FROM doc_versions v JOIN documents d ON d.id=v.document_id
               JOIN orders o ON o.id=d.order_id
               WHERE v.status='published' AND v.customer_confirmed=0 AND o.status='active'
               ORDER BY v.published_at DESC LIMIT 8""").fetchall()]

        return render(request, "board.html", cols=cols, stats=stats, slow=slow,
                      unconfirmed=unconfirmed, cards=cards)
    finally:
        conn.close()


# ---------------------------------------------------------------- 订单

@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, s: int | None = None):
    conn = db.connect()
    try:
        sql = "SELECT * FROM orders"
        args = []
        if s:
            sql += " WHERE current_stage=?"
            args.append(s)
        sql += " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC"
        rows = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            d["stage_info"] = db.stage(r["current_stage"])
            d["doc_count"] = conn.execute("SELECT COUNT(*) c FROM documents WHERE order_id=?", (r["id"],)).fetchone()["c"]
            d["bom_count"] = conn.execute("SELECT COUNT(*) c FROM bom_items WHERE order_id=?", (r["id"],)).fetchone()["c"]
            rows.append(d)
        return render(request, "orders.html", rows=rows, cur_s=s)
    finally:
        conn.close()


@app.post("/orders/new")
def order_new(request: Request, customer_name: str = Form(""), product_name: str = Form(...),
              requirements: str = Form(""), due_date: str = Form(""), note: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        ts = db.now_str()
        n = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] + 1
        code = f"DD-{ts[:4]}-{n:04d}"
        while conn.execute("SELECT 1 FROM orders WHERE code=?", (code,)).fetchone():
            n += 1
            code = f"DD-{ts[:4]}-{n:04d}"
        oid = conn.execute(
            """INSERT INTO orders(code,customer_name,product_name,sales_name,requirements,due_date,
                                  current_stage,status,bom_rev,note,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,1,'active',0,?,?,?,?)""",
            (code, customer_name, product_name, me["name"], requirements, due_date, note, me["id"], ts, ts),
        ).lastrowid
        if customer_name:
            conn.execute("INSERT OR IGNORE INTO customers(name,created_at) VALUES(?,?)", (customer_name, ts))
        log(conn, me, "创建订单", "order", oid, oid, f"{code} {product_name}")
        conn.commit()
        return go(f"/orders/{oid}", ok="订单已创建")
    finally:
        conn.close()


@app.get("/orders/{oid}", response_class=HTMLResponse)
def order_detail(request: Request, oid: int):
    me = current_user(request)
    conn = db.connect()
    try:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return go("/orders", err="未找到该订单")
        o = dict(o)
        docs = []
        for r in conn.execute("SELECT * FROM documents WHERE order_id=? ORDER BY id", (oid,)).fetchall():
            d = dict(r)
            d["ver"] = dict(conn.execute("SELECT * FROM doc_versions WHERE id=?", (r["current_version_id"],)).fetchone()) if r["current_version_id"] else None
            d["n_ver"] = conn.execute("SELECT COUNT(*) c FROM doc_versions WHERE document_id=?", (r["id"],)).fetchone()["c"]
            docs.append(d)
        handovers = [dict(r) for r in conn.execute(
            "SELECT * FROM handovers WHERE order_id=? ORDER BY id", (oid,)).fetchall()]
        changes = [dict(r) for r in conn.execute(
            "SELECT * FROM changes WHERE order_id=? ORDER BY id DESC", (oid,)).fetchall()]
        bom = bom_view(conn, oid) if conn.execute(
            "SELECT 1 FROM bom_items WHERE order_id=? LIMIT 1", (oid,)).fetchone() else []
        logs = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_logs WHERE order_id=? ORDER BY id DESC LIMIT 20", (oid,)).fetchall()]
        # 每个环节是否已完成、完成时间
        steps = []
        for st in db.STAGES:
            at = None
            for h in handovers:
                if h["status"] == "accepted" and h["to_stage"] == st["no"]:
                    at = h["received_at"]
            if o["status"] == "closed" or st["no"] < o["current_stage"]:
                state = "done"
            elif st["no"] == o["current_stage"]:
                state = "now"
            else:
                state = "pend"
            steps.append({"info": st, "state": state, "at": at})

        # 交接单要能勾选所有未作废版本
        for d in docs:
            d["versions"] = [dict(v) for v in conn.execute(
                "SELECT * FROM doc_versions WHERE document_id=? AND status!='obsolete' "
                "ORDER BY version_no DESC", (d["id"],)).fetchall()]
        lamps = {"ok": sum(1 for r in bom if r["lamp"] == "ok"),
                 "warn": sum(1 for r in bom if r["lamp"] == "warn"),
                 "bad": sum(1 for r in bom if r["lamp"] == "bad")}
        cur = db.stage(o["current_stage"])
        gate = db.gate_check(conn, o, cur["no"])
        gate_miss = [g for g in gate if not g["ok"]]
        rps = [dict(r) for r in conn.execute(
            """SELECT r.*, d.title AS doc_title, d.design_no, v.version_no FROM rp_requests r
               LEFT JOIN documents d ON d.id=r.document_id
               LEFT JOIN doc_versions v ON v.id=r.version_id
               WHERE r.order_id=? ORDER BY r.id DESC""", (oid,)).fetchall()]
        can_submit = (cur["dept"] == me["dept"] or me["role"] in ("manager", "admin")) and \
            o["status"] == "active" and cur["no"] < 7 and \
            not any(h["status"] in ("pending", "approved") for h in handovers)
        return render(request, "order_detail.html", o=o, docs=docs, handovers=handovers,
                      changes=changes, bom=bom, logs=logs, cur=cur, can_submit=can_submit,
                      steps=steps, lamps=lamps, gate=gate, gate_miss=gate_miss, rps=rps)
    finally:
        conn.close()


@app.post("/orders/{oid}/fill")
def order_fill(request: Request, oid: int,
               color_req: str = Form(""), customer_part_no: str = Form(""),
               customer_ok_at: str = Form(""), customer_ok_note: str = Form(""),
               po_no: str = Form(""), po_date: str = Form(""),
               mold_no_done: str = Form(""), t0_done: str = Form("")):
    """补录齐套清单里缺的项。空的输入不覆盖已有值。"""
    me = current_user(request)
    conn = db.connect()
    try:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return go("/orders", err="未找到该订单")
        sets, args, touched = [], [], []
        for k, v in [("color_req", color_req), ("customer_part_no", customer_part_no),
                     ("customer_ok_at", customer_ok_at), ("customer_ok_note", customer_ok_note),
                     ("po_no", po_no), ("po_date", po_date)]:
            if v.strip():
                sets.append(f"{k}=?")
                args.append(v.strip())
                touched.append(k)
        for k, v in [("mold_no_done", mold_no_done), ("t0_done", t0_done)]:
            if v == "on":
                sets.append(f"{k}=1")
                touched.append(k)
        if not sets:
            return go(f"/orders/{oid}", err="没有需要更新的内容")
        sets.append("updated_at=?")
        args.extend([db.now_str(), oid])
        conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", args)
        log(conn, me, "补录信息", "order", oid, oid, "、".join(touched))
        conn.commit()
        return go(f"/orders/{oid}", ok="已保存")
    finally:
        conn.close()


@app.post("/orders/{oid}/close")
def order_close(request: Request, oid: int):
    me = current_user(request)
    conn = db.connect()
    try:
        conn.execute("UPDATE orders SET status='closed', updated_at=? WHERE id=?", (db.now_str(), oid))
        log(conn, me, "结案", "order", oid, oid, "订单结案")
        conn.commit()
        return go(f"/orders/{oid}", ok="订单已结案")
    finally:
        conn.close()


# ---------------------------------------------------------------- 图档

@app.get("/docs", response_class=HTMLResponse)
def docs_page(request: Request, q: str = "", t: str = ""):
    conn = db.connect()
    try:
        sql = """SELECT d.*, o.code AS order_code FROM documents d
                 LEFT JOIN orders o ON o.id=d.order_id WHERE 1=1"""
        args = []
        if q:
            sql += " AND (d.design_no LIKE ? OR IFNULL(d.mold_no,'') LIKE ? OR d.title LIKE ? OR IFNULL(d.aliases,'') LIKE ?)"
            args += [f"%{q}%"] * 4
        if t:
            sql += " AND d.doc_type=?"
            args.append(t)
        sql += " ORDER BY d.updated_at DESC"
        rows = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            d["ver"] = dict(conn.execute("SELECT * FROM doc_versions WHERE id=?", (r["current_version_id"],)).fetchone()) if r["current_version_id"] else None
            d["n_ver"] = conn.execute("SELECT COUNT(*) c FROM doc_versions WHERE document_id=?", (r["id"],)).fetchone()["c"]
            rows.append(d)
        orders_list = [dict(r) for r in conn.execute(
            "SELECT id, code, product_name FROM orders WHERE status='active' ORDER BY id DESC").fetchall()]
        return render(request, "docs.html", rows=rows, q=q, t=t, orders_list=orders_list)
    finally:
        conn.close()


@app.post("/docs/new")
def doc_new(request: Request, order_id: int = Form(...), design_no: str = Form(...),
            title: str = Form(...), doc_type: str = Form("3D图"), mold_no: str = Form(""),
            aliases: str = Form(""), note: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        design_no = design_no.strip()
        if conn.execute("SELECT 1 FROM documents WHERE design_no=?", (design_no,)).fetchone():
            return go(f"/orders/{order_id}", err=f"设计编号 {design_no} 已被使用")
        ts = db.now_str()
        did = conn.execute(
            """INSERT INTO documents(order_id,design_no,mold_no,title,doc_type,aliases,note,
                                     created_by,created_by_name,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, design_no, mold_no.strip(), title, doc_type, aliases, note, me["id"], me["name"], ts, ts),
        ).lastrowid
        log(conn, me, "建图档", "document", did, order_id, f"{design_no} {title}")
        conn.commit()
        return go(f"/docs/{did}", ok="图档已创建，请上传首版")
    finally:
        conn.close()


@app.get("/docs/{did}", response_class=HTMLResponse)
def doc_detail(request: Request, did: int):
    conn = db.connect()
    try:
        d = conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
        if not d:
            return go("/docs", err="未找到该图档")
        d = dict(d)
        o = conn.execute("SELECT * FROM orders WHERE id=?", (d["order_id"],)).fetchone()
        vers = [dict(r) for r in conn.execute(
            "SELECT * FROM doc_versions WHERE document_id=? ORDER BY version_no DESC", (did,)).fetchall()]
        changes = [dict(r) for r in conn.execute(
            "SELECT * FROM changes WHERE document_id=? ORDER BY id DESC", (did,)).fetchall()]
        in_ho = [dict(r) for r in conn.execute(
            "SELECT h.code, h.id, h.status, h.received_at FROM handover_items i "
            "JOIN handovers h ON h.id=i.handover_id WHERE i.document_id=? ORDER BY h.id DESC", (did,)).fetchall()]
        return render(request, "doc_detail.html", d=d, o=dict(o) if o else None, vers=vers,
                      changes=changes, in_ho=in_ho)
    finally:
        conn.close()


@app.post("/docs/{did}/update")
def doc_update(request: Request, did: int, mold_no: str = Form(""), aliases: str = Form(""),
               title: str = Form(""), doc_type: str = Form(""), note: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        d = conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
        if not d:
            return go("/docs", err="找不到这张图档")
        if me["dept"] != "engineering" and me["role"] not in ("manager", "admin"):
            return go(f"/docs/{did}", err="开模编号由工程部维护，当前账号无修改权限")
        conn.execute(
            "UPDATE documents SET mold_no=?, aliases=?, title=?, doc_type=?, note=?, updated_at=? WHERE id=?",
            (mold_no.strip(), aliases, title or d["title"], doc_type or d["doc_type"], note,
             db.now_str(), did))
        log(conn, me, "改图档信息", "document", did, d["order_id"], f"{d['design_no']} 开模编号={mold_no}")
        conn.commit()
        return go(f"/docs/{did}", ok="已保存")
    finally:
        conn.close()


@app.post("/docs/{did}/upload")
async def doc_upload(request: Request, did: int, file: UploadFile = File(...),
                     change_note: str = Form(""), change_id: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        d = conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
        if not d:
            return go("/docs", err="找不到这张图档")
        next_no = (conn.execute("SELECT MAX(version_no) m FROM doc_versions WHERE document_id=?",
                                (did,)).fetchone()["m"] or 0) + 1
        raw = await file.read()
        stored = f"{secrets.token_hex(8)}_{Path(file.filename or 'file').name}"
        (FILES_DIR / stored).write_bytes(raw)
        vid = conn.execute(
            """INSERT INTO doc_versions(document_id,version_no,file_name,stored_name,size,sha256,
                                       change_note,change_id,status,uploaded_by,uploaded_by_name,uploaded_at)
               VALUES(?,?,?,?,?,?,?,?,'draft',?,?,?)""",
            (did, next_no, file.filename or stored, stored, len(raw),
             hashlib.sha256(raw).hexdigest(), change_note, int(change_id) if change_id else None,
             me["id"], me["name"], db.now_str()),
        ).lastrowid
        conn.execute("UPDATE documents SET updated_at=? WHERE id=?", (db.now_str(), did))
        log(conn, me, "上传版本", "document", did, d["order_id"],
            f"{d['design_no']} v{next_no} {file.filename}")
        conn.commit()
        return go(f"/docs/{did}", ok=f"v{next_no} 已上传，当前为草稿状态，经交接单审批后转为正式版本")
    finally:
        conn.close()


@app.post("/versions/{vid}/customer")
def ver_customer(request: Request, vid: int, note: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        v = conn.execute("SELECT * FROM doc_versions WHERE id=?", (vid,)).fetchone()
        if not v:
            return go("/docs", err="未找到该版本")
        d = conn.execute("SELECT * FROM documents WHERE id=?", (v["document_id"],)).fetchone()
        if me["dept"] not in ("sales", "manage") and me["role"] != "admin":
            return go(f"/docs/{d['id']}", err="客户确认由业务部门登记，当前账号无操作权限")
        conn.execute(
            "UPDATE doc_versions SET customer_confirmed=1, customer_confirm_note=?, "
            "customer_confirmed_at=?, customer_confirmed_by=? WHERE id=?",
            (note, db.now_str(), me["name"], vid))
        log(conn, me, "标记客户确认", "version", vid, d["order_id"],
            f"{d['design_no']} v{v['version_no']}：{note}")
        conn.commit()
        return go(f"/docs/{d['id']}", ok="客户确认已登记")
    finally:
        conn.close()


@app.get("/files/{stored}")
def get_file(request: Request, stored: str):
    p = FILES_DIR / stored
    if not p.exists():
        return go("/", err="文件不存在")
    name = stored
    conn = db.connect()
    try:
        r = conn.execute("SELECT file_name FROM doc_versions WHERE stored_name=?", (stored,)).fetchone()
        if r:
            name = r["file_name"]
    finally:
        conn.close()
    return FileResponse(p, filename=name)


# ---------------------------------------------------------------- 交接

@app.get("/handovers", response_class=HTMLResponse)
def handovers_page(request: Request, tab: str = "all"):
    me = current_user(request)
    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            """SELECT h.*, o.code AS order_code, o.product_name FROM handovers h
               JOIN orders o ON o.id=h.order_id ORDER BY h.id DESC""").fetchall()]
        for r in rows:
            r["from_info"] = db.stage(r["from_stage"])
            r["to_info"] = db.stage(r["to_stage"])
            r["n_items"] = conn.execute("SELECT COUNT(*) c FROM handover_items WHERE handover_id=?", (r["id"],)).fetchone()["c"]
        if tab == "todo":
            rows = [r for r in rows if
                    (r["status"] == "pending" and me["role"] in ("manager", "admin")) or
                    (r["status"] == "approved" and (r["to_info"]["dept"] == me["dept"] or me["role"] in ("manager", "admin")))]
        elif tab == "mine":
            rows = [r for r in rows if r["submitter_id"] == me["id"]]
        return render(request, "handovers.html", rows=rows, tab=tab)
    finally:
        conn.close()


@app.post("/handovers/new")
async def handover_new(request: Request):
    me = current_user(request)
    form = await request.form()
    oid = int(form.get("order_id") or 0)
    note = (form.get("note") or "").strip()
    ver_ids = [int(v) for v in form.getlist("ver_ids") if str(v).isdigit()]
    include_bom = form.get("include_bom") == "on"
    conn = db.connect()
    try:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return go("/orders", err="未找到该订单")
        cur = db.stage(o["current_stage"])
        if cur["dept"] != me["dept"] and me["role"] not in ("manager", "admin"):
            return go(f"/orders/{oid}", err=f"第 {cur['no']} 环由{db.dept_name(cur['dept'])}负责")
        if conn.execute("SELECT 1 FROM handovers WHERE order_id=? AND status IN ('pending','approved')",
                        (oid,)).fetchone():
            return go(f"/orders/{oid}", err="该订单已有进行中的交接单，请处理完成后再提交")
        if not note:
            return go(f"/orders/{oid}", err="请填写本环节完成情况说明")
        # 齐套校验：缺任何一项都不允许转出，避免把缺口推给下游
        miss = [g for g in db.gate_check(conn, dict(o), cur["no"]) if not g["ok"]]
        if miss:
            names = "、".join(g["label"] for g in miss)
            return go(f"/orders/{oid}", err=f"本环节还缺 {len(miss)} 项，无法转出：{names}")
        rnd = (conn.execute("SELECT MAX(round) m FROM handovers WHERE order_id=? AND from_stage=?",
                            (oid, cur["no"])).fetchone()["m"] or 0) + 1
        ts = db.now_str()
        hid = conn.execute(
            """INSERT INTO handovers(order_id,from_stage,to_stage,note,status,round,submitter_id,
                                     submitter_name,submitter_dept,submitted_at)
               VALUES(?,?,?,?,'pending',?,?,?,?,?)""",
            (oid, cur["no"], cur["no"] + 1, note, rnd, me["id"], me["name"], me["dept"], ts),
        ).lastrowid
        conn.execute("UPDATE handovers SET code=? WHERE id=?", (f"JJ-{hid:04d}", hid))
        for vid in ver_ids:
            v = conn.execute("SELECT * FROM doc_versions WHERE id=?", (vid,)).fetchone()
            if not v:
                continue
            d = conn.execute("SELECT * FROM documents WHERE id=?", (v["document_id"],)).fetchone()
            conn.execute(
                """INSERT INTO handover_items(handover_id,document_id,version_id,design_no,mold_no,
                                              doc_title,doc_type,version_no,file_name)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (hid, d["id"], v["id"], d["design_no"], d["mold_no"], d["title"], d["doc_type"],
                 v["version_no"], v["file_name"]))
        if include_bom:
            rows = bom_view(conn, oid)
            conn.execute(
                "INSERT INTO handover_boms(handover_id,rev,payload,item_count,created_at) VALUES(?,?,?,?,?)",
                (hid, o["bom_rev"], json.dumps(bom_payload(rows), ensure_ascii=False), len(rows), ts))
        conn.execute("UPDATE orders SET updated_at=? WHERE id=?", (ts, oid))
        log(conn, me, "提交交接", "handover", hid, oid,
            f"{cur['no']:02d}→{cur['no']+1:02d} {cur['name']} → {db.stage(cur['no']+1)['name']}")
        conn.commit()
        return go(f"/handovers/{hid}", ok="已提交，等待主管审批")
    finally:
        conn.close()


@app.get("/handovers/{hid}", response_class=HTMLResponse)
def handover_detail(request: Request, hid: int):
    me = current_user(request)
    conn = db.connect()
    try:
        h = conn.execute("SELECT * FROM handovers WHERE id=?", (hid,)).fetchone()
        if not h:
            return go("/handovers", err="未找到该交接单")
        h = dict(h)
        o = dict(conn.execute("SELECT * FROM orders WHERE id=?", (h["order_id"],)).fetchone())
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM handover_items WHERE handover_id=? ORDER BY id", (hid,)).fetchall()]
        bomsnap = conn.execute("SELECT * FROM handover_boms WHERE handover_id=?", (hid,)).fetchone()
        bom = json.loads(bomsnap["payload"]) if bomsnap else []
        from_info, to_info = db.stage(h["from_stage"]), db.stage(h["to_stage"])
        is_mgr = me["role"] in ("manager", "admin")
        can_approve = h["status"] == "pending" and is_mgr
        can_accept = h["status"] == "approved" and (to_info["dept"] == me["dept"] or me["role"] == "admin")
        return render(request, "handover_detail.html", h=h, o=o, items=items, bom=bom,
                      bom_rev=bomsnap["rev"] if bomsnap else None,
                      from_info=from_info, to_info=to_info, is_mgr=is_mgr,
                      can_approve=can_approve, can_accept=can_accept)
    finally:
        conn.close()


@app.post("/handovers/{hid}/approve")
def handover_approve(request: Request, hid: int, note: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        h = conn.execute("SELECT * FROM handovers WHERE id=?", (hid,)).fetchone()
        if not h or h["status"] != "pending":
            return go(f"/handovers/{hid}", err="该交接单当前状态不可审批")
        if me["role"] not in ("manager", "admin"):
            return go(f"/handovers/{hid}", err="仅主管可执行审批")
        ts = db.now_str()
        conn.execute("UPDATE handovers SET status='approved', approver_name=?, approved_at=?, approve_note=? WHERE id=?",
                     (me["name"], ts, note, hid))
        # 审批通过 → 单上的版本转「已发布」，成为该图档的当前版本
        for it in conn.execute("SELECT * FROM handover_items WHERE handover_id=?", (hid,)).fetchall():
            if not it["version_id"]:
                continue
            conn.execute("UPDATE doc_versions SET status='obsolete' WHERE document_id=? AND status='published'",
                         (it["document_id"],))
            conn.execute("UPDATE doc_versions SET status='published', published_by_name=?, published_at=?, publish_note=? WHERE id=?",
                         (me["name"], ts, note or "审批通过", it["version_id"]))
            conn.execute("UPDATE documents SET current_version_id=?, updated_at=? WHERE id=?",
                         (it["version_id"], ts, it["document_id"]))
        log(conn, me, "审批通过", "handover", hid, h["order_id"],
            f"{db.stage(h['from_stage'])['name']} → {db.stage(h['to_stage'])['name']}")
        conn.commit()
        return go(f"/handovers/{hid}", ok="审批通过，等待下一环节签收")
    finally:
        conn.close()


@app.post("/handovers/{hid}/reject")
def handover_reject(request: Request, hid: int, note: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        h = conn.execute("SELECT * FROM handovers WHERE id=?", (hid,)).fetchone()
        if not h or h["status"] not in ("pending", "approved"):
            return go(f"/handovers/{hid}", err="该交接单当前状态不可退回")
        if me["role"] not in ("manager", "admin"):
            return go(f"/handovers/{hid}", err="仅主管可执行退回")
        conn.execute("UPDATE handovers SET status='rejected', approver_name=?, approved_at=?, approve_note=? WHERE id=?",
                     (me["name"], db.now_str(), note or "退回修改", hid))
        log(conn, me, "驳回交接", "handover", hid, h["order_id"], note or "退回重做")
        conn.commit()
        return go(f"/handovers/{hid}", err="已退回，图档仍为草稿状态，修改后请重新提交")
    finally:
        conn.close()


@app.post("/handovers/{hid}/accept")
def handover_accept(request: Request, hid: int, note: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        h = conn.execute("SELECT * FROM handovers WHERE id=?", (hid,)).fetchone()
        if not h or h["status"] != "approved":
            return go(f"/handovers/{hid}", err="该交接单尚未审批，无法签收")
        to_info = db.stage(h["to_stage"])
        if to_info["dept"] != me["dept"] and me["role"] != "admin":
            return go(f"/handovers/{hid}", err=f"签收由{db.dept_name(to_info['dept'])}执行，当前账号无操作权限")
        ts = db.now_str()
        conn.execute("UPDATE handovers SET status='accepted', receiver_name=?, received_at=?, receive_note=? WHERE id=?",
                     (me["name"], ts, note, hid))
        conn.execute("UPDATE orders SET current_stage=?, updated_at=? WHERE id=?",
                     (h["to_stage"], ts, h["order_id"]))
        log(conn, me, "签收交接", "handover", hid, h["order_id"],
            f"第 {h['to_stage']} 环 {to_info['name']} 开始")
        conn.commit()
        return go(f"/orders/{h['order_id']}", ok=f"已签收，订单进入第 {h['to_stage']} 环节")
    finally:
        conn.close()


# ---------------------------------------------------------------- RP 打样单

@app.get("/rp", response_class=HTMLResponse)
def rp_list(request: Request, tab: str = "open"):
    conn = db.connect()
    try:
        rows = []
        for r in conn.execute(
            """SELECT r.*, o.code AS order_code, o.product_name, d.design_no, d.doc_type,
                      v.version_no, v.status AS ver_status
               FROM rp_requests r
               JOIN orders o ON o.id=r.order_id
               LEFT JOIN documents d ON d.id=r.document_id
               LEFT JOIN doc_versions v ON v.id=r.version_id
               ORDER BY CASE r.status WHEN 'open' THEN 0 WHEN 'making' THEN 1 ELSE 2 END,
                        r.id DESC""").fetchall():
            d = dict(r)
            d["stage_info"] = db.stage(
                conn.execute("SELECT current_stage FROM orders WHERE id=?", (r["order_id"],)).fetchone()[0])
            rows.append(d)
        if tab == "open":
            rows = [r for r in rows if r["status"] in ("open", "making")]
        orders = [dict(r) for r in conn.execute(
            "SELECT id, code, product_name, color_req FROM orders WHERE status='active' ORDER BY id DESC").fetchall()]
        # 打样必须挂到具体版本上，这里给出可选的版本清单
        vers = [dict(r) for r in conn.execute(
            """SELECT v.id, v.version_no, v.document_id, v.status, d.order_id, d.title, d.design_no
               FROM doc_versions v JOIN documents d ON d.id=v.document_id
               WHERE v.status IN ('published','draft')
               ORDER BY d.order_id DESC, d.id, v.version_no DESC""").fetchall()]
        return render(request, "rp.html", rows=rows, tab=tab, orders=orders, vers=vers)
    finally:
        conn.close()


@app.post("/rp/new")
def rp_new(request: Request, order_id: int = Form(...), version_id: int = Form(0),
           purpose: str = Form(""), color_req: str = Form(""), lens_spec: str = Form(""),
           qty: int = Form(1), due_date: str = Form(""), handler: str = Form(""),
           note: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            return go("/rp", err="未找到该订单")
        if not purpose.strip():
            return go("/rp", err="请写明这次打样要确认什么")
        if not color_req.strip():
            return go("/rp", err="做色要求必填：没有它打出来的样大概率要重做")
        if not version_id:
            return go("/rp", err="请选择打样依据的图档版本")
        v = conn.execute("SELECT * FROM doc_versions WHERE id=?", (version_id,)).fetchone()
        if not v:
            return go("/rp", err="所选版本不存在")
        if v["status"] == "obsolete":
            return go("/rp", err="不能按已作废版本打样")
        ts = db.now_str()
        # 沿用车间习惯的 090xxx 流水号，接在已有最大号之后
        mrow = conn.execute(
            "SELECT MAX(CAST(SUBSTR(code,4) AS INTEGER)) m FROM rp_requests WHERE code LIKE 'RP-090%'"
        ).fetchone()
        n = (mrow["m"] or 309) + 1
        code = f"RP-{n:06d}"
        while conn.execute("SELECT 1 FROM rp_requests WHERE code=?", (code,)).fetchone():
            n += 1
            code = f"RP-{n:06d}"
        rid = conn.execute(
            """INSERT INTO rp_requests(code,order_id,document_id,version_id,purpose,color_req,lens_spec,
                                       qty,due_date,handler,status,note,created_by,created_by_name,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?)""",
            (code, order_id, v["document_id"], version_id, purpose.strip(), color_req.strip(),
             lens_spec.strip(), max(1, qty), due_date, handler.strip(), note.strip(),
             me["id"], me["name"], ts)).lastrowid
        log(conn, me, "开打样单", "rp", rid, order_id,
            f"{code} {o['product_name']} 按 v{v['version_no']} 打 {qty} 只")
        conn.commit()
        return go("/rp", ok=f"打样单 {code} 已开")
    finally:
        conn.close()


@app.post("/rp/{rid}/status")
def rp_status(request: Request, rid: int, status: str = Form(...)):
    me = current_user(request)
    conn = db.connect()
    try:
        r = conn.execute("SELECT * FROM rp_requests WHERE id=?", (rid,)).fetchone()
        if not r:
            return go("/rp", err="未找到该打样单")
        if status not in db.RP_STATUS:
            return go("/rp", err="状态不合法")
        ts = db.now_str()
        if status == "done":
            conn.execute("UPDATE rp_requests SET status='done', done_at=?, done_by_name=? WHERE id=?",
                         (ts, me["name"], rid))
        else:
            conn.execute("UPDATE rp_requests SET status=? WHERE id=?", (status, rid))
        log(conn, me, "打样进度", "rp", rid, r["order_id"],
            f"{r['code']} → {db.RP_STATUS[status]}")
        conn.commit()
        return go("/rp", ok=f"{r['code']} 已更新为{db.RP_STATUS[status]}")
    finally:
        conn.close()


# ---------------------------------------------------------------- BOM

@app.get("/bom", response_class=HTMLResponse)
def bom_list(request: Request):
    conn = db.connect()
    try:
        rows = []
        for r in conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall():
            d = dict(r)
            items = bom_view(conn, r["id"])
            d["n"] = len(items)
            d["warn"] = sum(1 for i in items if i["lamp"] == "warn")
            d["bad"] = sum(1 for i in items if i["lamp"] == "bad")
            d["stage_info"] = db.stage(r["current_stage"])
            rows.append(d)
        return render(request, "bom_list.html", rows=rows)
    finally:
        conn.close()


@app.get("/bom/{oid}", response_class=HTMLResponse)
def bom_page(request: Request, oid: int):
    me = current_user(request)
    conn = db.connect()
    try:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return go("/orders", err="找不到订单")
        o = dict(o)
        rows = bom_view(conn, oid)
        docs = [dict(r) for r in conn.execute(
            "SELECT * FROM documents WHERE order_id=? ORDER BY design_no", (oid,)).fetchall()]
        snaps = [dict(r) for r in conn.execute(
            """SELECT b.*, h.code AS ho_code, h.status AS ho_status, h.received_at
               FROM handover_boms b JOIN handovers h ON h.id=b.handover_id
               WHERE h.order_id=? ORDER BY b.id DESC""", (oid,)).fetchall()]
        lamps = {"ok": sum(1 for r in rows if r["lamp"] == "ok"),
                 "warn": sum(1 for r in rows if r["lamp"] == "warn"),
                 "bad": sum(1 for r in rows if r["lamp"] == "bad")}
        can_edit = me["dept"] == "engineering" or me["role"] in ("manager", "admin")
        return render(request, "bom.html", o=o, rows=rows, docs=docs, snaps=snaps,
                      lamps=lamps, can_edit=can_edit)
    finally:
        conn.close()


@app.post("/bom/{oid}/add")
def bom_add(request: Request, oid: int, part_name: str = Form(...), part_no: str = Form(""),
            spec: str = Form(""), qty: float = Form(1), unit: str = Form("件"),
            source: str = Form("自制"), parent_id: str = Form(""), document_id: str = Form(""),
            note: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        if me["dept"] != "engineering" and me["role"] not in ("manager", "admin"):
            return go(f"/bom/{oid}", err="BOM 由工程部维护，当前账号为只读权限")
        ts = db.now_str()
        seq = (conn.execute("SELECT MAX(seq) m FROM bom_items WHERE order_id=?", (oid,)).fetchone()["m"] or 0) + 1
        conn.execute(
            """INSERT INTO bom_items(order_id,parent_id,seq,part_no,part_name,spec,qty,unit,source,
                                     document_id,note,updated_by_name,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (oid, int(parent_id) if parent_id else None, seq, part_no.strip(), part_name.strip(),
             spec, qty, unit, source, int(document_id) if document_id else None, note, me["name"], ts))
        conn.execute("UPDATE orders SET bom_rev=bom_rev+1, updated_at=? WHERE id=?", (ts, oid))
        log(conn, me, "改 BOM", "order", oid, oid, f"新增零件 {part_no} {part_name} ×{qty:g}")
        conn.commit()
        return go(f"/bom/{oid}", ok="已新增零件")
    finally:
        conn.close()


@app.post("/bom/{oid}/delete")
def bom_delete(request: Request, oid: int, item_id: int = Form(...)):
    me = current_user(request)
    conn = db.connect()
    try:
        if me["dept"] != "engineering" and me["role"] not in ("manager", "admin"):
            return go(f"/bom/{oid}", err="BOM 归工程部维护，你只能看")
        it = conn.execute("SELECT * FROM bom_items WHERE id=?", (item_id,)).fetchone()
        if not it:
            return go(f"/bom/{oid}", err="该零件记录已不存在")
        conn.execute("DELETE FROM bom_items WHERE parent_id=?", (item_id,))
        conn.execute("DELETE FROM bom_items WHERE id=?", (item_id,))
        conn.execute("UPDATE orders SET bom_rev=bom_rev+1, updated_at=? WHERE id=?", (db.now_str(), oid))
        log(conn, me, "改 BOM", "order", oid, oid, f"删除零件 {it['part_no']} {it['part_name']}")
        conn.commit()
        return go(f"/bom/{oid}", ok="已删除（含全部子件）")
    finally:
        conn.close()


@app.get("/bom/{oid}/export")
def bom_export(request: Request, oid: int):
    conn = db.connect()
    try:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        rows = bom_view(conn, oid)
    finally:
        conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["序号", "零件号", "零件名称", "规格/材质", "数量", "单位", "来源", "对应图档", "图档状态", "备注"])
    for r in rows:
        w.writerow([r["no"], r["part_no"], r["part_name"], r["spec"], f"{r['qty']:g}", r["unit"],
                    r["source"], r["doc"]["design_no"] if r["doc"] else "",
                    r["lamp_txt"], r["note"] or ""])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    name = f"{o['code']}_BOM_r{o['bom_rev']}.csv"
    return StreamingResponse(io.BytesIO(data), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


# ---------------------------------------------------------------- 变更单

@app.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request):
    me = current_user(request)
    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            """SELECT c.*, o.code AS order_code, o.product_name FROM changes c
               JOIN orders o ON o.id=c.order_id ORDER BY c.id DESC""").fetchall()]
        can_decide = me["role"] in ("manager", "admin")
        orders_list = [dict(r) for r in conn.execute(
            "SELECT id, code, product_name FROM orders WHERE status='active' ORDER BY id DESC").fetchall()]
        return render(request, "changes.html", rows=rows, can_decide=can_decide, orders_list=orders_list)
    finally:
        conn.close()


@app.post("/changes/new")
def change_new(request: Request, order_id: int = Form(...), kind: str = Form("customer"),
               title: str = Form(...), detail: str = Form(""), source: str = Form(""),
               customer_confirmed: str = Form(""), document_id: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        ts = db.now_str()
        n = conn.execute("SELECT COUNT(*) c FROM changes").fetchone()["c"] + 1
        prefix = {"customer": "BG", "mold": "GM", "eng": "GC"}.get(kind, "BG")
        code = f"{prefix}-{n:04d}"
        while conn.execute("SELECT 1 FROM changes WHERE code=?", (code,)).fetchone():
            n += 1
            code = f"{prefix}-{n:04d}"
        cid = conn.execute(
            """INSERT INTO changes(code,order_id,kind,title,detail,source,customer_confirmed,status,
                                   document_id,created_by,created_by_name,created_at)
               VALUES(?,?,?,?,?,?,?,'open',?,?,?,?)""",
            (code, order_id, kind, title, detail, source,
             1 if customer_confirmed == "on" else 0,
             int(document_id) if document_id else None, me["id"], me["name"], ts),
        ).lastrowid
        log(conn, me, "提变更单", "change", cid, order_id,
            f"{code} {db.CHANGE_KIND.get(kind, kind)}：{title}")
        conn.commit()
        return go("/changes", ok=f"{code} 已提交")
    finally:
        conn.close()


@app.post("/changes/{cid}/decide")
def change_decide(request: Request, cid: int, status: str = Form(...), note: str = Form("")):
    me = current_user(request)
    conn = db.connect()
    try:
        if me["role"] not in ("manager", "admin"):
            return go("/changes", err="变更裁定由主管执行")
        c = conn.execute("SELECT * FROM changes WHERE id=?", (cid,)).fetchone()
        if not c:
            return go("/changes", err="未找到该变更单")
        conn.execute("UPDATE changes SET status=?, decision_note=?, decided_by_name=?, decided_at=? WHERE id=?",
                     (status, note, me["name"], db.now_str(), cid))
        log(conn, me, "裁定变更", "change", cid, c["order_id"],
            f"{c['code']} {db.CHANGE_STATUS.get(status, status)}：{note}")
        conn.commit()
        return go("/changes", ok="裁定已记录")
    finally:
        conn.close()


# ---------------------------------------------------------------- 动态 / 搜索 / 设置

@app.get("/activity", response_class=HTMLResponse)
def activity(request: Request, q: str = "", actor: str = ""):
    conn = db.connect()
    try:
        sql = "SELECT * FROM audit_logs WHERE 1=1"
        args = []
        if q:
            sql += " AND detail LIKE ?"
            args.append(f"%{q}%")
        if actor:
            sql += " AND actor_name=?"
            args.append(actor)
        sql += " ORDER BY id DESC LIMIT 400"
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        people = [r["actor_name"] for r in conn.execute(
            "SELECT DISTINCT actor_name FROM audit_logs").fetchall()]
        return render(request, "activity.html", rows=rows, q=q, actor=actor, people=people)
    finally:
        conn.close()


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    conn = db.connect()
    try:
        docs, ords = [], []
        if q.strip():
            like = f"%{q.strip()}%"
            docs = [dict(r) for r in conn.execute(
                """SELECT d.*, o.code AS order_code FROM documents d LEFT JOIN orders o ON o.id=d.order_id
                   WHERE d.design_no LIKE ? OR IFNULL(d.mold_no,'') LIKE ? OR d.title LIKE ?
                      OR IFNULL(d.aliases,'') LIKE ? ORDER BY d.updated_at DESC""",
                (like, like, like, like)).fetchall()]
            for d in docs:
                d["ver"] = dict(conn.execute("SELECT * FROM doc_versions WHERE id=?", (d["current_version_id"],)).fetchone()) if d["current_version_id"] else None
            ords = [dict(r) for r in conn.execute(
                "SELECT * FROM orders WHERE code LIKE ? OR product_name LIKE ? OR IFNULL(customer_name,'') LIKE ?",
                (like, like, like)).fetchall()]
        return render(request, "search.html", q=q, docs=docs, ords=ords)
    finally:
        conn.close()


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    me = current_user(request)
    conn = db.connect()
    try:
        users = [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY id").fetchall()]
        counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("orders", "documents", "doc_versions", "handovers", "bom_items")}
        return render(request, "settings.html", users=users, counts=counts,
                      can_admin=me["role"] == "admin")
    finally:
        conn.close()


@app.post("/settings/users/new")
def user_new(request: Request, username: str = Form(...), name: str = Form(...),
             dept: str = Form("structure"), role: str = Form("staff"), password: str = Form("123456")):
    me = current_user(request)
    if me["role"] != "admin":
        return go("/settings", err="仅管理员可新增账号")
    conn = db.connect()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username.strip(),)).fetchone():
            return go("/settings", err="该账号已存在")
        conn.execute("INSERT INTO users(username,name,dept,role,pwd_hash,created_at) VALUES(?,?,?,?,?,?)",
                     (username.strip(), name, dept, role, db.hash_pwd(password), db.now_str()))
        log(conn, me, "加账号", "user", 0, None, f"{name}（{db.dept_name(dept)}）")
        conn.commit()
        return go("/settings", ok="账号已创建")
    finally:
        conn.close()


@app.post("/settings/users/{uid}/toggle")
def user_toggle(request: Request, uid: int):
    me = current_user(request)
    if me["role"] != "admin":
        return go("/settings", err="仅管理员可变更账号状态")
    conn = db.connect()
    try:
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if u and u["username"] != "admin":
            conn.execute("UPDATE users SET active=? WHERE id=?", (0 if u["active"] else 1, uid))
            conn.commit()
        return go("/settings", ok="已保存")
    finally:
        conn.close()


@app.post("/settings/reset")
def settings_reset(request: Request, confirm: str = Form("")):
    me = current_user(request)
    if me["role"] != "admin":
        return go("/settings", err="仅管理员可重置数据")
    if confirm != "RESET":
        return go("/settings", err="请输入 RESET 以确认")
    conn = db.connect()
    try:
        db.reset_business_data(conn)
        for f in FILES_DIR.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        db.seed_demo(conn)
        conn.commit()
        return go("/settings", ok="演示数据已重置")
    finally:
        conn.close()


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
