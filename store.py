import json
import time
import threading
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_CREDITS = 10.0
CREDIT_INTERVAL_MS = 6 * 60 * 60 * 1000

OWNER_ID = "984cd1b9-28d9-404a-96d5-449d56e3cee8"

_lock = threading.Lock()


def _load(filename, default=None):
    path = DATA_DIR / filename
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {} if default is None else default


def _save(filename, data):
    path = DATA_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
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
            "admin_notes": "",
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


def update_user_email(user_id, new_email):
    with _lock:
        users = _load("users.json")
        if user_id in users:
            users[user_id]["email"] = new_email.lower().strip()
            _save("users.json", users)
            return True
        return False


def update_user_password(user_id, new_password_hash):
    with _lock:
        users = _load("users.json")
        if user_id in users:
            users[user_id]["password_hash"] = new_password_hash
            _save("users.json", users)
            return True
        return False


def update_user_roblox_id(user_id, roblox_id):
    with _lock:
        users = _load("users.json")
        if user_id in users:
            users[user_id]["roblox_id"] = str(roblox_id) if roblox_id else ""
            _save("users.json", users)
            return True
        return False


def set_user_note(user_id, note_text):
    with _lock:
        users = _load("users.json")
        if user_id in users:
            users[user_id]["admin_notes"] = note_text or ""
            _save("users.json", users)
            return True
        return False


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

        for subdir in ["conv_lists", "preferences", "rate_limits", "credit_history"]:
            p = DATA_DIR / subdir / f"{user_id}.json"
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    return True


def self_delete_account(user_id):
    if user_id == OWNER_ID:
        return False
    return delete_user(user_id)


def block_user(user_id):
    if user_id == OWNER_ID:
        return False
    with _lock:
        users = _load("users.json")
        if user_id in users:
            users[user_id]["blocked"] = True
            _save("users.json", users)
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


def delete_all_user_sessions(user_id):
    with _lock:
        sessions = _load("sessions.json")
        to_del = [t for t, s in sessions.items() if s.get("user_id") == user_id]
        for t in to_del:
            del sessions[t]
        if to_del:
            _save("sessions.json", sessions)
        return len(to_del)


def get_all_sessions():
    with _lock:
        sessions = _load("sessions.json")
        result = []
        for t, s in sessions.items():
            result.append({
                "token_preview": t[:8] + "…" + t[-4:],
                "token_full": t,
                "user_id": s["user_id"],
                "created_at": s.get("created_at", 0),
            })
        return result


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


def set_user_credits(user_id, balance, max_credit):
    with _lock:
        credits = _load("credits.json")
        if user_id not in credits:
            credits[user_id] = {"balance": MAX_CREDITS, "last_updated": 0}
        credits[user_id]["balance"] = float(balance)
        credits[user_id]["max_credit"] = float(max_credit)
        _save("credits.json", credits)


# ═══════════════════════════════════════════
#  CREDIT HISTORY
# ═══════════════════════════════════════════

def save_credit_entry(user_id, amount, reason, admin_id=None):
    with _lock:
        history = _load(f"credit_history/{user_id}.json")
        if not isinstance(history, list):
            history = []
        entry = {
            "amount": round(amount, 6),
            "reason": reason or "",
            "admin_id": admin_id or "",
            "timestamp": int(time.time() * 1000),
        }
        history.append(entry)
        if len(history) > 500:
            history = history[-500:]
        _save(f"credit_history/{user_id}.json", history)


def get_credit_history(user_id, limit=50):
    with _lock:
        history = _load(f"credit_history/{user_id}.json")
        if not isinstance(history, list):
            history = []
        return history[-limit:]


def get_global_credit_history(limit=100):
    with _lock:
        all_entries = []
        history_dir = DATA_DIR / "credit_history"
        if history_dir.exists():
            for f in history_dir.glob("*.json"):
                try:
                    user_id = f.stem
                    entries = json.loads(f.read_text(encoding="utf-8"))
                    for e in entries:
                        e["user_id"] = user_id
                        all_entries.append(e)
                except Exception:
                    pass
        all_entries.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return all_entries[:limit]


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


