"""Локальная база диалогов с клиентами (SQLite): переписка для ИИ и связь диалога с заявкой в CRM."""
import json
import sqlite3
import time

from .config import cfg

_db = sqlite3.connect(cfg.data_dir / "dialogs.db", check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript("""
CREATE TABLE IF NOT EXISTS dialogs (
    key TEXT PRIMARY KEY,          -- tg:<chat_id> или vk:<peer_id>
    channel TEXT NOT NULL,
    name TEXT DEFAULT '',
    username TEXT DEFAULT '',
    order_id TEXT DEFAULT '',      -- заявка/заказ в CRM, связанный с диалогом
    state TEXT DEFAULT 'unknown',  -- unknown | lead | not_client
    checks INTEGER DEFAULT 0,      -- сколько раз ИИ проверял, клиент ли это
    created INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    ts INTEGER NOT NULL,
    outgoing INTEGER NOT NULL,     -- 1: написал владелец, 0: клиент
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_key ON messages(key, ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
""")


def get(key: str) -> dict | None:
    row = _db.execute("SELECT * FROM dialogs WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def ensure(key: str, channel: str, name: str = "", username: str = "") -> dict:
    d = get(key)
    if d is None:
        _db.execute("INSERT INTO dialogs(key, channel, name, username, created) VALUES (?,?,?,?,?)",
                    (key, channel, name, username, int(time.time())))
        _db.commit()
        return get(key)
    if name and name != d["name"]:
        update(key, name=name, username=username)
        d |= {"name": name, "username": username}
    return d


def update(key: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    _db.execute(f"UPDATE dialogs SET {cols} WHERE key = ?", (*fields.values(), key))
    _db.commit()


def add_message(key: str, outgoing: bool, text: str, ts: int | None = None) -> None:
    _db.execute("INSERT INTO messages(key, ts, outgoing, text) VALUES (?,?,?,?)",
                (key, ts or int(time.time()), int(outgoing), text))
    _db.commit()


def history(key: str, limit: int = 40) -> list[dict]:
    rows = _db.execute("SELECT ts, outgoing, text FROM messages WHERE key = ? ORDER BY ts DESC, id DESC LIMIT ?",
                       (key, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def count_incoming(key: str) -> int:
    return _db.execute("SELECT COUNT(*) FROM messages WHERE key = ? AND outgoing = 0", (key,)).fetchone()[0]


# Небольшое хранилище состояний (черновики подтверждений, ожидание исправления), переживает перезапуск.
def kv_set(k: str, v) -> None:
    _db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))
    _db.commit()


def kv_get(k: str, default=None):
    row = _db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
    return json.loads(row[0]) if row else default


def kv_pop(k: str, default=None):
    v = kv_get(k, default)
    _db.execute("DELETE FROM kv WHERE k = ?", (k,))
    _db.commit()
    return v
