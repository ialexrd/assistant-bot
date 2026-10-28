"""Firestore: заказы CRM (коллекция orders), дела планера (planner_events), заметки (notes)."""
import asyncio
import logging
import time

import firebase_admin
import httpx
from firebase_admin import firestore

from .config import cfg

log = logging.getLogger("storage")

firebase_admin.initialize_app()  # ключ берётся из GOOGLE_APPLICATION_CREDENTIALS
db = firestore.client()

ORDER_FIELDS = ("name", "phone", "addr", "date", "time", "sum", "src", "spec", "vol", "status", "note")
OPEN_STATUSES = ("new", "thinking")
STATUS_RU = {"new": "новая", "thinking": "думает", "booked": "записан", "done": "выполнен", "lost": "отказ"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _docs(q) -> list[dict]:
    return [{"id": d.id, **d.to_dict()} for d in q.stream()]


def _status(o: dict) -> str:
    # Заказы, созданные до появления статусов, считаем записанными.
    return o.get("status") or "booked"


# ---------- CRM ----------

async def add_order(data: dict) -> str:
    order = {k: str(data.get(k) or "") for k in ORDER_FIELDS}
    order["status"] = order["status"] or ("booked" if order["date"] else "new")
    order["createdAt"] = _now_ms()
    order["source"] = "bot"
    _, ref = await asyncio.to_thread(db.collection("orders").add, order)
    return ref.id


async def update_order(order_id: str, fields: dict) -> None:
    fields = {**fields, "updatedAt": _now_ms()}
    await asyncio.to_thread(db.collection("orders").document(order_id).update, fields)


async def get_order(order_id: str) -> dict | None:
    snap = await asyncio.to_thread(db.collection("orders").document(order_id).get)
    return {"id": snap.id, **snap.to_dict()} if snap.exists else None


async def sync_to_sheets(order: dict) -> None:
    """Та же копия в Google Таблицу, что делает сайт CRM при добавлении заказа."""
    if not cfg.sheets_url:
        return
    fields = ("name", "phone", "addr", "date", "time", "sum", "src", "spec", "vol")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            await c.get(cfg.sheets_url, params={k: order.get(k, "") or "" for k in fields})
    except httpx.HTTPError as ex:
        log.warning("Sheets sync failed: %s", ex)


async def delete_doc(collection: str, doc_id: str) -> None:
    await asyncio.to_thread(db.collection(collection).document(doc_id).delete)


async def orders_on(day: str) -> list[dict]:
    q = db.collection("orders").where(filter=firestore.FieldFilter("date", "==", day))
    res = await asyncio.to_thread(_docs, q)
    res = [o for o in res if _status(o) in ("booked", "done")]
    return sorted(res, key=lambda o: o.get("time") or "99:99")


async def orders_between(start: str, end: str) -> list[dict]:
    q = (db.collection("orders")
         .where(filter=firestore.FieldFilter("date", ">=", start))
         .where(filter=firestore.FieldFilter("date", "<=", end)))
    res = await asyncio.to_thread(_docs, q)
    return sorted([o for o in res if _status(o) != "lost"], key=lambda o: (o["date"], o.get("time") or ""))


async def open_leads() -> list[dict]:
    q = db.collection("orders").where(filter=firestore.FieldFilter("status", "in", list(OPEN_STATUSES)))
    res = await asyncio.to_thread(_docs, q)
    return sorted(res, key=lambda o: o.get("createdAt", 0))


async def find_orders(who: str) -> list[dict]:
    """Заявки по телефону или имени, новые сначала."""
    digits = "".join(c for c in who if c.isdigit())
    res = await asyncio.to_thread(_docs, db.collection("orders"))
    def match(o):
        if len(digits) >= 4:
            return digits[-7:] in "".join(c for c in o.get("phone", "") if c.isdigit())
        return who.lower() in (o.get("name") or "").lower()
    return sorted((o for o in res if match(o)), key=lambda o: o.get("createdAt", 0), reverse=True)


# ---------- Планер ----------

# Поля как у сайта планера: s/e — начало и конец, meta — пояснение, type — направление, d — сделано.
def _task_out(t: dict) -> dict:
    return {**t, "start": t.get("s", ""), "end": t.get("e", ""), "note": t.get("meta", ""),
            "area": t.get("type", ""), "done": bool(t.get("d"))}


def _task_in(f: dict) -> dict:
    names = {"start": "s", "end": "e", "note": "meta", "area": "type", "done": "d"}
    return {names.get(k, k): v for k, v in f.items()}


async def add_task(data: dict) -> str:
    task = {k: str(data.get(k) or "") for k in ("date", "start", "end", "title", "note", "area")}
    task["area"] = task["area"] or "work"
    task = _task_in(task) | {"d": False, "createdAt": _now_ms(), "source": "bot"}
    _, ref = await asyncio.to_thread(db.collection("planner_events").add, task)
    return ref.id


async def get_task(task_id: str) -> dict | None:
    snap = await asyncio.to_thread(db.collection("planner_events").document(task_id).get)
    return _task_out({"id": snap.id, **snap.to_dict()}) if snap.exists else None


async def update_task(task_id: str, fields: dict) -> None:
    await asyncio.to_thread(db.collection("planner_events").document(task_id).update, _task_in(fields))


async def tasks_between(start: str, end: str) -> list[dict]:
    q = (db.collection("planner_events")
         .where(filter=firestore.FieldFilter("date", ">=", start))
         .where(filter=firestore.FieldFilter("date", "<=", end)))
    res = [_task_out(t) for t in await asyncio.to_thread(_docs, q) if not t.get("deleted")]
    return sorted(res, key=lambda t: (t["date"], t["start"]))


# ---------- Заметки ----------

async def add_note(kind: str, text: str) -> str:
    _, ref = await asyncio.to_thread(db.collection("notes").add,
                                     {"kind": kind or "note", "text": text, "createdAt": _now_ms()})
    return ref.id


async def recent_notes(limit: int = 15) -> list[dict]:
    q = db.collection("notes").order_by("createdAt", direction=firestore.Query.DESCENDING).limit(limit)
    return await asyncio.to_thread(_docs, q)