def get_all_conversations(limit=200):
    """Get conversations across all users for moderation."""
    with _lock:
        result = []
        conv_lists_dir = DATA_DIR / "conv_lists"
        if not conv_lists_dir.exists():
            return result
        for clf in conv_lists_dir.glob("*.json"):
            try:
                user_id = clf.stem
                conv_list = json.loads(clf.read_text(encoding="utf-8"))
                for cid, meta in conv_list.items():
                    result.append({
                        "id": cid,
                        "title": meta.get("title", ""),
                        "user_id": user_id,
                        "mode": meta.get("mode", "chat"),
                        "model": meta.get("model", ""),
                        "updatedAt": meta.get("updatedAt", 0),
                    })
            except Exception:
                pass
        result.sort(key=lambda x: x.get("updatedAt", 0), reverse=True)
        return result[:limit]


def flag_conversation(conv_id, reason, admin_id):
    with _lock:
        flags = _load("conv_flags.json")
        flags[conv_id] = {
            "reason": reason,
            "admin_id": admin_id,
            "timestamp": int(time.time() * 1000),
        }
        _save("conv_flags.json", flags)


def get_conv_flags():
    with _lock:
        return _load("conv_flags.json")


# ═══════════════════════════════════════════
#  CHECKPOINTS
# ═══════════════════════════════════════════

def get_checkpoints(user_id):
    with _lock:
        return _load(f"checkpoints/{user_id}.json")


def get_checkpoint(user_id, checkpoint_id):
    with _lock:
        data = _load(f"checkpoints/{user_id}.json")
        return data.get(checkpoint_id)


def save_checkpoint(user_id, checkpoint_id, checkpoint_data):
    with _lock:
        data = _load(f"checkpoints/{user_id}.json")
        data[checkpoint_id] = checkpoint_data
        _save(f"checkpoints/{user_id}.json", data)


def delete_checkpoint(user_id, checkpoint_id):
    with _lock:
        data = _load(f"checkpoints/{user_id}.json")
        if checkpoint_id not in data:
            return False
        del data[checkpoint_id]
        _save(f"checkpoints/{user_id}.json", data)
        return True


def get_all_checkpoints(limit=200):
    """Get checkpoints across all users for admin browsing."""
    with _lock:
        result = []
        ckpts_dir = DATA_DIR / "checkpoints"
        if not ckpts_dir.exists():
            return result
        for f in ckpts_dir.glob("*.json"):
            try:
                user_id = f.stem
                data = json.loads(f.read_text(encoding="utf-8"))
                for ckpt_id, ckpt in data.items():
                    result.append({
                        "id": ckpt_id,
                        "label": ckpt.get("label", ""),
                        "user_id": user_id,
                        "created_at": ckpt.get("created_at", 0),
                        "scripts_count": len(ckpt.get("scripts", {})),
                        "auto": ckpt.get("auto", False),
                    })
            except Exception:
                pass
        result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return result[:limit]


# ═══════════════════════════════════════════
#  USER PREFERENCES
# ═══════════════════════════════════════════

DEFAULT_PREFERENCES = {
    "sound_enabled": False,
    "default_model": "sonnet",
    "default_mode": "chat",
    "compact_mode": False,
    "show_thinking": True,
    "show_credits_log": True,
    "auto_scroll": True,
}


def get_preferences(user_id):
    with _lock:
        prefs = _load(f"preferences/{user_id}.json")
        if not prefs:
            return dict(DEFAULT_PREFERENCES)
        merged = dict(DEFAULT_PREFERENCES)
        merged.update(prefs)
        return merged


def save_preferences(user_id, prefs_data):
    with _lock:
        existing = _load(f"preferences/{user_id}.json")
        if not existing:
            existing = dict(DEFAULT_PREFERENCES)
        existing.update(prefs_data)
        _save(f"preferences/{user_id}.json", existing)
        return existing


# ═══════════════════════════════════════════
#  RATE LIMITING
# ═══════════════════════════════════════════

def check_rate_limit(user_id, action="api", limit=30, window_ms=60000):
    now = int(time.time() * 1000)
    with _lock:
        data = _load(f"rate_limits/{user_id}.json")
        key = action
        record = data.get(key, {"count": 0, "window_start": now})
        window_start = record.get("window_start", now)
        count = record.get("count", 0)
        if now - window_start > window_ms:
            window_start = now
            count = 0
        if count >= limit:
            reset_at = window_start + window_ms
            return False, 0, reset_at
        count += 1
        data[key] = {"count": count, "window_start": window_start}
        _save(f"rate_limits/{user_id}.json", data)
        remaining = limit - count
        reset_at = window_start + window_ms
        return True, remaining, reset_at


