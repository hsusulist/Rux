import json
import time
import threading
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_CREDITS = 10.0
CREDIT_INTERVAL_MS = 6 * 60 * 60 * 1000  # 6 hours per credit

_lock = threading.Lock()


def _load(filename):
    path = DATA_DIR / filename
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(filename, data):
    path = DATA_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# ═══════════════════════════════════════════
#  USERS
# ═══════════════════════════════════════════

def save_user(user_id, email, password_hash, roblox_id=""):
    with _lock:
        users = _load("users.json")
        users[user_id] = {
            "id": user_id,
            "email": email,
            "password_hash": password_hash,
            "roblox_id": str(roblox_id) if roblox_id else "",
            "created_at": int(time.time()),
        }
        _save("users.json", users)


def get_user_by_id(user_id):
    with _lock:
        users = _load("users.json")
        return users.get(user_id)


def get_user_by_email(email):
    with _lock:
        users = _load("users.json")
        lowered = email.lower().strip()
        for uid, u in users.items():
            if u.get("email", "").lower() == lowered:
                return u
        return None


def get_user_by_roblox_id(roblox_id):
    with _lock:
        users = _load("users.json")
        rid = str(roblox_id)
        if not rid:
            return None
        for uid, u in users.items():
            if str(u.get("roblox_id", "")) == rid:
                return u
        return None


# ═══════════════════════════════════════════
#  SESSIONS  (auth tokens)
# ═══════════════════════════════════════════

def save_session(token, user_id):
    with _lock:
        sessions = _load("sessions.json")
        sessions[token] = {"user_id": user_id, "created_at": int(time.time())}
        _save("sessions.json", sessions)


def get_session(token):
    with _lock:
        sessions = _load("sessions.json")
        return sessions.get(token)


def delete_session(token):
    with _lock:
        sessions = _load("sessions.json")
        sessions.pop(token, None)
        _save("sessions.json", sessions)


# ═══════════════════════════════════════════
#  CREDITS
# ═══════════════════════════════════════════

