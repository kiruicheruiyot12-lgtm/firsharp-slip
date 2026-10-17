"""One-time Telegram setup. Finds your chat ID for you and saves everything to .env."""
import json, os, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(BASE, ".env")

def api(token, method, payload=None):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode() if payload else None,
        headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode()).get("description", "")
        except Exception:
            desc = ""
        return {"ok": False, "description": f"{e.code} {desc}".strip()}
    except Exception as e:
        return {"ok": False, "description": str(e)}

def save_env(values):
    lines = []
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8") as f:
            lines = f.read().splitlines()
    for key, val in values.items():
        lines = [l for l in lines if not l.startswith(key + "=")]
        lines.append(f"{key}={val}")
    with open(ENV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def main():
    print("\n=== Telegram setup ===\n")
    if os.path.exists(os.path.join(BASE, "env")) and not os.path.exists(ENV):
        print("Note: you have a file called 'env' without the dot. This script will create '.env' for you.\n")
    token = input("Paste your bot token from @BotFather, then press Enter: ").strip()
    me = api(token, "getMe")
    if not me.get("ok"):
        print("\nTelegram did not accept that token:", me.get("description"))
        print("Copy it again: @BotFather > /mybots > your bot > API Token.")
        return
    name = me["result"]["username"]
    print(f"\nBot found: @{name}")
    input(f"Now open @{name} in Telegram and press START (or send 'hi').\n"
          "Then come back here and press Enter... ")
    chats = {}
    for u in api(token, "getUpdates").get("result", []):
        ch = (u.get("message") or {}).get("chat") or {}
        if ch.get("type") == "private":
            chats[ch["id"]] = ch.get("first_name") or ch.get("username") or "unknown"
    if not chats:
        print("\nNo message from you was found. Open the bot, press START, wait a few seconds,")
        print("then run this script again.")
        return
    ids = list(chats)
    if len(ids) > 1:
        print("\nSeveral people have messaged this bot:")
        for i, cid in enumerate(ids, 1):
            print(f"  {i}. {chats[cid]} ({cid})")
        chat_id = ids[int(input("Type the number that is you: ")) - 1]
    else:
        chat_id = ids[0]
    print(f"\nSending a test message to {chats[chat_id]}...")
    r = api(token, "sendMessage", {"chat_id": chat_id,
            "text": "Sharp Slip is connected. Voucher codes will arrive here."})
    if not r.get("ok"):
        print("The test message failed:", r.get("description"))
        return
    save_env({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id})
    print("\nDone! Settings saved to .env.")
    print("Now stop the website (Ctrl+C) and start it again so it picks up the new settings.")

if __name__ == "__main__":
    main()