# ═══════════════════════════════════════════
#  AUDIT LOG
# ═══════════════════════════════════════════

def save_audit_entry(admin_id, action, target_user_id="", details=""):
    with _lock:
        log = _load("audit_log.json", default=[])
        if not isinstance(log, list):
            log = []
        entry = {
            "id": str(time.time_ns()),
            "admin_id": admin_id,
            "action": action,
            "target_user_id": target_user_id or "",
            "details": details or "",
            "timestamp": int(time.time() * 1000),
        }
        log.append(entry)
        if len(log) > 5000:
            log = log[-5000:]
        _save("audit_log.json", log)


def get_audit_log(limit=100, offset=0, action_filter=None):
    with _lock:
        log = _load("audit_log.json", default=[])
        if not isinstance(log, list):
            log = []
        if action_filter:
            log = [e for e in log if e.get("action") == action_filter]
        log.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return log[offset:offset + limit]


# ═══════════════════════════════════════════
#  ANNOUNCEMENTS
# ═══════════════════════════════════════════

def get_announcements():
    with _lock:
        anns = _load("announcements.json", default=[])
        return anns if isinstance(anns, list) else []


def get_active_announcements():
    with _lock:
        anns = _load("announcements.json", default=[])
        if not isinstance(anns, list):
            anns = []
        now = int(time.time() * 1000)
        return [a for a in anns if a.get("enabled", True) and (not a.get("expires_at") or a["expires_at"] > now)]


def save_announcement(ann_data):
    with _lock:
        anns = _load("announcements.json", default=[])
        if not isinstance(anns, list):
            anns = []
        anns.append(ann_data)
        _save("announcements.json", anns)


def update_announcement(ann_id, updates):
    with _lock:
        anns = _load("announcements.json", default=[])
        if not isinstance(anns, list):
            anns = []
        for a in anns:
            if a.get("id") == ann_id:
                a.update(updates)
                break
        _save("announcements.json", anns)


def delete_announcement(ann_id):
    with _lock:
        anns = _load("announcements.json", default=[])
        if not isinstance(anns, list):
            anns = []
        anns = [a for a in anns if a.get("id") != ann_id]
        _save("announcements.json", anns)


# ═══════════════════════════════════════════
#  EMAIL BANS
# ═══════════════════════════════════════════

def get_email_bans():
    with _lock:
        return _load("email_bans.json")


def add_email_ban(pattern, admin_id, reason=""):
    with _lock:
        bans = _load("email_bans.json")
        ban_id = "ban-" + str(int(time.time() * 1000))
        bans[ban_id] = {
            "id": ban_id,
            "pattern": pattern.lower().strip(),
            "reason": reason or "",
            "admin_id": admin_id,
            "created_at": int(time.time() * 1000),
        }
        _save("email_bans.json", bans)
        return ban_id


def delete_email_ban(ban_id):
    with _lock:
        bans = _load("email_bans.json")
        if ban_id in bans:
            del bans[ban_id]
            _save("email_bans.json", bans)
            return True
        return False


def check_email_banned(email):
    lowered = email.lower().strip()
    with _lock:
        bans = _load("email_bans.json")
        for ban in bans.values():
            pattern = ban.get("pattern", "")
            if pattern.startswith("@"):
                if lowered.endswith(pattern) or "@" + lowered.split("@")[-1] == pattern[1:]:
                    return True
            elif pattern.startswith("*."):
                domain = pattern[1:]
                if lowered.endswith(domain):
                    return True
            else:
                if lowered == pattern:
                    return True
        return False


# ═══════════════════════════════════════════
#  INVITE CODES
# ═══════════════════════════════════════════

def get_invites():
    with _lock:
        return _load("invites.json")


def create_invite(code, max_uses=1, expires_at=0, admin_id=""):
    with _lock:
        invites = _load("invites.json")
        invites[code] = {
            "code": code,
            "max_uses": max_uses,
            "uses": 0,
            "expires_at": expires_at or 0,
            "admin_id": admin_id,
            "created_at": int(time.time() * 1000),
        }
        _save("invites.json", invites)


