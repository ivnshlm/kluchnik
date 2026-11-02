#!/usr/bin/env python3
"""База клиентов VPN: кто привязан к какому аккаунту MAX, какие у него
устройства и какие коды приглашения выданы. Пишут оба бота - админский в
телеграме выдаёт коды, клиентский в MAX их принимает."""
import os
import re
import secrets
import sqlite3
import time

DB_PATH = "/var/lib/vpnbot/clients.db"
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих друг на друга I/1, O/0
INVITE_TTL = 7 * 86400
DEV_LIMIT = 2

_db = None


def db():
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), mode=0o700, exist_ok=True)
        _db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=15)
        _db.execute("PRAGMA journal_mode=WAL")  # два процесса на одном файле
        _db.executescript("""
        CREATE TABLE IF NOT EXISTS people (
            uid INTEGER PRIMARY KEY, label TEXT, joined INTEGER,
            dev_limit INTEGER, blocked INTEGER DEFAULT 0, last_reissue INTEGER DEFAULT 0,
            chat INTEGER);
        CREATE TABLE IF NOT EXISTS devices (
            peer TEXT PRIMARY KEY, uid INTEGER, created INTEGER);
        CREATE TABLE IF NOT EXISTS invites (
            code TEXT PRIMARY KEY, peer TEXT, created INTEGER, expires INTEGER,
            used_by INTEGER, used_at INTEGER);
        CREATE TABLE IF NOT EXISTS pending (
            uid INTEGER PRIMARY KEY, kind TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS log (
            ts INTEGER, uid INTEGER, action TEXT, detail TEXT);
        """)
        os.chmod(DB_PATH, 0o600)
    return _db


def logit(uid, action, detail=""):
    db().execute("INSERT INTO log(ts,uid,action,detail) VALUES(?,?,?,?)",
                 (int(time.time()), uid, action, detail))


# --- приглашения ---

def norm_code(text):
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def fmt_code(code):
    return code[:4] + "-" + code[4:]


def invite_create(peer):
    """Код на одного человека. Старые неиспользованные коды того же пира гасим,
    чтобы по второму приглашению нельзя было привязать второй аккаунт."""
    now = int(time.time())
    db().execute("DELETE FROM invites WHERE peer=? AND used_by IS NULL", (peer,))
    code = "".join(secrets.choice(CODE_CHARS) for _ in range(8))
    db().execute("INSERT INTO invites(code,peer,created,expires) VALUES(?,?,?,?)",
                 (code, peer, now, now + INVITE_TTL))
    return code


def invite_open():
    now = int(time.time())
    return db().execute(
        "SELECT code,peer,expires FROM invites WHERE used_by IS NULL AND expires>? "
        "ORDER BY created", (now,)).fetchall()


def invite_find(code):
    return db().execute(
        "SELECT code,peer,created,expires,used_by FROM invites WHERE code=?", (code,)).fetchone()


def invite_use(code, uid):
    db().execute("UPDATE invites SET used_by=?, used_at=? WHERE code=?",
                 (uid, int(time.time()), code))


# --- люди и устройства ---

COLS = "uid,label,joined,dev_limit,blocked,last_reissue,chat"


def person(uid):
    return db().execute(f"SELECT {COLS} FROM people WHERE uid=?", (uid,)).fetchone()


def person_add(uid, label, chat, dev_limit=DEV_LIMIT):
    db().execute(
        "INSERT OR REPLACE INTO people(uid,label,joined,dev_limit,blocked,last_reissue,chat) "
        "VALUES(?,?,?,?,0,0,?)", (uid, label, int(time.time()), dev_limit, chat))


def chat_set(uid, chat):
    """Рассылка идёт в диалог, а его chat_id не равен user_id, поэтому держим его."""
    db().execute("UPDATE people SET chat=? WHERE uid=? AND (chat IS NULL OR chat<>?)",
                 (chat, uid, chat))


def people_all():
    return db().execute(f"SELECT {COLS} FROM people ORDER BY joined").fetchall()


def reissue_mark(uid):
    db().execute("UPDATE people SET last_reissue=? WHERE uid=?", (int(time.time()), uid))


def devices_of(uid):
    return [r[0] for r in db().execute(
        "SELECT peer FROM devices WHERE uid=? ORDER BY created", (uid,)).fetchall()]


def device_add(peer, uid):
    db().execute("INSERT OR REPLACE INTO devices(peer,uid,created) VALUES(?,?,?)",
                 (peer, uid, int(time.time())))


def device_owner(peer):
    row = db().execute("SELECT uid FROM devices WHERE peer=?", (peer,)).fetchone()
    return row[0] if row else None


def bound_peers():
    return {r[0] for r in db().execute("SELECT peer FROM devices").fetchall()}


# --- незаконченный диалог (ждём от человека текст) ---

def pending_set(uid, kind):
    db().execute("INSERT OR REPLACE INTO pending(uid,kind,ts) VALUES(?,?,?)",
                 (uid, kind, int(time.time())))


def pending_get(uid):
    row = db().execute("SELECT kind,ts FROM pending WHERE uid=?", (uid,)).fetchone()
    if not row:
        return None
    if int(time.time()) - row[1] > 600:
        pending_clear(uid)
        return None
    return row[0]


def pending_clear(uid):
    db().execute("DELETE FROM pending WHERE uid=?", (uid,))
