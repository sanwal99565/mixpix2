"""Professional-Conversion Account Bot — SINGLE FILE (sab kuch isi me).

Chalane ke liye:
    python profbot\\bot.py

SETUP (ek baar):
  1) Telegram par @BotFather se NAYA bot banao → token copy karo.
  2) Project root me .env file banao (repo me `.env.example` dekh lo):
       BOT_TOKEN=<token>
       ADMIN_IDS=<apni telegram numeric id>     # bot ko /myid bhejo to pata chal jayegi
     (Ya hosting platform par yahi env vars set kar do.)
  3) Chalao:  python profbot\\bot.py

  NOTE: .env file .gitignore me hai — GitHub par push nahi hogi (token safe rahega).

Flow:
  ADMIN : /admin panel → txt file bhejkar accounts add, /stats, /get (done txt),
          /errors (problem txt), /users, /adduser <id>, /removeuser <id>, /release <id>
  USER  : /start → (admin approve karega) → "Get Account" → id+pass (copy) →
          "Get OTP" (jab chahe, task done hone tak) → "Task Done" → next account
          ("Problem" dabane par account admin ko submit ho jata hai)

Sab kuch ek hi file: config + TOTP + Telegram API + SQLite storage + bot logic.
Sirf Python stdlib use hoti hai (urllib, sqlite3, hmac...) — koi pip install nahi.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import struct
import threading
import time
import traceback
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# ================== CONFIG ==================
BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path):
    """Chhota .env loader (koi dependency nahi). Jo pehle se set ho use override nahi karta."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


# .env dhoondo — profbot/.env aur project root .env (dono optional).
# Hosting par seedhe platform ke env vars (BOT_TOKEN, ADMIN_IDS) set kar dena.
_load_env_file(BASE_DIR / ".env")
_load_env_file(BASE_DIR.parent / ".env")

# Naya bot token (BotFather) — .env / env var se
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Admin telegram numeric ids — comma ya space se alag kar sakte ho
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", "")) if x.strip().isdigit()}

DATA_DIR = BASE_DIR / "data"
EXPORT_DIR = DATA_DIR / "exports"
DATA_DIR.mkdir(exist_ok=True)
EXPORT_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "profbot.db"

# ---- OTP ----
OTP_PERIOD = 30     # seconds (standard TOTP)
OTP_DIGITS = 6


# ================== TOTP (RFC 6238) ==================
def base32_decode(secret: str) -> bytes:
    """Base32 secret → bytes. Spaces/padding/grouping handle karta hai."""
    s = (secret or "").strip().replace(" ", "").replace("-", "").upper()
    s = s.rstrip("=")
    if not s:
        raise ValueError("empty 2FA secret")
    pad = "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s + pad)