def use_invite(code):
    with _lock:
        invites = _load("invites.json")
        inv = invites.get(code)
        if not inv:
            return False, "Invalid invite code"
        if inv["uses"] >= inv["max_uses"]:
            return False, "Invite code has been used up"
        if inv.get("expires_at") and int(time.time() * 1000) > inv["expires_at"]:
            return False, "Invite code has expired"
        inv["uses"] += 1
        _save("invites.json", invites)
        return True, "OK"


def delete_invite(code):
    with _lock:
        invites = _load("invites.json")
        if code in invites:
            del invites[code]
            _save("invites.json", invites)
            return True
        return False


def is_invite_only():
    with _lock:
        config = _load("system_config.json")
        return config.get("invite_only", False)


# ═══════════════════════════════════════════
#  SYSTEM CONFIG
# ═══════════════════════════════════════════

DEFAULT_CONFIG = {
    "invite_only": False,
    "default_starting_credits": MAX_CREDITS,
    "credit_regen_interval_ms": CREDIT_INTERVAL_MS,
    "max_credits_default": MAX_CREDITS,
    "max_agent_steps": 20,
    "code_expiry_ms": 300000,
}


def get_config():
    with _lock:
        config = _load("system_config.json")
        if not config:
            return dict(DEFAULT_CONFIG)
        merged = dict(DEFAULT_CONFIG)
        merged.update(config)
        return merged


def save_config(config_data):
    with _lock:
        existing = _load("system_config.json")
        if not existing:
            existing = dict(DEFAULT_CONFIG)
        existing.update(config_data)
        _save("system_config.json", existing)
        return existing


# ═══════════════════════════════════════════
#  WEBHOOKS
# ═══════════════════════════════════════════

def get_webhooks():
    with _lock:
        return _load("webhooks.json")


def save_webhooks(data):
    with _lock:
        _save("webhooks.json", data)


# ═══════════════════════════════════════════
#  SYSTEM STATISTICS
# ═══════════════════════════════════════════

def get_system_stats():
    with _lock:
        users = _load("users.json")
        credits = _load("credits.json")
        admins = _load("admins.json")
        sessions = _load("sessions.json")
        plugins = _load("plugins.json")
        maintenance = _load("maintenance.json")

        total_users = len(users)
        total_blocked = sum(1 for u in users.values() if u.get("blocked", False))
        total_admins = len(admins.get("ids", [])) + 1

        total_credits = 0.0
        zero_credit_users = 0
        for uid in users:
            c = credits.get(uid, {"balance": MAX_CREDITS, "max_credit": MAX_CREDITS})
            bal = float(c.get("balance", 0))
            total_credits += bal
            if bal <= 0:
                zero_credit_users += 1

        active_sessions = len(sessions)
        connected_plugins = len(plugins)

        convs_dir = DATA_DIR / "convs"
        total_convs = len(list(convs_dir.glob("*.json"))) if convs_dir.exists() else 0

        now = int(time.time())
        recent_24h = sum(1 for u in users.values() if now - u.get("created_at", 0) < 86400)
        recent_7d = sum(1 for u in users.values() if now - u.get("created_at", 0) < 604800)

        total_checkpoints = 0
        ckpts_dir = DATA_DIR / "checkpoints"
        if ckpts_dir.exists():
            for f in ckpts_dir.glob("*.json"):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    total_checkpoints += len(d)
                except Exception:
                    pass

        avg_credits = round(total_credits / total_users, 2) if total_users > 0 else 0

        # Disk usage
        total_size = 0
        for fp in DATA_DIR.rglob("*"):
            if fp.is_file():
                try:
                    total_size += fp.stat().st_size
                except Exception:
                    pass

        return {
            "total_users": total_users,
            "total_blocked": total_blocked,
            "total_admins": total_admins,
            "total_credits": round(total_credits, 2),
            "avg_credits": avg_credits,
            "zero_credit_users": zero_credit_users,
            "active_sessions": active_sessions,
            "connected_plugins": connected_plugins,
            "total_conversations": total_convs,
            "total_checkpoints": total_checkpoints,
            "recent_signups_24h": recent_24h,
            "recent_signups_7d": recent_7d,
            "maintenance_enabled": maintenance.get("enabled", False),
            "disk_usage_bytes": total_size,
            "disk_usage_mb": round(total_size / 1048576, 2),
        }


# ═══════════════════════════════════════════
#  ADMIN VIEWS
# ═══════════════════════════════════════════

