import os, uuid, json, tempfile
from datetime import datetime, date
from functools import wraps
from flask import (Flask, g, render_template, request, redirect, url_for, session,
                   flash, send_from_directory, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import config, documents as D, dbconn, storage

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
if storage.BUCKET:   # files live in S3; local dirs are scratch space only
    _SCRATCH = tempfile.mkdtemp(prefix="intranet-")
    GEN, UPL = os.path.join(_SCRATCH, "generated"), os.path.join(_SCRATCH, "uploads")
else:
    GEN, UPL = os.path.join(DATA, "generated"), os.path.join(DATA, "uploads")
os.makedirs(GEN, exist_ok=True); os.makedirs(UPL, exist_ok=True)
app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
if os.environ.get("INTRANET_PROD"):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
    app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=8 * 3600)
_FAILS = {}

NDA_STATUSES = ["Draft", "Internal review", "Sent to counterparty", "Signed by counterparty",
                "Fully executed", "Expired / terminated"]


def db():
    if "db" not in g:
        g.db = dbconn.Conn(os.path.join(DATA, "intranet.db"))
    return g.db


@app.teardown_appcontext
def _close(e):
    if "db" in g: g.db.close()


def init_db():
    with app.app_context():
        c = db(); c.ensure_schema()
        import migrate_import; migrate_import.run(c)
        for m in config.MEMBERS:
            c.execute("insert or ignore into users(name,email,pw,admin) values(?,?,?,?)",
                      (m["name"], m["email"], generate_password_hash("changeme"), int(m["admin"])))
        c.commit()


def now(): return datetime.now().isoformat(timespec="seconds")
def newref(prefix): return f"{prefix}-{date.today():%Y%m}-{uuid.uuid4().hex[:5].upper()}"


def audit(entity, eid, action, detail=""):
    db().execute("insert into audit(at,user_id,entity,entity_id,action,detail) values(?,?,?,?,?,?)",
                 (now(), session.get("uid"), entity, eid, action, detail)); db().commit()


def me():
    return db().execute("select * from users where id=?", (session.get("uid"),)).fetchone()


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("uid"): return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


@app.context_processor
def inject(): return {"cfg": config, "me": me() if session.get("uid") else None}


@app.template_filter("fromiso")
def fromiso(s): return s.replace("T", " ") if s else ""

# --------------------------------------------------------------- auth / home
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        import time
        key = request.remote_addr; t = time.time()
        recent = [x for x in _FAILS.get(key, []) if t - x < 900]
        if len(recent) >= 5:
            flash("Too many failed attempts - try again in 15 minutes", "error")
            return render_template("login.html"), 429
        u = db().execute("select * from users where email=? and active=1",
                         (request.form["email"].strip().lower(),)).fetchone()
        if u and check_password_hash(u["pw"], request.form["password"]):
            _FAILS.pop(key, None); session.permanent = True
            session["uid"] = u["id"]
            nxt = request.args.get("next") or "/"
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        _FAILS[key] = recent + [t]
        flash("Invalid login", "error")
    return render_template("login.html")


@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required
def password():
    if request.method == "POST":
        if not check_password_hash(me()["pw"], request.form["old"]): flash("Current password wrong", "error")
        elif len(request.form["new"]) < 8: flash("Use at least 8 characters", "error")
        else:
            db().execute("update users set pw=? where id=?", (generate_password_hash(request.form["new"]), me()["id"]))
            db().commit(); flash("Password changed"); return redirect("/")
    return render_template("password.html")


@app.route("/")
@login_required
def home():
    c = db(); uid = session["uid"]
    pending = c.execute("""select m.* from memos m where m.status='Awaiting sign-off' and not exists
        (select 1 from signoffs s where s.memo_id=m.id and s.user_id=?)""", (uid,)).fetchall()
    ndas = c.execute("select * from ndas where status not in ('Fully executed','Expired / terminated') order by id desc limit 5").fetchall()
    return render_template("home.html", pending=pending, ndas=ndas)


@app.route("/files/<kind>/<path:name>")
@login_required
def files(kind, name):
    if kind not in ("generated", "uploads"): abort(404)
    if storage.BUCKET: return redirect(storage.download_url(kind, secure_filename(name)))
    return send_from_directory(os.path.join(DATA, kind), name, as_attachment=True)

# --------------------------------------------------------------- 1. NDAs
@app.route("/nda")
@login_required
def nda_list():
    rows = db().execute("select n.*, u.name as author from ndas n left join users u on u.id=n.created_by order by n.id desc").fetchall()
    return render_template("nda_list.html", rows=rows)