def totp(secret: str, digits: int = None, period: int = None, when: float = None) -> str:
    """Current OTP string (default 6 digits / 30s)."""
    digits = digits or OTP_DIGITS
    period = period or OTP_PERIOD
    key = base32_decode(secret)
    counter = int((when if when is not None else time.time()) // period)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def totp_with_remaining(secret: str, digits: int = None, period: int = None):
    """Return (otp, seconds_remaining) — user ko batane ke liye kitna time bacha hai."""
    period = period or OTP_PERIOD
    now = time.time()
    remaining = period - int(now % period)
    return totp(secret, digits, period, now), remaining


# ================== TELEGRAM API CLIENT ==================
class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    # ---------- core ----------
    def call(self, method: str, params: dict = None, timeout: int = 60):
        url = f"{self.base}/{method}"
        data = None
        headers = {}
        if params is not None:
            data = json.dumps(params).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if not res.get("ok"):
            raise TelegramError(f"{method} failed: {res}")
        return res.get("result")

    # ---------- updates ----------
    def get_updates(self, offset=None, timeout: int = 30):
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + 20)

    def get_me(self):
        return self.call("getMe")

    # ---------- sending ----------
    def send_message(self, chat_id, text: str, reply_markup: dict = None, parse_mode: str = None):
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self.call("sendMessage", params)

    def edit_message_text(self, chat_id, message_id, text: str, reply_markup: dict = None,
                          parse_mode: str = None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        try:
            return self.call("editMessageText", params)
        except TelegramError:
            return None

    def answer_callback(self, callback_id, text: str = None, show_alert: bool = False):
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        if show_alert:
            params["show_alert"] = True
        try:
            return self.call("answerCallbackQuery", params)
        except TelegramError:
            return None

    def send_chat_action(self, chat_id, action: str = "typing"):
        try:
            return self.call("sendChatAction", {"chat_id": chat_id, "action": action})
        except TelegramError:
            return None

    # ---------- files ----------
    def get_file_path(self, file_id: str) -> str:
        res = self.call("getFile", {"file_id": file_id})
        return res["file_path"]

    def download_file(self, file_id: str) -> bytes:
        fp = self.get_file_path(file_id)
        url = f"{self.file_base}/{fp}"
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.read()

    def send_document(self, chat_id, file_path, caption: str = None):
        file_path = Path(file_path)
        content = file_path.read_bytes()
        boundary = "----profbot" + uuid.uuid4().hex

        def _field(name, value):
            return (f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n").encode("utf-8")

        body = b""
        body += _field("chat_id", str(chat_id))
        if caption:
            body += _field("caption", caption)
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="document"; '
                 f'filename="{file_path.name}"\r\n'
                 f"Content-Type: text/plain\r\n\r\n").encode("utf-8")
        body += content
        body += f"\r\n--{boundary}--\r\n".encode("utf-8")

        req = urllib.request.Request(
            f"{self.base}/sendDocument", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if not res.get("ok"):
            raise TelegramError(f"sendDocument failed: {res}")
        return res.get("result")


tg = Telegram(BOT_TOKEN)


# ================== STORAGE (SQLite) ==================
# Tables:
#   users    → telegram users (approval + display info)
#   accounts → account pool (username/password/2FA secret) + status/assignment
# Account status flow:  available → assigned → done → archived

_db_lock = threading.Lock()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def db_init():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id  INTEGER PRIMARY KEY,
                username     TEXT,
                first_name   TEXT,
                approved     INTEGER DEFAULT 0,
                blocked      INTEGER DEFAULT 0,
                requested_at TEXT,
                added_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                username     TEXT UNIQUE,
                password     TEXT,
                twofa_secret TEXT,
                session_id   TEXT,
                email        TEXT,
                status       TEXT DEFAULT 'available',
                assigned_to  INTEGER,
                assigned_at  TEXT,
                done_by      INTEGER,
                done_at      TEXT,
                error_by     INTEGER,
                error_at     TEXT,
                added_at     TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
            CREATE INDEX IF NOT EXISTS idx_accounts_assigned ON accounts(assigned_to);
            """
        )


# ---------- users ----------
def upsert_user(tid: int, username: str = "", first_name: str = ""):
    with _db_lock, _conn() as c:
        row = c.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (tid,)).fetchone()
        if row:
            c.execute("UPDATE users SET username=?, first_name=? WHERE telegram_id=?",
                      (username, first_name, tid))
        else:
            c.execute(
                "INSERT INTO users(telegram_id, username, first_name, approved, requested_at, added_at) "
                "VALUES(?,?,?,0,?,?)",
                (tid, username, first_name, now_str(), now_str()),
            )


def get_user(tid: int):
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
        return dict(r) if r else None


def is_approved(tid: int) -> bool:
    with _conn() as c:
        r = c.execute("SELECT approved FROM users WHERE telegram_id=?", (tid,)).fetchone()
        return bool(r and r["approved"])


def set_approved(tid: int, approved: bool, username: str = "", first_name: str = ""):
    with _db_lock, _conn() as c:
        row = c.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (tid,)).fetchone()
        if row:
            c.execute("UPDATE users SET approved=?, blocked=0 WHERE telegram_id=?",
                      (1 if approved else 0, tid))
        else:
            c.execute(
                "INSERT INTO users(telegram_id, username, first_name, approved, added_at) "
                "VALUES(?,?,?,?,?)",
                (tid, username, first_name, 1 if approved else 0, now_str()),
            )


def remove_user(tid: int) -> bool:
    with _db_lock, _conn() as c:
        cur = c.execute("DELETE FROM users WHERE telegram_id=?", (tid,))
        return cur.rowcount > 0


def list_users():
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY approved DESC, added_at").fetchall()
        return [dict(r) for r in rows]


# ---------- accounts ----------
def add_accounts(rows: list) -> tuple:
    """rows = [{username,password,twofa_secret,session_id,email}] → (added, skipped)."""
    added = skipped = 0
    with _db_lock, _conn() as c:
        for r in rows:
            u = (r.get("username") or "").strip().lstrip("@")
            p = (r.get("password") or "").strip()
            if not u or not p:
                skipped += 1
                continue
            try:
                cur = c.execute(
                    "INSERT INTO accounts(username, password, twofa_secret, session_id, email, "
                    "status, added_at) VALUES(?,?,?,?,?, 'available', ?)",
                    (u, p, (r.get("twofa_secret") or "").strip(),
                     (r.get("session_id") or "").strip(),
                     (r.get("email") or "").strip(), now_str()),
                )
                if cur.rowcount:
                    added += 1
                else:
                    skipped += 1
            except sqlite3.IntegrityError:
                skipped += 1
    return added, skipped


def stats() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM accounts GROUP BY status").fetchall()
        d = {"available": 0, "assigned": 0, "done": 0, "archived": 0, "error": 0}
        for r in rows:
            d[r["status"]] = r["n"]
        d["total"] = sum(d.values())
        return d


def assign_account(tid: int):
    """Ek available account user ko do (atomic). None agar available nahi."""
    with _db_lock, _conn() as c:
        r = c.execute("SELECT * FROM accounts WHERE status='available' ORDER BY id LIMIT 1").fetchone()
        if not r:
            return None
        c.execute("UPDATE accounts SET status='assigned', assigned_to=?, assigned_at=? WHERE id=?",
                  (tid, now_str(), r["id"]))
        d = dict(r)
        d["status"] = "assigned"
        d["assigned_to"] = tid
        return d


def get_active_for_user(tid: int):
    """User ke paas jo abhi assigned account hai (ek hi hoga)."""
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM accounts WHERE status='assigned' AND assigned_to=? ORDER BY id LIMIT 1",
            (tid,),
        ).fetchone()
        return dict(r) if r else None


def mark_done(username: str, tid: int) -> bool:
    with _db_lock, _conn() as c:
        cur = c.execute(
            "UPDATE accounts SET status='done', done_at=?, done_by=?, assigned_to=NULL "
            "WHERE username=? AND status='assigned'",
            (now_str(), tid, username),
        )
        return cur.rowcount > 0


def release_user(tid: int) -> int:
    """User ka assigned account wapas available kar do."""
    with _db_lock, _conn() as c:
        cur = c.execute(
            "UPDATE accounts SET status='available', assigned_to=NULL, assigned_at=NULL "
            "WHERE status='assigned' AND assigned_to=?",
            (tid,),
        )
        return cur.rowcount


def report_problem(username: str, tid: int) -> bool:
    """User ne 'problem/not complete' report kiya → account admin ko submit,
    user ka access khatam (status='error')."""
    with _db_lock, _conn() as c:
        cur = c.execute(
            "UPDATE accounts SET status='error', error_at=?, error_by=?, assigned_to=NULL "
            "WHERE username=? AND status='assigned'",
            (now_str(), tid, username),
        )
        return cur.rowcount > 0


def last_finished(tid: int):
    """User ka sabse recent khatam hua account (done ya error)."""
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM accounts WHERE done_by=? OR error_by=? "
            "ORDER BY COALESCE(done_at, error_at) DESC LIMIT 1",
            (tid, tid),
        ).fetchone()
        return dict(r) if r else None


def list_errors():
    with _conn() as c:
        rows = c.execute("SELECT * FROM accounts WHERE status='error' ORDER BY error_at").fetchall()
        return [dict(r) for r in rows]


def archive_errors() -> int:
    with _db_lock, _conn() as c:
        cur = c.execute("UPDATE accounts SET status='archived' WHERE status='error'")
        return cur.rowcount


def list_done():
    with _conn() as c:
        rows = c.execute("SELECT * FROM accounts WHERE status='done' ORDER BY done_at").fetchall()
        return [dict(r) for r in rows]


def archive_done() -> int:
    with _db_lock, _conn() as c:
        cur = c.execute("UPDATE accounts SET status='archived' WHERE status='done'")
        return cur.rowcount


def per_user_done() -> list:
    """[(telegram_id, username, first_name, completed_count), ...]"""
    with _conn() as c:
        rows = c.execute(
            """
            SELECT a.done_by AS tid, u.username AS uname, u.first_name AS fname, COUNT(*) AS n
            FROM accounts a
            LEFT JOIN users u ON u.telegram_id = a.done_by
            WHERE a.status IN ('done','archived') AND a.done_by IS NOT NULL
            GROUP BY a.done_by
            ORDER BY n DESC
            """
        ).fetchall()
        return [(r["tid"], r["uname"], r["fname"], r["n"]) for r in rows]


def current_holders() -> list:
    """[(telegram_id, username, first_name, count), ...] — abhi kis user ke paas kitne."""
    with _conn() as c:
        rows = c.execute(
            """
            SELECT a.assigned_to AS tid, u.username AS uname, u.first_name AS fname, COUNT(*) AS n
            FROM accounts a
            LEFT JOIN users u ON u.telegram_id = a.assigned_to
            WHERE a.status='assigned'
            GROUP BY a.assigned_to
            ORDER BY n DESC
            """
        ).fetchall()
        return [(r["tid"], r["uname"], r["fname"], r["n"]) for r in rows]


# ---------- txt parser ----------
def _strip_prefix(s: str) -> str:
    """Line ke aage ka emoji/symbol hata do: '👤 User: x' → 'User: x'."""
    return re.sub(r"^[^A-Za-z0-9]+", "", (s or "").strip()).strip()


def parse_accounts_text(text: str) -> list:
    """Admin ki txt ko accounts list me convert karo. Formats support:

    A) test.py format (emoji ke saath ya bina):
         👤 User: @username
         🔑 Pass: password
         🔐 2FA Key: BASE32
         🆔 Session ID: ...      (optional)
         📧 Email: ...           (optional)
    B) simple pipe:  username|password|2fa   (aur  username:password)
    """
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    if not lines:
        return []

    has_kv = any(_strip_prefix(ln).lower().startswith(("user:", "pass:")) for ln in lines)
    out = []

    if has_kv:
        cur = {}

        def flush():
            nonlocal cur
            if cur.get("username") and cur.get("password"):
                out.append({
                    "username": cur["username"],
                    "password": cur["password"],
                    "twofa_secret": cur.get("twofa_secret", ""),
                    "session_id": cur.get("session_id", ""),
                    "email": cur.get("email", ""),
                })
            cur = {}

        for ln in lines:
            n = _strip_prefix(ln)
            if not n:
                continue
            low = n.lower()
            if low.startswith("user:"):
                if cur.get("username"):
                    flush()
                cur["username"] = n.split(":", 1)[1].strip().lstrip("@")
            elif low.startswith("pass:"):
                cur["password"] = n.split(":", 1)[1].strip()
            elif low.startswith("2fa key:") or low.startswith("2fa:"):
                cur["twofa_secret"] = n.split(":", 1)[1].strip()
            elif low.startswith("session id:"):
                v = n.split(":", 1)[1].strip()
                if v and v.upper() != "N/A":
                    cur["session_id"] = v
            elif low.startswith("email:"):
                cur["email"] = n.split(":", 1)[1].strip()
        flush()
        return out

    # pipe / colon format
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if "|" in s:
            parts = [p.strip() for p in s.split("|")]
            if len(parts) >= 2 and parts[0] and parts[1]:
                out.append({
                    "username": parts[0].lstrip("@"),
                    "password": parts[1],
                    "twofa_secret": parts[2] if len(parts) > 2 else "",
                    "session_id": parts[3] if len(parts) > 3 else "",
                    "email": "",
                })
        elif s.count(":") == 1:
            u, p = s.split(":", 1)
            if u.strip() and p.strip():
                out.append({"username": u.strip().lstrip("@"), "password": p.strip(),
                            "twofa_secret": "", "session_id": "", "email": ""})
    return out


# ================== BOT LOGIC ==================
def is_admin(tid: int) -> bool:
    return tid in ADMIN_IDS


def can_work(tid: int) -> bool:
    return is_admin(tid) or is_approved(tid)


def notify_admins(text: str, reply_markup: dict = None):
    for aid in ADMIN_IDS:
        try:
            tg.send_message(aid, text, reply_markup=reply_markup)
        except Exception as e:
            print(f"[!] admin notify failed ({aid}): {e}")


def name_of(u: dict) -> str:
    if not u:
        return "?"
    return u.get("username") or u.get("first_name") or str(u.get("telegram_id"))


# ---------- keyboards ----------
def user_kb(tid: int) -> dict:
    has = get_active_for_user(tid) is not None
    rows = []
    if has:
        rows.append([{"text": "🔐 Get OTP", "callback_data": "u:otp"},
                     {"text": "👤 My Account", "callback_data": "u:myacc"}])
        rows.append([{"text": "✅ Task Done", "callback_data": "u:done"},
                     {"text": "⚠️ Problem", "callback_data": "u:problem"}])
    else:
        rows.append([{"text": "📥 Get Account", "callback_data": "u:get"}])
        rows.append([{"text": "👤 My Account", "callback_data": "u:myacc"}])
    return {"inline_keyboard": rows}


def admin_kb() -> dict:
    return {"inline_keyboard": [
        [{"text": "📊 Stats", "callback_data": "a:stats"},
         {"text": "⬇️ Get Done", "callback_data": "a:get"}],
        [{"text": "⚠️ Get Problems", "callback_data": "a:errors"},
         {"text": "👥 Users", "callback_data": "a:users"}],
        [{"text": "📤 Upload Help", "callback_data": "a:upload"}],
    ]}


def approve_kb(tid: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"a:approve:{tid}"},
        {"text": "❌ Reject", "callback_data": f"a:reject:{tid}"},
    ]]}


# ---------- user actions ----------
def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def account_text(acc: dict) -> str:
    """User ko account (tap-to-copy) — test.py jaisa format."""
    return (f"👤 User: @<code>{esc(acc['username'])}</code>\n"
            f"🔑 Pass: <code>{esc(acc['password'])}</code>\n")


def _account_full_text(acc: dict) -> str:
    """Admin ke liye poore details (plain text)."""
    lines = [f"👤 User: @{acc.get('username', '')}", f"🔑 Pass: {acc.get('password', '')}"]
    if acc.get("session_id"):
        lines.append(f"🆔 Session ID: {acc['session_id']}")
    if acc.get("twofa_secret"):
        lines.append(f"🔐 2FA Key: {acc['twofa_secret']}")
    if acc.get("email"):
        lines.append(f"📧 Email: {acc['email']}")
    return "\n".join(lines)


def do_get_account(tid: int, chat_id: int):
    if not can_work(tid):
        tg.send_message(chat_id, "⛔ Aapko access nahi hai. Admin se approve karwao — /start bhejo.")
        return
    active = get_active_for_user(tid)
    if active:
        tg.send_message(
            chat_id,
            "⚠️ Aapke paas pehle se ek account active hai. Pehle use '✅ Task Done' karo.\n\n"
            + account_text(active),
            reply_markup=user_kb(tid),
            parse_mode="HTML",
        )
        return
    acc = assign_account(tid)
    if not acc:
        tg.send_message(chat_id, "📭 Abhi koi account available nahi hai. Admin se naye accounts daalne ko bolo.",
                        reply_markup=user_kb(tid))
        return
    tg.send_message(
        chat_id,
        "🎯 Account mil gaya\n\n"
        + account_text(acc)
        + "\n2FA OTP chahiye to '🔐 Get OTP' dabao (jab tak chahiye).\n"
          "Kaam poorA hone par '✅ Task Done' dabao.",
        reply_markup=user_kb(tid),
        parse_mode="HTML",
    )


def do_otp(tid: int, chat_id: int):
    active = get_active_for_user(tid)
    if not active:
        tg.send_message(chat_id, "Koi active account nahi. Pehle '📥 Get Account' dabao.",
                        reply_markup=user_kb(tid))
        return
    secret = (active.get("twofa_secret") or "").strip()
    if not secret:
        tg.send_message(chat_id, f"ℹ️ @{active['username']} me 2FA set nahi hai (koi key nahi).",
                        reply_markup=user_kb(tid))
        return
    try:
        code, remaining = totp_with_remaining(secret)
    except Exception as e:
        tg.send_message(chat_id, f"[!] OTP generate nahi hua: {e}", reply_markup=user_kb(tid))
        return
    tg.send_message(
        chat_id,
        f"🔐 OTP: <code>{code}</code>\n"
        f"⏳ Valid ~{remaining}s\n\n"
        f"(@{esc(active['username'])} ke liye — turant use karo, 30s me badal jayega)",
        reply_markup=user_kb(tid),
        parse_mode="HTML",
    )


def do_done(tid: int, chat_id: int):
    active = get_active_for_user(tid)
    if not active:
        tg.send_message(chat_id, "Koi active account nahi. Pehle '📥 Get Account' dabao.",
                        reply_markup=user_kb(tid))
        return
    uname = active["username"]
    if mark_done(uname, tid):
        tg.send_message(
            chat_id,
            f"✅ @{uname} done mark ho gaya!\n\n"
            "Ab is account ka aapka access khatam — ID/pass/OTP band.\n"
            "Naya account lene ke liye '📥 Get Account' dabao.",
            reply_markup=user_kb(tid),
        )
        notify_admins(f"✅ @{uname} → done by {name_of(get_user(tid))} ({tid})")
    else:
        tg.send_message(chat_id, "⚠️ Done mark nahi ho paaya (account assigned nahi hai?).",
                        reply_markup=user_kb(tid))


def do_report_problem(tid: int, chat_id: int):
    """Task me problem → account admin ko submit, user ka access khatam."""
    active = get_active_for_user(tid)
    if not active:
        tg.send_message(chat_id, "Koi active account nahi. Pehle '📥 Get Account' dabao.",
                        reply_markup=user_kb(tid))
        return
    uname = active["username"]
    if report_problem(uname, tid):
        tg.send_message(
            chat_id,
            f"⚠️ @{uname} admin ko submit kar diya (problem / not complete).\n\n"
            "Ab is account ka aapka access khatam. Naya account lene ke liye '📥 Get Account' dabao.",
            reply_markup=user_kb(tid),
        )
        notify_admins(
            "⚠️ Problem / Not-complete report\n"
            f"Reported by: {name_of(get_user(tid))} ({tid})\n\n"
            + _account_full_text(active)
        )
    else:
        tg.send_message(chat_id, "⚠️ Report nahi ho paaya.", reply_markup=user_kb(tid))


def do_my_account(tid: int, chat_id: int):
    active = get_active_for_user(tid)
    if active:
        tg.send_message(chat_id, "👤 Your current account\n\n" + account_text(active),
                        reply_markup=user_kb(tid), parse_mode="HTML")
        return
    last = last_finished(tid)
    if last and last.get("done_by") == tid:
        tg.send_message(chat_id, f"✅ Completed: @{esc(last['username'])}",
                        reply_markup=user_kb(tid), parse_mode="HTML")
        return
    if last:
        tg.send_message(chat_id, f"⚠️ Reported (admin ko submit): @{esc(last['username'])}",
                        reply_markup=user_kb(tid), parse_mode="HTML")
        return
    tg.send_message(chat_id, "Aapke paas abhi koi account nahi hai.", reply_markup=user_kb(tid))


# ---------- admin actions ----------
def do_stats(chat_id: int):
    s = stats()
    per = per_user_done()
    holders = current_holders()
    lines = [
        "📊 BOT STATS",
        f"📦 Total: {s['total']}",
        f"📥 Available: {s['available']}",
        f"🔄 In-progress (assigned): {s['assigned']}",
        f"✅ Done: {s['done']}",
        f"⚠️ Problems (error): {s['error']}",
        f"🗄 Archived (exported): {s['archived']}",
        "",
        "👥 Completed per user:",
    ]
    if per:
        for tid, uname, fname, n in per:
            lines.append(f"  • {uname or fname or tid} ({tid}) → {n}")
    else:
        lines.append("  (abhi kisi ne complete nahi kiya)")
    lines.append("")
    lines.append("🔄 Currently holding:")
    if holders:
        for tid, uname, fname, n in holders:
            lines.append(f"  • {uname or fname or tid} ({tid}) → {n}")
    else:
        lines.append("  (koi account kisi ke paas nahi)")
    tg.send_message(chat_id, "\n".join(lines), reply_markup=admin_kb())


def do_get_done(chat_id: int):
    rows = list_done()
    if not rows:
        tg.send_message(chat_id, "📭 Abhi koi 'done' account nahi hai.", reply_markup=admin_kb())
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    fn = EXPORT_DIR / f"done_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    out = [f"FURMAN DONE EXPORT — {stamp}", f"Total: {len(rows)} account(s)", "=" * 50]
    for i, r in enumerate(rows, 1):
        out.append(f"\n[{i}]")
        out.append(f"👤 User: @{r['username']}")
        out.append(f"🔑 Pass: {r['password']}")
        if r.get("session_id"):
            out.append(f"🆔 Session ID: {r['session_id']}")
        if r.get("twofa_secret"):
            out.append(f"🔐 2FA Key: {r['twofa_secret']}")
        if r.get("email"):
            out.append(f"📧 Email: {r['email']}")
        out.append(f"✅ Done by: {r.get('done_by')}")
        out.append(f"🕒 Done at: {r.get('done_at')}")
    fn.write_text("\n".join(out), encoding="utf-8")
    try:
        tg.send_document(chat_id, fn, caption=f"⬇️ {len(rows)} done account(s)\n🕒 {stamp}")
    except Exception as e:
        tg.send_message(chat_id, f"[!] file bhejne me error: {e}")
        return
    n = archive_done()
    tg.send_message(chat_id, f"🗄 {n} account(s) archived (ab active list se hat gaye).",
                    reply_markup=admin_kb())


def do_get_errors(chat_id: int):
    """Problem/not-complete accounts admin ko txt me do, phir archive."""
    rows = list_errors()
    if not rows:
        tg.send_message(chat_id, "📭 Abhi koi problem/error account nahi hai.", reply_markup=admin_kb())
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    fn = EXPORT_DIR / f"errors_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    out = [f"FURMAN PROBLEM/ERROR EXPORT — {stamp}", f"Total: {len(rows)} account(s)", "=" * 50]
    for i, r in enumerate(rows, 1):
        out.append(f"\n[{i}]")
        out.append(f"👤 User: @{r['username']}")
        out.append(f"🔑 Pass: {r['password']}")
        if r.get("session_id"):
            out.append(f"🆔 Session ID: {r['session_id']}")
        if r.get("twofa_secret"):
            out.append(f"🔐 2FA Key: {r['twofa_secret']}")
        if r.get("email"):
            out.append(f"📧 Email: {r['email']}")
        out.append(f"⚠️ Reported by: {r.get('error_by')}")
        out.append(f"🕒 Reported at: {r.get('error_at')}")
    fn.write_text("\n".join(out), encoding="utf-8")
    try:
        tg.send_document(chat_id, fn, caption=f"⚠️ {len(rows)} problem account(s)\n🕒 {stamp}")
    except Exception as e:
        tg.send_message(chat_id, f"[!] file bhejne me error: {e}")
        return
    n = archive_errors()
    tg.send_message(chat_id, f"🗄 {n} problem account(s) archived.", reply_markup=admin_kb())


def do_users(chat_id: int):
    users = list_users()
    if not users:
        tg.send_message(chat_id, "Koi user nahi. (Admin ko chhod kar)", reply_markup=admin_kb())
        return
    per = {tid: n for tid, _, _, n in per_user_done()}
    lines = ["👥 USERS", ""]
    for u in users:
        tid = u["telegram_id"]
        mark = "✅" if u["approved"] else "⏳"
        tag = " (admin)" if is_admin(tid) else ""
        lines.append(f"{mark} {name_of(u)} — {tid}{tag}\n     completed: {per.get(tid, 0)}")
    lines.append("\n✅ = approved, ⏳ = pending")
    tg.send_message(chat_id, "\n".join(lines), reply_markup=admin_kb())


def do_upload_help(chat_id: int):
    tg.send_message(
        chat_id,
        "📤 Accounts add karna\n\n"
        "Do tarike — dono chalenge:\n"
        "• Ek `.txt` file bhejo (document ke roop me), YA\n"
        "• Account ka text seedha yahan paste karke bhej do.\n\n"
        "Format (emoji ke saath ya bina):\n"
        "   👤 User: @username\n"
        "   🔑 Pass: password\n"
        "   🔐 2FA Key: BASE32SECRET\n"
        "   🆔 Session ID: ...      (optional)\n"
        "   📧 Email: ...           (optional)\n\n"
        "2) Simple pipe:\n"
        "   username|password|2fa_secret\n\n"
        "Ek hi file me bahut accounts ho sakte hain. Duplicate username skip ho jayega.",
        reply_markup=admin_kb(),
    )


# ---------- routing ----------
def handle_message(msg: dict):
    chat = msg.get("chat", {})
    tid = chat.get("id")
    if tid is None:
        return
    frm = msg.get("from", {})
    upsert_user(tid, frm.get("username", ""), frm.get("first_name", ""))

    # admin sends a txt file → ingest accounts
    if msg.get("document"):
        if not is_admin(tid):
            tg.send_message(tid, "⛔ Sirf admin accounts upload kar sakta hai.")
            return
        ingest_document(tid, msg["document"])
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return
    cmd = text.split()[0].lower().split("@")[0]
    args = text.split()[1:]

    # ---- admin commands ----
    if is_admin(tid):
        # admin ne seedha text paste kiya (account data) → add karo
        if not text.startswith("/") and looks_like_accounts(text):
            ingest_text(tid, text, source="pasted text")
            return
        if cmd in ("/admin", "/panel"):
            tg.send_message(tid, "🛠 Admin Panel", reply_markup=admin_kb())
            return
        if cmd == "/stats":
            do_stats(tid)
            return
        if cmd in ("/get", "/done"):
            do_get_done(tid)
            return
        if cmd in ("/errors", "/problems"):
            do_get_errors(tid)
            return
        if cmd == "/users":
            do_users(tid)
            return
        if cmd == "/upload":
            do_upload_help(tid)
            return
        if cmd == "/adduser" and args:
            try:
                uid = int(args[0])
            except ValueError:
                tg.send_message(tid, "Usage: /adduser <telegram_id>")
                return
            set_approved(uid, True)
            tg.send_message(tid, f"✅ User {uid} approved.")
            try:
                tg.send_message(uid, "✅ Aapko access mil gaya! /start dabao aur account lo.")
            except Exception:
                pass
            return
        if cmd == "/removeuser" and args:
            try:
                uid = int(args[0])
            except ValueError:
                tg.send_message(tid, "Usage: /removeuser <telegram_id>")
                return
            remove_user(uid)
            tg.send_message(tid, f"🗑 User {uid} hata diya.")
            return
        if cmd == "/release" and args:
            try:
                uid = int(args[0])
            except ValueError:
                tg.send_message(tid, "Usage: /release <telegram_id>")
                return
            n = release_user(uid)
            tg.send_message(tid, f"♻️ {n} account(s) wapas available kiye (user {uid}).")
            return

    # ---- basic commands ----
    if cmd in ("/start", "/help"):
        on_start(tid, frm)
        return
    if cmd in ("/myid", "/id"):
        tg.send_message(tid, f"🆔 Aapki Telegram ID: {tid}")
        return
    if cmd in ("/get", "/getaccount"):
        do_get_account(tid, tid)
        return
    if cmd == "/otp":
        do_otp(tid, tid)
        return
    if cmd == "/done":
        do_done(tid, tid)
        return
    if cmd in ("/myaccount", "/my"):
        do_my_account(tid, tid)
        return

    # fallback
    if not can_work(tid):
        tg.send_message(tid, "⛔ Access nahi hai. /start bhejo (admin approve karega).")
    else:
        tg.send_message(tid, "Samajh nahi aaya. /start dabao.", reply_markup=user_kb(tid))


def on_start(tid: int, frm: dict):
    if is_admin(tid):
        tg.send_message(tid, "🛠 Admin Panel\n/stats /get /errors /users /adduser <id> /upload",
                        reply_markup=admin_kb())
        return
    if can_work(tid):
        tg.send_message(
            tid,
            "👋 Welcome!\n\n"
            "Yahan se Instagram account lekar uska 2FA OTP log le sakte ho aur professional "
            "banane ke baad 'Done' report kar sakte ho.\n\n"
            "Ek time par ek hi account milega — Done karne ke baad next milega.",
            reply_markup=user_kb(tid),
        )
        return
    # pending
    upsert_user(tid, frm.get("username", ""), frm.get("first_name", ""))
    tg.send_message(tid, "⏳ Aapka access request bhej diya gaya hai. Admin approve karega, phir try karo.")
    notify_admins(
        f"🔔 Access request\n{name_of(get_user(tid))} ({tid}) ne access maanga.",
        reply_markup=approve_kb(tid),
    )


def looks_like_accounts(text: str) -> bool:
    """Message me account data lagta hai? (User:/Pass: ya pipe format)"""
    t = text or ""
    if "|" in t and " " not in t.split("|", 1)[0].strip():
        return True
    for ln in t.splitlines():
        n = _strip_prefix(ln).lower()
        if n.startswith(("user:", "pass:")):
            return True
    return False


def ingest_text(tid: int, text: str, source: str = "text"):
    """Text (ya txt file) se accounts pool me add karo."""
    rows = parse_accounts_text(text)
    if not rows:
        tg.send_message(tid, f"⚠️ {source} me koi valid account nahi mila. Format check karo (/upload).")
        return
    added, skipped = add_accounts(rows)
    s = stats()
    tg.send_message(
        tid,
        f"✅ Upload done ({source})\n"
        f"➕ Added: {added}\n"
        f"⏭ Skipped (duplicate/invalid): {skipped}\n\n"
        f"📥 Available now: {s['available']} | 🔄 Assigned: {s['assigned']} | ✅ Done: {s['done']}",
        reply_markup=admin_kb(),
    )


def ingest_document(tid: int, doc: dict):
    name = doc.get("file_name", "file.txt")
    if not name.lower().endswith((".txt", ".csv")):
        tg.send_message(tid, f"⚠️ '{name}' txt nahi lagti. Phir bhi try kar raha hoon...")
    tg.send_chat_action(tid, "typing")
    try:
        raw = tg.download_file(doc["file_id"])
    except Exception as e:
        tg.send_message(tid, f"[!] file download fail: {e}")
        return
    ingest_text(tid, raw.decode("utf-8", errors="ignore"), source=name)


def handle_callback(cb: dict):
    data = cb.get("data", "")
    frm = cb.get("from", {})
    tid = frm.get("id")
    cb_id = cb.get("id")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id", tid)

    if data.startswith("u:"):
        action = data[2:]
        if not can_work(tid):
            tg.answer_callback(cb_id, "Access nahi hai. /start bhejo.", show_alert=True)
            return
        tg.answer_callback(cb_id)
        if action == "get":
            do_get_account(tid, chat_id)
        elif action == "otp":
            do_otp(tid, chat_id)
        elif action == "done":
            do_done(tid, chat_id)
        elif action == "problem":
            do_report_problem(tid, chat_id)
        elif action == "myacc":
            do_my_account(tid, chat_id)
        return

    if data.startswith("a:"):
        if not is_admin(tid):
            tg.answer_callback(cb_id, "Sirf admin.", show_alert=True)
            return
        parts = data.split(":")
        action = parts[1]
        if action == "stats":
            tg.answer_callback(cb_id)
            do_stats(chat_id)
        elif action == "get":
            tg.answer_callback(cb_id)
            do_get_done(chat_id)
        elif action == "errors":
            tg.answer_callback(cb_id)
            do_get_errors(chat_id)
        elif action == "users":
            tg.answer_callback(cb_id)
            do_users(chat_id)
        elif action == "upload":
            tg.answer_callback(cb_id)
            do_upload_help(chat_id)
        elif action in ("approve", "reject"):
            uid = int(parts[2])
            if action == "approve":
                set_approved(uid, True)
                tg.answer_callback(cb_id, "Approved ✅")
                tg.edit_message_text(chat_id, msg.get("message_id"), f"✅ Approved user {uid}")
                try:
                    tg.send_message(uid, "✅ Aapko access mil gaya! /start dabao.")
                except Exception:
                    pass
            else:
                remove_user(uid)
                tg.answer_callback(cb_id, "Rejected ❌")
                tg.edit_message_text(chat_id, msg.get("message_id"), f"❌ Rejected user {uid}")
                try:
                    tg.send_message(uid, "❌ Aapki request reject kar di gayi.")
                except Exception:
                    pass
        else:
            tg.answer_callback(cb_id)
        return

    tg.answer_callback(cb_id)


# ================== MAIN ==================
def main():
    if "PASTE" in BOT_TOKEN or not BOT_TOKEN.strip():
        print("=" * 60)
        print("SETUP NEEDED:")
        print("  1) @BotFather se NAYA bot banao → token copy karo")
        print("  2) Project root me .env banao (ya env var set karo):")
        print("       BOT_TOKEN=<token>")
        print("       ADMIN_IDS=<apni telegram id>       # /myid se pata chalti hai")
        print("  3) Phir chalao:  python profbot\\bot.py")
        print("=" * 60)
        return

    db_init()
    try:
        me = tg.get_me()
    except Exception as e:
        print(f"[!] bot token check fail: {e}")
        return
    print(f"[*] bot @{me.get('username')} chal raha hai. Admin ids: {sorted(ADMIN_IDS)}")
    print("[*] rukne ke liye Ctrl+C")

    offset = None
    while True:
        try:
            updates = tg.get_updates(offset, timeout=30)
        except KeyboardInterrupt:
            print("\n[*] bye")
            break
        except Exception as e:
            print(f"[!] getUpdates error: {e}")
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    handle_message(u["message"])
                elif "callback_query" in u:
                    handle_callback(u["callback_query"])
            except Exception:
                traceback.print_exc()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] bye")