def get_all_users_with_credits():
    with _lock:
        users = _load("users.json")
        credits_data = _load("credits.json")
        admins = _load("admins.json")
        admin_ids = set(admins.get("ids", []))
        admin_ids.add(OWNER_ID)
        result = []
        for uid, u in users.items():
            c = credits_data.get(uid, {"balance": MAX_CREDITS, "last_updated": 0, "max_credit": MAX_CREDITS})
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
                "admin_notes": u.get("admin_notes", ""),
            })
        return result


def get_user_detail(user_id):
    """Get full detail for a specific user."""
    with _lock:
        users = _load("users.json")
        u = users.get(user_id)
        if not u:
            return None
        credits_data = _load("credits.json")
        c = credits_data.get(user_id, {"balance": MAX_CREDITS, "last_updated": 0, "max_credit": MAX_CREDITS})
        admins = _load("admins.json")
        admin_ids = set(admins.get("ids", []))
        admin_ids.add(OWNER_ID)

        # Credit history
        credit_hist = _load(f"credit_history/{user_id}.json")

        # Conversations
        conv_list = _load(f"conv_lists/{user_id}.json")
        conv_summary = []
        for cid, meta in conv_list.items():
            conv_summary.append({
                "id": cid,
                "title": meta.get("title", ""),
                "mode": meta.get("mode", "chat"),
                "model": meta.get("model", ""),
                "updatedAt": meta.get("updatedAt", 0),
            })
        conv_summary.sort(key=lambda x: x.get("updatedAt", 0), reverse=True)

        # Checkpoints
        checkpoints = _load(f"checkpoints/{user_id}.json")
        ckpt_summary = []
        for ckpt_id, ckpt in checkpoints.items():
            ckpt_summary.append({
                "id": ckpt_id,
                "label": ckpt.get("label", ""),
                "created_at": ckpt.get("created_at", 0),
                "scripts_count": len(ckpt.get("scripts", {})),
                "auto": ckpt.get("auto", False),
            })
        ckpt_summary.sort(key=lambda x: x.get("created_at", 0), reverse=True)

        # Preferences
        prefs = _load(f"preferences/{user_id}.json")

        return {
            "user": {
                "id": u.get("id", user_id),
                "email": u.get("email", ""),
                "roblox_id": u.get("roblox_id", ""),
                "created_at": u.get("created_at", 0),
                "blocked": u.get("blocked", False),
                "is_admin": user_id in admin_ids,
                "is_owner": user_id == OWNER_ID,
                "admin_notes": u.get("admin_notes", ""),
            },
            "credits": {
                "balance": float(c.get("balance", 0)),
                "max_credit": float(c.get("max_credit", MAX_CREDITS)),
                "last_updated": c.get("last_updated", 0),
            },
            "credit_history": credit_hist[-30:],
            "conversations": conv_summary[:30],
            "checkpoints": ckpt_summary[:20],
            "preferences": prefs,
        }


# ═══════════════════════════════════════════
#  EXPORT / IMPORT
# ═══════════════════════════════════════════

def export_all():
    with _lock:
        result = {}
        for filename in ["users.json", "sessions.json", "credits.json", "plugins.json",
                         "admins.json", "maintenance.json", "audit_log.json",
                         "announcements.json", "email_bans.json", "invites.json",
                         "system_config.json", "webhooks.json", "conv_flags.json"]:
            result[filename.replace(".json", "")] = _load(filename)

        for subdir in ["conv_lists", "convs", "conv_lists", "preferences",
                       "rate_limits", "credit_history", "checkpoints"]:
            dir_path = DATA_DIR / subdir
            sub_data = {}
            if dir_path.exists():
                for f in dir_path.glob("*.json"):
                    try:
                        sub_data[f.stem] = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:
                        pass
            result[subdir] = sub_data

        return result


def import_all(data):
    with _lock:
        for key in ["users", "sessions", "credits", "plugins", "admins",
                     "maintenance", "audit_log", "announcements", "email_bans",
                     "invites", "system_config", "webhooks", "conv_flags"]:
            if key in data:
                _save(f"{key}.json", data[key])

        for subdir in ["conv_lists", "convs", "preferences", "rate_limits",
                        "credit_history", "checkpoints"]:
            if subdir in data:
                for item_id, item_data in data[subdir].items():
                    _save(f"{subdir}/{item_id}.json", item_data)