def _regenerate(balance, last_updated, max_credit=MAX_CREDITS):
    now = int(time.time() * 1000)
    if last_updated > 0 and balance < max_credit:
        elapsed = now - last_updated
        if elapsed >= CREDIT_INTERVAL_MS:
            intervals = int(elapsed // CREDIT_INTERVAL_MS)
            add = min(float(intervals), max_credit - balance)
            balance = round(balance + add, 4)
            last_updated += intervals * CREDIT_INTERVAL_MS
    return balance, last_updated


def get_credits(user_id):
    with _lock:
        credits = _load("credits.json")
        if user_id not in credits:
            credits[user_id] = {"balance": MAX_CREDITS, "last_updated": 0, "max_credit": MAX_CREDITS}
            _save("credits.json", credits)
            return MAX_CREDITS, 0
        data = credits[user_id]
        balance = float(data.get("balance", 0))
        last_updated = data.get("last_updated", 0)
        max_credit = float(data.get("max_credit", MAX_CREDITS))
        balance, last_updated = _regenerate(balance, last_updated, max_credit)
        if balance != float(data.get("balance", 0)):
            credits[user_id] = {"balance": balance, "last_updated": last_updated, "max_credit": max_credit}
            _save("credits.json", credits)
        return balance, last_updated


def deduct_credits(user_id, amount):
    with _lock:
        credits = _load("credits.json")
        if user_id not in credits:
            credits[user_id] = {"balance": MAX_CREDITS, "last_updated": 0, "max_credit": MAX_CREDITS}
        data = credits[user_id]
        balance = float(data.get("balance", 0))
        last_updated = data.get("last_updated", 0)
        max_credit = float(data.get("max_credit", MAX_CREDITS))
        balance, last_updated = _regenerate(balance, last_updated, max_credit)
        balance = round(balance - amount, 6)
        credits[user_id] = {"balance": balance, "last_updated": last_updated, "max_credit": max_credit}
        _save("credits.json", credits)
        return balance, last_updated


def init_credits(user_id):
    with _lock:
        credits = _load("credits.json")
        if user_id not in credits:
            credits[user_id] = {"balance": MAX_CREDITS, "last_updated": 0, "max_credit": MAX_CREDITS}
            _save("credits.json", credits)



# ═══════════════════════════════════════════
#  PLUGIN CONNECTIONS
# ═══════════════════════════════════════════

def get_user_plugin(user_id):
    with _lock:
        plugins = _load("plugins.json")
        return plugins.get(user_id)


def save_user_plugin(user_id, plugin_id, session_id):
    with _lock:
        plugins = _load("plugins.json")
        plugins[user_id] = {"plugin_id": plugin_id, "session_id": session_id}
        _save("plugins.json", plugins)


def delete_user_plugin(user_id):
    with _lock:
        plugins = _load("plugins.json")
        plugins.pop(user_id, None)
        _save("plugins.json", plugins)


# ═══════════════════════════════════════════
#  CONVERSATIONS
# ═══════════════════════════════════════════

def get_conv_list(user_id):
    with _lock:
        return _load(f"conv_lists/{user_id}.json")


def get_conv(conv_id):
    with _lock:
        return _load(f"convs/{conv_id}.json")


def save_conv(user_id, conv_id, conv_data):
    with _lock:
        conv_list = _load(f"conv_lists/{user_id}.json")
        conv_list[conv_id] = {
            "id": conv_id,
            "title": conv_data.get("title", "Conversation"),
            "mode": conv_data.get("mode", "chat"),
            "model": conv_data.get("model", "sonnet"),
            "updatedAt": conv_data.get("updatedAt", 0),
        }
        _save(f"conv_lists/{user_id}.json", conv_list)
        _save(f"convs/{conv_id}.json", conv_data)


def delete_conv(user_id, conv_id):
    with _lock:
        conv_list = _load(f"conv_lists/{user_id}.json")
        conv_list.pop(conv_id, None)
        _save(f"conv_lists/{user_id}.json", conv_list)
        path = DATA_DIR / "convs" / f"{conv_id}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass

# ═══════════════════════════════════════════
#  ADMIN
# ═══════════════════════════════════════════

def get_all_users_with_credits():
    with _lock:
        users = _load("users.json")
        credits = _load("credits.json")
        result = []
        for uid, u in users.items():
            c = credits.get(uid, {"balance": MAX_CREDITS, "last_updated": 0})
            result.append({
                "id": u.get("id", uid),
                "email": u.get("email", ""),
                "password_hash": u.get("password_hash", ""),
                "roblox_id": u.get("roblox_id", ""),
                "created_at": u.get("created_at", 0),
                "balance": float(c.get("balance", 0)),
                "max_credit": float(c.get("max_credit", MAX_CREDITS)),
                "last_updated": c.get("last_updated", 0),
            })
        return result


def set_user_credits(user_id, balance, max_credit):
    with _lock:
        credits = _load("credits.json")
        if user_id not in credits:
            credits[user_id] = {"balance": MAX_CREDITS, "last_updated": 0}
        credits[user_id]["balance"] = float(balance)
        credits[user_id]["max_credit"] = float(max_credit)
        _save("credits.json", credits)

# ═══════════════════════════════════════════
#  EXPORT / IMPORT
# ═══════════════════════════════════════════

def export_all():
    with _lock:
        result = {}
        # Main JSON files
        for filename in ["users.json", "sessions.json", "credits.json", "plugins.json"]:
            result[filename.replace(".json", "")] = _load(filename)

        # Conversation lists per user
        conv_lists_dir = DATA_DIR / "conv_lists"
        conv_lists = {}
        if conv_lists_dir.exists():
            for f in conv_lists_dir.glob("*.json"):
                conv_lists[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        result["conv_lists"] = conv_lists

        # Individual conversations
        convs_dir = DATA_DIR / "convs"
        convs = {}
        if convs_dir.exists():
            for f in convs_dir.glob("*.json"):
                convs[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        result["convs"] = convs

        return result


def import_all(data):
    with _lock:
        # Main files
        for key in ["users", "sessions", "credits", "plugins"]:
            if key in data:
                _save(f"{key}.json", data[key])

        # Conversation lists
        if "conv_lists" in data:
            for user_id, conv_list_data in data["conv_lists"].items():
                _save(f"conv_lists/{user_id}.json", conv_list_data)

        # Conversations
        if "convs" in data:
            for conv_id, conv_data in data["convs"].items():
                _save(f"convs/{conv_id}.json", conv_data)