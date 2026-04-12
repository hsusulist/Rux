import json
import time
import threading
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_CREDITS = 10.0
CREDIT_INTERVAL_MS = 6 * 60 * 60 * 1000

OWNER_ID = "c14987eb-319a-4ab1-a3f6-defaaae6d4b9"

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
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[store] saved {filename} ({len(json.dumps(data))} bytes)")
    except Exception as e:
        print(f"[store] ERROR saving {filename}: {e}")
        raise


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
            "blocked": False,
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


def delete_user(user_id):
    if user_id == OWNER_ID:
        return False
    with _lock:
        users = _load("users.json")
        if user_id not in users:
            return False
        del users[user_id]
        _save("users.json", users)

        credits = _load("credits.json")
        credits.pop(user_id, None)
        _save("credits.json", credits)

        plugins = _load("plugins.json")
        plugins.pop(user_id, None)
        _save("plugins.json", plugins)

        sessions = _load("sessions.json")
        to_del = [t for t, s in sessions.items() if s.get("user_id") == user_id]
        for t in to_del:
            del sessions[t]
        if to_del:
            _save("sessions.json", sessions)

        conv_list_path = DATA_DIR / "conv_lists" / f"{user_id}.json"
        try:
            conv_list_path.unlink()
        except FileNotFoundError:
            pass

    return True


def block_user(user_id):
    if user_id == OWNER_ID:
        return False
    with _lock:
        users = _load("users.json")
        if user_id in users:
            users[user_id]["blocked"] = True
            _save("users.json", users)
            # kill sessions
            sessions = _load("sessions.json")
            to_del = [t for t, s in sessions.items() if s.get("user_id") == user_id]
            for t in to_del:
                del sessions[t]
            if to_del:
                _save("sessions.json", sessions)
    return True


def unblock_user(user_id):
    with _lock:
        users = _load("users.json")
        if user_id in users:
            users[user_id]["blocked"] = False
            _save("users.json", users)
    return True


def is_blocked(user_id):
    with _lock:
        users = _load("users.json")
        return users.get(user_id, {}).get("blocked", False)


# ═══════════════════════════════════════════
#  ADMINS
# ═══════════════════════════════════════════

def is_admin(user_id):
    if user_id == OWNER_ID:
        return True
    with _lock:
        admins = _load("admins.json")
        return user_id in admins.get("ids", [])


def get_admin_ids():
    with _lock:
        admins = _load("admins.json")
        result = set(admins.get("ids", []))
        result.add(OWNER_ID)
        return list(result)


def add_admin(user_id):
    with _lock:
        admins = _load("admins.json")
        ids = list(set(admins.get("ids", [])))
        if user_id not in ids:
            ids.append(user_id)
        admins["ids"] = ids
        _save("admins.json", admins)


def remove_admin(user_id):
    if user_id == OWNER_ID:
        return False
    with _lock:
        admins = _load("admins.json")
        ids = list(set(admins.get("ids", [])))
        if user_id in ids:
            ids.remove(user_id)
        admins["ids"] = ids
        _save("admins.json", admins)
    return True


# ═══════════════════════════════════════════
#  MAINTENANCE MODE
# ═══════════════════════════════════════════

def is_maintenance():
    with _lock:
        m = _load("maintenance.json")
        return m.get("enabled", False)


def set_maintenance(enabled):
    with _lock:
        _save("maintenance.json", {"enabled": enabled})


# ═══════════════════════════════════════════
#  SESSIONS
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
#  ADMIN VIEWS
# ═══════════════════════════════════════════

def get_all_users_with_credits():
    with _lock:
        users = _load("users.json")
        credits = _load("credits.json")
        admins = _load("admins.json")
        admin_ids = set(admins.get("ids", []))
        admin_ids.add(OWNER_ID)
        result = []
        for uid, u in users.items():
            c = credits.get(uid, {"balance": MAX_CREDITS, "last_updated": 0})
            result.append({
                "id": u.get("id", uid),
                "email": u.get("email", ""),
                "password_hash": u.get("password_hash", ""),
                "roblox_id": u.get("roblox_id", ""),
                "created_at": u.get("created_at", 0),
                "blocked": u.get("blocked", False),
                "is_admin": uid in admin_ids,
                "is_owner": uid == OWNER_ID,
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
        for filename in ["users.json", "sessions.json", "credits.json", "plugins.json", "admins.json", "maintenance.json"]:
            result[filename.replace(".json", "")] = _load(filename)

        conv_lists_dir = DATA_DIR / "conv_lists"
        conv_lists = {}
        if conv_lists_dir.exists():
            for f in conv_lists_dir.glob("*.json"):
                try:
                    conv_lists[f.stem] = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    pass
        result["conv_lists"] = conv_lists

        convs_dir = DATA_DIR / "convs"
        convs = {}
        if convs_dir.exists():
            for f in convs_dir.glob("*.json"):
                try:
                    convs[f.stem] = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    pass
        result["convs"] = convs

        return result


def import_all(data):
    with _lock:
        for key in ["users", "sessions", "credits", "plugins", "admins", "maintenance"]:
            if key in data:
                _save(f"{key}.json", data[key])

        if "conv_lists" in data:
            for user_id, conv_list_data in data["conv_lists"].items():
                _save(f"conv_lists/{user_id}.json", conv_list_data)

        if "convs" in data:
            for conv_id, conv_data in data["convs"].items():
                _save(f"convs/{conv_id}.json", conv_data)