@app.route("/nda/new", methods=["GET", "POST"])
@login_required
def nda_new():
    if request.method == "POST":
        f = request.form
        purpose = f["purpose"].strip() or config.NDA_PURPOSES.get(f["cp_type"], "").format(
            firm=config.FIRM_NAME, cp=f["counterparty"])
        ref = newref("NDA")
        cur = db().execute("""insert into ndas(ref,counterparty,cp_details,cp_type,purpose,ci_scope,duration,
            conf_period,governing_law,changes,effective_date,our_signatory,cp_signatory,cp_signatory_title,
            cp_email,status,created_by,created,dispute,our_title,our_email) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ref, f["counterparty"].strip(), f["cp_details"], f["cp_type"], purpose, "", "",
             f["conf_period"], "England and Wales", f["changes"], f["effective_date"], f["our_signatory"],
             f["cp_signatory"], f["cp_signatory_title"], f["cp_email"], "Draft", session["uid"], now(),
             "Litigation in the courts of England and Wales", f["our_title"], f["our_email"]))
        db().commit(); nid = cur.lastrowid
        _generate_nda(nid); audit("nda", nid, "created", ref)
        return redirect(url_for("nda_view", nid=nid))
    return render_template("nda_form.html", purposes=config.NDA_PURPOSES, today=date.today().isoformat(),
                           user=me())


def _generate_nda(nid):
    n = dict(db().execute("select * from ndas where id=?", (nid,)).fetchone())
    base = secure_filename(f"{n['ref']}_{n['counterparty']}")
    docx = os.path.join(GEN, base + ".docx")
    D.build_nda_docx(n, docx)
    raw = D.docx_to_pdf(docx, GEN)
    pdf = os.path.join(GEN, base + "_issued.pdf")
    D.wordmark_pdf(raw, pdf, n["counterparty"], n["ref"], append=None, brand=False)
    os.remove(raw); storage.put("generated", docx); storage.put("generated", pdf)
    db().execute("update ndas set docx=?, pdf=? where id=?", (os.path.basename(docx), os.path.basename(pdf), nid))
    db().commit()


@app.route("/nda/<int:nid>", methods=["GET", "POST"])
@login_required
def nda_view(nid):
    n = db().execute("select * from ndas where id=?", (nid,)).fetchone() or abort(404)
    if request.method == "POST":
        act = request.form["action"]
        if act == "status":
            st = request.form["status"]
            db().execute("update ndas set status=? where id=?", (st, nid)); audit("nda", nid, "status", st)
        elif act == "regen":
            _generate_nda(nid); audit("nda", nid, "regenerated")
        elif act == "signed" and request.files.get("file"):
            fn = secure_filename(f"{n['ref']}_signed_{request.files['file'].filename}")
            request.files["file"].save(os.path.join(UPL, fn)); storage.put("uploads", os.path.join(UPL, fn))
            db().execute("update ndas set signed=?, status='Fully executed' where id=?", (fn, nid))
            audit("nda", nid, "signed copy uploaded", fn)
        db().commit(); return redirect(url_for("nda_view", nid=nid))
    log = db().execute("select a.*, u.name from audit a left join users u on u.id=a.user_id where entity='nda' and entity_id=? order by a.id", (nid,)).fetchall()
    return render_template("nda_view.html", n=n, statuses=NDA_STATUSES, log=log,
                           terms_present=os.path.exists(D.ONENDA_TEMPLATE))

# --------------------------------------------------------------- 2 & 3. Personalised docs
@app.route("/issue", methods=["GET", "POST"])
@login_required
def issue():
    if request.method == "POST":
        f = request.files.get("file"); prospect = request.form["prospect"].strip()
        if not f or not f.filename.lower().endswith(".docx") or not prospect:
            flash("Upload a .docx and enter the prospect's name", "error"); return redirect(url_for("issue"))
        ref = newref("DOC")
        src = os.path.join(UPL, secure_filename(f"{ref}_src_{f.filename}")); f.save(src); storage.put("uploads", src)
        stem = secure_filename(f"{ref}_{os.path.splitext(f.filename)[0]}_{prospect}")
        docx = os.path.join(GEN, stem + ".docx")
        D.personalise_docx(src, docx, prospect, ref)
        raw = D.docx_to_pdf(docx, GEN)
        pdf = os.path.join(GEN, stem + "_wm.pdf")
        D.wordmark_pdf(raw, pdf, prospect, ref, password=request.form.get("password") or None)
        os.remove(raw); storage.put("generated", docx); storage.put("generated", pdf)
        cur = db().execute("insert into issues(ref,prospect,src_name,docx,pdf,sha,notes,created_by,created) values(?,?,?,?,?,?,?,?,?)",
                           (ref, prospect, f.filename, os.path.basename(docx), os.path.basename(pdf), D.sha256(pdf),
                            request.form.get("notes", ""), session["uid"], now()))
        db().commit(); audit("issue", cur.lastrowid, "issued", f"{f.filename} -> {prospect}")
        flash(f"Issued {ref} for {prospect}")
        return redirect(url_for("issue"))
    rows = db().execute("select i.*, u.name as author from issues i left join users u on u.id=i.created_by order by i.id desc").fetchall()
    return render_template("issue.html", rows=rows)


@app.route("/verify", methods=["GET", "POST"])
@login_required
def verify():
    """Upload a leaked/forwarded PDF to find out who it was issued to."""
    hit = None
    if request.method == "POST" and request.files.get("file"):
        tmp = os.path.join(UPL, "verify_" + uuid.uuid4().hex); request.files["file"].save(tmp)
        sha = D.sha256(tmp)
        hit = db().execute("select * from issues where sha=?", (sha,)).fetchone()
        if not hit:
            try:
                meta = D.PdfReader(tmp).metadata or {}
                ref = (meta.get("/Subject") or "").replace("Ref ", "")
                hit = db().execute("select * from issues where ref=?", (ref,)).fetchone() or \
                      db().execute("select ref, counterparty as prospect, created from ndas where ref=?", (ref,)).fetchone()
            except Exception: pass
        os.remove(tmp)
        if not hit: flash("No match found in the issue register", "error")
    return render_template("verify.html", hit=hit)

# --------------------------------------------------------------- 4. Memos
def _memo_status(mid):
    members = db().execute("select id from users where active=1").fetchall()
    s = {r["user_id"]: r["decision"] for r in db().execute("select * from signoffs where memo_id=?", (mid,))}
    if "Rejected" in s.values(): st = "Rejected"
    elif all(s.get(m["id"]) == "Approved" for m in members): st = "Approved"
    else: st = "Awaiting sign-off"
    db().execute("update memos set status=? where id=? and status!='Withdrawn'", (st, mid)); db().commit()
    return st


@app.route("/memos")
@login_required
def memo_list():
    rows = db().execute("""select m.*, u.name as author,
        (select count(*) from signoffs s where s.memo_id=m.id and s.decision='Approved') as approvals
        from memos m left join users u on u.id=m.created_by order by m.id desc""").fetchall()
    n = db().execute("select count(*) from users where active=1").fetchone()[0]
    return render_template("memo_list.html", rows=rows, n=n)


@app.route("/memos/new", methods=["GET", "POST"])
@login_required
def memo_new():
    if request.method == "POST":
        f = request.form; att = None
        if request.files.get("file") and request.files["file"].filename:
            att = secure_filename(f"{uuid.uuid4().hex[:6]}_{request.files['file'].filename}")
            request.files["file"].save(os.path.join(UPL, att)); storage.put("uploads", os.path.join(UPL, att))
        ref = newref("MEMO")
        cur = db().execute("insert into memos(ref,title,amount,currency,supplier,body,attachment,status,created_by,created) values(?,?,?,?,?,?,?,?,?,?)",
                           (ref, f["title"], float(f["amount"] or 0), f["currency"], f["supplier"], f["body"], att,
                            "Awaiting sign-off", session["uid"], now()))
        db().commit(); audit("memo", cur.lastrowid, "created", ref)
        return redirect(url_for("memo_view", mid=cur.lastrowid))
    return render_template("memo_form.html")


@app.route("/memos/<int:mid>", methods=["GET", "POST"])
@login_required
def memo_view(mid):
    m = db().execute("select m.*, u.name as author from memos m left join users u on u.id=m.created_by where m.id=?", (mid,)).fetchone() or abort(404)
    if request.method == "POST":
        act = request.form["action"]
        if act in ("Approved", "Rejected") and m["status"] == "Awaiting sign-off":
            if act == "Rejected" and not request.form.get("comment"):
                flash("Please give a reason for rejecting", "error")
            else:
                db().execute("insert or replace into signoffs(memo_id,user_id,decision,comment,at,ip) values(?,?,?,?,?,?)",
                             (mid, session["uid"], act, request.form.get("comment", ""), now(), request.remote_addr))
                db().commit(); audit("memo", mid, act, request.form.get("comment", "")); _memo_status(mid)
        elif act == "withdraw" and m["created_by"] == session["uid"]:
            db().execute("update memos set status='Withdrawn' where id=?", (mid,)); db().commit(); audit("memo", mid, "withdrawn")
        return redirect(url_for("memo_view", mid=mid))
    members = db().execute("""select u.id, u.name, s.decision, s.comment, s.at from users u
        left join signoffs s on s.user_id=u.id and s.memo_id=? where u.active=1 order by u.id""", (mid,)).fetchall()
    mine = next((x for x in members if x["id"] == session["uid"]), None)
    return render_template("memo_view.html", m=m, members=members, mine=mine)


@app.route("/memos/<int:mid>/print")
@login_required
def memo_print(mid):
    m = db().execute("select m.*, u.name as author from memos m left join users u on u.id=m.created_by where m.id=?", (mid,)).fetchone() or abort(404)
    members = db().execute("""select u.name, s.decision, s.comment, s.at, s.ip from users u
        left join signoffs s on s.user_id=u.id and s.memo_id=? where u.active=1""", (mid,)).fetchall()
    return render_template("memo_print.html", m=m, members=members)


@app.route("/audit")
@login_required
def audit_log():
    rows = db().execute("select a.*, u.name from audit a left join users u on u.id=a.user_id order by a.id desc limit 500").fetchall()
    return render_template("audit.html", rows=rows)


@app.route("/healthz")
def healthz(): return "ok"


init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
