import html, json, os, re, secrets, threading, time, urllib.request
from flask import Flask, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()
BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "public"), static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=30 * 86400)
lock = threading.RLock()

PLANS = {
    "single": {"name": "Single pick", "price": "R90", "days": 1, "only_latest": True},
    "weekly": {"name": "Weekly pass", "price": "R350", "days": 7},
    "season": {"name": "Season", "price": "R2,700", "days": 365},
}

def read(name):
    with open(os.path.join(BASE, name), encoding="utf-8") as f:
        return json.load(f)

def write(name, data):
    with open(os.path.join(BASE, name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def now_ms():
    return int(time.time() * 1000)

def tg_send(text):
    """Send a message to your Telegram channel or chat. Returns True if sent."""
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
    """Return ('email'|'phone', normalised) or (None, None)."""
    s = str(raw or "").strip().lower()
    if "@" in s:
        ok = re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s)
        return ("email", s) if ok else (None, None)
    d = re.sub(r"[\s\-()]", "", s).lstrip("+")
    if d.startswith("0"):
        d = "27" + d[1:]
    return ("phone", "+" + d) if re.fullmatch(r"\d{10,15}", d) else (None, None)

# ---- users ----
def find_user(ident):
    return next((u for u in read("users.json") if u["ident"] == ident), None)

def current_user():
    uid = session.get("uid")
    return next((u for u in read("users.json") if u["id"] == uid), None) if uid else None

def login_session(u):
    session.clear()
    session.permanent = True
    session["uid"] = u["id"]

def user_access(uid):
    best = None
    for v in read("vouchers.json"):
        if v.get("userId") != uid or not v.get("activatedAt"):
            continue
        plan = PLANS[v["plan"]]
        exp = v["activatedAt"] + plan["days"] * 86400000
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
        users = read("users.json")
        u = next((x for x in users if x["ident"] == ident), None)
        if u and u.get("verified"):
            return jsonify(error="An account with this email or phone already exists. Sign in instead."), 409
        if not u:
            u = {"id": secrets.token_hex(6), "ident": ident, "kind": kind,
                 "verified": False, "createdAt": now_ms()}
            users.append(u)
        u["pw"] = generate_password_hash(pw)
        u["verified"] = True
        write("users.json", users)
    login_session(u)
    return jsonify(ok=True)

@app.post("/api/login")
def login():
    if limited(request.remote_addr + "log"):
        return jsonify(error="Too many attempts. Try again in 15 minutes."), 429
    b = request.get_json(silent=True) or {}
    kind, ident = clean_id(b.get("identifier"))
    u = find_user(ident) if kind else None
    if not u or not check_password_hash(u["pw"], str(b.get("password") or "")):
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
    a = user_access(u["id"])
    mine = [{"plan": PLANS[r["plan"]]["name"], "status": r["status"]}
            for r in read("requests.json") if r["userId"] == u["id"]][-5:]
    return jsonify(identifier=u["ident"], plan=a[0]["name"] if a else None,
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
        vouchers = read("vouchers.json")
        v = next((x for x in vouchers if x["code"] == code), None)
        if not v:
            # Not one of our own codes: treat it as a bought voucher and send it to you to check
            pk = b.get("plan")
            if pk not in PLANS or len(code) < 4:
                return jsonify(error="Enter your voucher number and choose a plan."), 400
            reqs = read("requests.json")
            if any(r["code"] == code for r in reqs):
                return jsonify(error="This voucher number has already been submitted."), 409
            r = {"id": secrets.token_hex(4), "userId": u["id"], "ident": u["ident"], "plan": pk,
                 "code": code, "raw": raw, "createdAt": now_ms(), "status": "pending"}
            auto = os.environ.get("AUTO_APPROVE", "true") != "false"
            if auto:  # give access straight away; you check the voucher afterwards
                r["status"] = "approved"
                vouchers.append({"code": "REQ" + r["id"], "plan": pk, "createdAt": now_ms(),
                                 "activatedAt": now_ms(), "userId": u["id"]})
                write("vouchers.json", vouchers)
            reqs.append(r)
            write("requests.json", reqs)
            head = "Voucher received (access already granted, please check it)" if auto else "Voucher to check"
            tg_send(f"<b>{head}</b>\nPlan: {PLANS[pk]['name']} ({PLANS[pk]['price']})\n"
                    f"Voucher number: <code>{html.escape(raw)}</code>\n"
                    f"Customer: {html.escape(u['ident'])}\nRequest ID: <code>{r['id']}</code>")
            if auto:
                return jsonify(plan=PLANS[pk]["name"], expires=now_ms() + PLANS[pk]["days"] * 86400000)
            return jsonify(pending=True, plan=PLANS[pk]["name"]), 202
        if v.get("userId") and v["userId"] != u["id"]:
            return jsonify(error="This voucher has already been used on another account."), 403
        if not v.get("userId"):
            v["userId"], v["activatedAt"] = u["id"], now_ms()
            write("vouchers.json", vouchers)
    plan = PLANS[v["plan"]]
    exp = v["activatedAt"] + plan["days"] * 86400000
    if exp < now_ms():
        return jsonify(error="This voucher has expired."), 403
    return jsonify(plan=plan["name"], expires=exp)

@app.get("/api/picks")
def picks():
    u = current_user()
    if not u:
        return jsonify(error="Please sign in."), 401
    a = user_access(u["id"])
    if not a:
        return jsonify(error="No active plan yet. Submit a voucher to unlock picks.", noPlan=True), 402
    rows = sorted(read("picks.json"), key=lambda p: p["createdAt"], reverse=True)
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
        return jsonify(error="plan must be single, weekly or season; count 1-500"), 400
    chars = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    with lock:
        vouchers = read("vouchers.json")
        made = []
        if custom:  # your own codes, e.g. {"plan":"weekly","codes":["482913","771204"]}
            existing = {v["code"] for v in vouchers}
            for raw in list(custom)[:500]:
                code = norm(raw)
                if len(code) >= 6 and code not in existing:
                    vouchers.append({"code": code, "plan": plan, "createdAt": now_ms(), "activatedAt": None})
                    existing.add(code)
                    made.append(code)
        else:
            for _ in range(count):
                code = "".join(secrets.choice(chars) for _ in range(12))
                vouchers.append({"code": code, "plan": plan, "createdAt": now_ms(), "activatedAt": None})
                made.append("-".join(code[i:i + 4] for i in range(0, 12, 4)))
        write("vouchers.json", vouchers)
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
        rows = read("picks.json"); rows.append(row); write("picks.json", rows)
    return jsonify(row)

@app.post("/api/admin/picks/<pick_id>/result")
def set_result(pick_id):
    if not is_admin():
        return jsonify(error="No"), 401
    result = (request.get_json(silent=True) or {}).get("result")
    with lock:
        rows = read("picks.json")
        row = next((p for p in rows if p["id"] == pick_id), None)
        if not row:
            return jsonify(error="Not found"), 404
        row["result"] = result
        write("picks.json", rows)
    return jsonify(row)

@app.get("/api/admin/requests")
def list_requests():
    if not is_admin():
        return jsonify(error="No"), 401
    status = request.args.get("status", "pending")
    return jsonify([r for r in read("requests.json") if r["status"] == status])

@app.post("/api/admin/requests/<rid>/<action>")
def review_request(rid, action):
    if not is_admin():
        return jsonify(error="No"), 401
    if action not in ("approve", "reject"):
        return jsonify(error="Use approve or reject"), 400
    with lock:
        reqs = read("requests.json")
        r = next((x for x in reqs if x["id"] == rid), None)
        if not r:
            return jsonify(error="Not found"), 404
        if r["status"] != "pending" and not (action == "reject" and r["status"] == "approved"):
            return jsonify(error="Already " + r["status"]), 400
        if action == "reject" and r["status"] == "approved":  # take back access already given
            vs = read("vouchers.json")
            write("vouchers.json", [v for v in vs if v["code"] != "REQ" + r["id"]])
        if action == "approve":
            vouchers = read("vouchers.json")
            vouchers.append({"code": "REQ" + r["id"], "plan": r["plan"], "createdAt": now_ms(),
                             "activatedAt": now_ms(), "userId": r["userId"]})
            write("vouchers.json", vouchers)
        r["status"] = "approved" if action == "approve" else "rejected"
        write("requests.json", reqs)
    return jsonify(r)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f"Running on http://localhost:{port}")
    app.run(host="127.0.0.1", port=port)