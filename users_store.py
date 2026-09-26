"""
Teacher accounts — users.json next to app.py (git-ignored).

    {"users": {"<username>": {"password_hash": "...", "display_name": "...",
                              "created_at": "...", "updated_at": "..."}}}

Passwords are stored only as Werkzeug salted hashes (scrypt/pbkdf2), which
ship with Flask — no extra library. Shared by app.py (sign-in) and
manage_users.py (creating accounts); neither imports the other.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash

from storage import read_json, update_json

USERS_FILE = Path(__file__).parent / "users.json"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,40}$")
MIN_PASSWORD_LEN = 8

# Checked against when the username doesn't exist, so a wrong username
# takes as long as a wrong password (no account-existence timing leak).
_DUMMY_HASH = generate_password_hash("edumark-dummy-password")


def _file(path) -> Path:
    return Path(path) if path else USERS_FILE


def _norm(username: str) -> str:
    return (username or "").strip().lower()


def load_users(path: Path = None) -> dict:
    data = read_json(_file(path), {"users": {}}) or {}
    users = data.get("users")
    return users if isinstance(users, dict) else {}


def has_users(path: Path = None) -> bool:
    return bool(load_users(path))


def verify(username: str, password: str, path: Path = None) -> Optional[dict]:
    """Returns {"username", "display_name"} on success, else None."""
    user = load_users(path).get(_norm(username))
    if not user:
        check_password_hash(_DUMMY_HASH, password or "")
        return None
    if not check_password_hash(user.get("password_hash", ""), password or ""):
        return None
    return {"username": _norm(username), "display_name": user.get("display_name") or _norm(username)}


def set_user(username: str, password: str, display_name: str = None, path: Path = None) -> bool:
    """Creates or updates an account. Returns True if it was newly created."""
    name = _norm(username)
    if not USERNAME_RE.match(name):
        raise ValueError("Username must be 2–40 characters: letters, numbers, dot, dash or underscore.")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    now = datetime.now().isoformat(timespec="seconds")
    created = {"value": False}

    def mutate(data):
        users = data.setdefault("users", {})
        rec = users.get(name)
        if rec is None:
            created["value"] = True
            rec = users[name] = {"created_at": now}
        rec["password_hash"] = generate_password_hash(password)
        rec["updated_at"] = now
        if display_name is not None or "display_name" not in rec:
            rec["display_name"] = (display_name or name).strip()

    update_json(_file(path), mutate, default={"users": {}})
    return created["value"]


def remove_user(username: str, path: Path = None) -> bool:
    name = _norm(username)
    removed = {"value": False}

    def mutate(data):
        removed["value"] = data.setdefault("users", {}).pop(name, None) is not None

    update_json(_file(path), mutate, default={"users": {}})
    return removed["value"]
