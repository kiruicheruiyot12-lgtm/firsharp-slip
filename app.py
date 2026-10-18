import html, json, os, re, secrets, threading, time, urllib.request
from flask import Flask, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy

load_dotenv()
BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "public"), static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=30 * 86400)

# --- DATABASE SETUP ---
db_url = os.environ.get('DATABASE_URL', 'sqlite:///local_test.db')
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- MODELS ---
class User(db.Model):
    id = db.Column(db.String(50), primary_key=True)
    ident = db.Column(db.String(120), unique=True, nullable=False)
    kind = db.Column(db.String(10), nullable=False)
    pw = db.Column(db.String(255), nullable=False)
    verified = db.Column(db.Boolean, default=False)
    createdAt = db.Column(db.BigInteger)

class Voucher(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    plan = db.Column(db.String(20), nullable=False)
    createdAt = db.Column(db.BigInteger)
    activatedAt = db.Column(db.BigInteger, nullable=True)
    userId = db.Column(db.String(50), nullable=True)

class Request(db.Model):
    id = db.Column(db.String(10), primary_key=True)
    userId = db.Column(db.String(50))
    ident = db.Column(db.String(120))
    plan = db.Column(db.String(20))
    code = db.Column(db.String(50))
    raw = db.Column(db.String(50))
    createdAt = db.Column(db.BigInteger)
    status = db.Column(db.String(20))

with app.app_context():
    db.create_all()
# ------------------

lock = threading.RLock()

# UPDATED: Synced exactly with the homepage and members.html dropdown
PLANS = {
    "triple": {"name": "Thripple pick – R100 (3 days)", "price": "R100", "days": 3, "only_latest": True},
    "weekly": {"name": "Weekly pass – R350 (7 days)", "price": "R350", "days": 7},
    "season": {"name": "Season – R700 (365 days)", "price": "R700", "days": 365},
}

# Keep read/write ONLY for picks.json
def read_picks():
    try:
        with open(os.path.join(BASE, "picks.json"), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def write_picks(data):
    with open(os.path.join(BASE, "picks.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def now_ms():
    return int(time.time() * 1000)

def tg_send(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        data = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        print("Telegram failed:", e)
        return False

def norm(code):
    return "".join(c for c in str(code or "").upper() if c.isalnum())

tries = {}
def limited(key, n=10):
    now = time.time()
    recent = [t for t in tries.get(key, []) if now - t < 900]
    recent.append(now)
    tries[key] = recent
    return len(recent) > n

def clean_id(raw):
    s = str(raw or "").strip().lower()
    if "@" in s:
        ok = re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s)
        return ("email", s) if ok else (None, None)
    d = re.sub(r"[\s\-()]", "", s).lstrip("+")
    if d.startswith("0"):
        d = "27" + d[1:]
    return ("phone", "+" + d) if re.fullmatch(r"\d{10,15}", d) else (None, None)

# ---- users ----
def current_user():
    uid = session.get("uid")
    return User.query.filter_by(id=uid).first() if uid else None

def login_session(u):
    session.clear()
    session.permanent = True
    session["uid"] = u.id

def user_access(uid):
    best = None
    vouchers = Voucher.query.filter_by(userId=uid).all()
    for v in vouchers:
        if not v.activatedAt:
            continue
        plan = PLANS[v.plan]
        exp = v.activatedAt + plan["days"] * 86400000
        if exp > now_ms() and (best is None or exp > best[1]):
            best = (plan, exp)
    return best

@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")

@app.post("/api/register")
def register():
    if limited(request.remote_addr + "reg"):
        return jsonify(error="Too many attempts. Try again in 15 minutes."), 429
    b = request.get_json(silent=True) or {}
    kind, ident = clean_id(b.get("identifier"))
    pw = str(b.get("password") or "")
    if not kind:
        return jsonify(error="Enter a valid email or phone number, e.g. 082 123 4567."), 400
    if len(pw) < 8:
        return jsonify(error="Password must be at least 8 characters."), 400
    with lock:
        u = User.query.filter_by(ident=ident).first()
        if u and u.verified:
            return jsonify(error="An account with this email or phone already exists. Sign in instead."), 409
        if not u:
            u = User(id=secrets.token_hex(6), ident=ident, kind=kind, verified=False, createdAt=now_ms())
            db.session.add(u)
        u.pw = generate_password_hash(pw)
        u.verified = True
        db.session.commit()
    login_session(u)
    return jsonify(ok=True)

@app.post("/api/login")
def login():
    if limited(request.remote_addr + "log"):
        return jsonify(error="Too many attempts. Try again in 15 minutes."), 429
    b = request.get_json(silent=True) or {}
    kind, ident = clean_id(b.get("identifier"))
    u = User.query.filter_by(ident=ident).first() if kind else None
    if not u or not check_password_hash(u.pw, str(b.get("password") or "")):
        return jsonify(error="Wrong email, phone or password."), 401
    login_session(u)
    return jsonify(ok=True)

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)

@app.get("/api/me")
def me():
    u = current_user()
    if not u:
        return jsonify(error="Not signed in"), 401
    a = user_access(u.id)
    reqs = Request.query.filter_by(userId=u.id).order_by(Request.createdAt.desc()).limit(5).all()
    mine = [{"plan": PLANS[r.plan]["name"], "status": r.status} for r in reqs]
    return jsonify(identifier=u.ident, plan=a[0]["name"] if a else None,
                   expires=a[1] if a else None, requests=mine)

@app.post("/api/redeem")
def redeem():
    u = current_user()
    if not u:
        return jsonify(error="Please sign in first."), 401
    if limited(request.remote_addr + "red"):
        return jsonify(error="Too many attempts. Try again in 15 minutes."), 429
    b = request.get_json(silent=True) or {}
    raw = str(b.get("code") or "").strip()[:40]
    code = norm(raw)
    
    with lock:
        v = Voucher.query.filter_by(code=code).first()
        if not v:
            pk = b.get("plan")
            if pk not in PLANS or len(code) < 4:
                return jsonify(error="Enter your voucher number and choose a plan."), 400
            existing_req = Request.query.filter_by(code=code).first()
            if existing_req:
                return jsonify(error="This voucher number has already been submitted."), 409
            
            r = Request(id=secrets.token_hex(4), userId=u.id, ident=u.ident, plan=pk,
                        code=code, raw=raw, createdAt=now_ms(), status="pending")
            auto = os.environ.get("AUTO_APPROVE", "true") != "false"
            
            if auto:
                r.status = "approved"
                new_v = Voucher(code="REQ" + r.id, plan=pk, createdAt=now_ms(),
                                activatedAt=now_ms(), userId=u.id)
                db.session.add(new_v)
                
            db.session.add(r)
            db.session.commit()
            
            head = "Voucher received (access already granted, please check it)" if auto else "Voucher to check"
            tg_send(f"<b>{head}</b>\nPlan: {PLANS[pk]['name']} ({PLANS[pk]['price']})\n"
                    f"Voucher number: <code>{html.escape(raw)}</code>\n"
                    f"Customer: {html.escape(u.ident)}\nRequest ID: <code>{r.id}</code>")
            if auto:
                return jsonify(plan=PLANS[pk]["name"], expires=now_ms() + PLANS[pk]["days"] * 86400000)
            return jsonify(pending=True, plan=PLANS[pk]["name"]), 202
            
        if v.userId and v.userId != u.id:
            return jsonify(error="This voucher has already been used on another account."), 403
        if not v.userId:
            v.userId = u.id
            v.activatedAt = now_ms()
            db.session.commit()
            
    plan = PLANS[v.plan]
    exp = v.activatedAt + plan["days"] * 86400000
    if exp < now_ms():
        return jsonify(error="This voucher has expired."), 403
    return jsonify(plan=plan["name"], expires=exp)

@app.get("/api/picks")
def picks():
    u = current_user()
    if not u:
        return jsonify(error="Please sign in."), 401
    a = user_access(u.id)
    if not a:
        return jsonify(error="No active plan yet. Submit a voucher to unlock picks.", noPlan=True), 402
    
    rows = sorted(read_picks(), key=lambda p: p["createdAt"], reverse=True)
    if a[0].get("only_latest"):
        rows = rows[:1]
    return jsonify(plan=a[0]["name"], expires=a[1], picks=rows)

# ---- admin ----
def is_admin():
    key = os.environ.get("ADMIN_KEY", "")
    return bool(key) and secrets.compare_digest(request.headers.get("x-admin-key", ""), key)

@app.post("/api/admin/vouchers")
def make_vouchers():
    if not is_admin():
        return jsonify(error="No"), 401
    body = request.get_json(silent=True) or {}
    plan, count, custom = body.get("plan"), int(body.get("count", 1)), body.get("codes")
    if plan not in PLANS or (not custom and not 1 <= count <= 500):
        return jsonify(error="plan must be triple, weekly or season; count 1-500"), 400
    chars = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    made = []
    
    with lock:
        if custom:
            for raw in list(custom)[:500]:
                code = norm(raw)
                if len(code) >= 6 and not Voucher.query.filter_by(code=code).first():
                    db.session.add(Voucher(code=code, plan=plan, createdAt=now_ms(), activatedAt=None))
                    made.append(code)
        else:
            for _ in range(count):
                code = "".join(secrets.choice(chars) for _ in range(12))
                db.session.add(Voucher(code=code, plan=plan, createdAt=now_ms(), activatedAt=None))
                made.append("-".join(code[i:i + 4] for i in range(0, 12, 4)))
        db.session.commit()
        
    sent = False
    if body.get("telegram", True):
        sent = True
        for i in range(0, len(made), 50):
            lines = "\n".join(f"<code>{c}</code>" for c in made[i:i + 50])
            sent = tg_send(f"<b>{PLANS[plan]['name']} vouchers</b>\n{lines}") and sent
    return jsonify(plan=plan, codes=made, telegram=sent)

@app.post("/api/admin/picks")
def add_pick():
    if not is_admin():
        return jsonify(error="No"), 401
    b = request.get_json(silent=True) or {}
    if not (b.get("match") and b.get("pick") and b.get("odds")):
        return jsonify(error="match, pick and odds are required"), 400
    row = {"id": secrets.token_hex(4), "createdAt": int(time.time()),
           "match": b["match"], "pick": b["pick"], "odds": str(b["odds"]),
           "stake": b.get("stake") or "1 unit", "why": b.get("why", ""), "result": "pending"}
    with lock:
        rows = read_picks()
        rows.append(row)
        write_picks(rows)
    return jsonify(row)

@app.post("/api/admin/picks/<pick_id>/result")
def set_result(pick_id):
    if not is_admin():
        return jsonify(error="No"), 401
    result = (request.get_json(silent=True) or {}).get("result")
    with lock:
        rows = read_picks()
        row = next((p for p in rows if p["id"] == pick_id), None)
        if not row:
            return jsonify(error="Not found"), 404
        row["result"] = result
        write_picks(rows)
    return jsonify(row)

@app.get("/api/admin/requests")
def list_requests():
    if not is_admin():
        return jsonify(error="No"), 401
    status = request.args.get("status", "pending")
    reqs = Request.query.filter_by(status=status).all()
    return jsonify([{
        "id": r.id, "userId": r.userId, "ident": r.ident, "plan": r.plan,
        "code": r.code, "raw": r.raw, "createdAt": r.createdAt, "status": r.status
    } for r in reqs])

@app.post("/api/admin/requests/<rid>/<action>")
def review_request(rid, action):
    if not is_admin():
        return jsonify(error="No"), 401
    if action not in ("approve", "reject"):
        return jsonify(error="Use approve or reject"), 400
    with lock:
        r = Request.query.filter_by(id=rid).first()
        if not r:
            return jsonify(error="Not found"), 404
        if r.status != "pending" and not (action == "reject" and r.status == "approved"):
            return jsonify(error="Already " + r.status), 400
            
        if action == "reject" and r.status == "approved":
            v = Voucher.query.filter_by(code="REQ" + r.id).first()
            if v:
                db.session.delete(v)
                
        if action == "approve":
            db.session.add(Voucher(code="REQ" + r.id, plan=r.plan, createdAt=now_ms(),
                                   activatedAt=now_ms(), userId=r.userId))
                                   
        r.status = "approved" if action == "approve" else "rejected"
        db.session.commit()
        
    return jsonify({"id": r.id, "status": r.status})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f"Running on http://localhost:{port}")
    app.run(host="127.0.0.1", port=port)