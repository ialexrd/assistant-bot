"""Общая логика для всех каналов: новые клиенты → заявка, фраза «Записал» → заказ с подтверждением."""
import asyncio
import logging
import secrets
import time
from collections import defaultdict
from typing import Awaitable, Callable

from . import dialogs, llm, mail, settings, storage
from .fmt import CHANNEL_RU, e, order_card

log = logging.getLogger("core")

# Отправка уведомления владельцу: (текст в HTML, кнопки [(надпись, callback_data)]). Задаёт Telegram-часть.
Notify = Callable[[str, list[tuple[str, str]]], Awaitable[None]]
notify: Notify | None = None

ORDER_KEYS = ("name", "phone", "addr", "date", "time", "sum", "vol", "note")
LEAD_CHECKS = 4            # сколько первых сообщений даём ИИ, чтобы понять, клиент ли это
NEW_CYCLE_GAP = 14 * 86400  # если клиент вернулся после закрытого заказа и паузы, это новая заявка

_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _notify(text: str, buttons: list[tuple[str, str]] | None = None) -> None:
    if notify:
        await notify(text, buttons or [])


def _who(d: dict) -> str:
    name = d.get("name") or "клиент"
    return f"{name} (@{d['username']})" if d.get("username") else name


def _dialog_for_mail(key: str, d: dict) -> str:
    return "Переписка:\n" + llm.format_dialog(dialogs.history(key, 30), d["name"])


# ---------- Входящие от клиентов ----------

async def client_message(channel: str, chat_id: str, text: str, name: str = "", username: str = "") -> None:
    key = f"{channel}:{chat_id}"
    async with _locks[key]:
        before = dialogs.get(key)
        d = dialogs.ensure(key, channel, name, username)
        last = dialogs.history(key, 1)
        dialogs.add_message(key, False, text)
        if before and d["order_id"] and last and time.time() - last[-1]["ts"] > NEW_CYCLE_GAP:
            o = await storage.get_order(d["order_id"])
            if not o or o.get("status") in ("done", "lost"):
                dialogs.update(key, order_id="", state="unknown", checks=0)
                d = dialogs.get(key)
        if settings.load()["auto_leads"] and d["state"] == "unknown" and not d["order_id"] \
                and d["checks"] < LEAD_CHECKS:
            await _check_lead(key, d)


async def _check_lead(key: str, d: dict) -> None:
    dialogs.update(key, checks=d["checks"] + 1)
    try:
        res = await llm.check_lead(llm.format_dialog(dialogs.history(key, 10), d["name"]), d["name"])
    except Exception:
        log.exception("lead check failed for %s", key)
        return
    if not res.is_client:
        if not res.unclear:
            dialogs.update(key, state="not_client")
        return
    contact = f"{CHANNEL_RU[d['channel']]}: @{d['username']}" if d.get("username") else CHANNEL_RU[d["channel"]]
    lead = {"name": res.name or d["name"], "phone": res.phone, "addr": res.addr, "vol": res.vol,
            "src": CHANNEL_RU[d["channel"]], "status": "new",
            "note": "; ".join(x for x in (res.summary, contact) if x)}
    oid = await storage.add_order(lead)
    dialogs.update(key, order_id=oid, state="lead")
    asyncio.create_task(mail.order("Новая заявка", lead, f"{CHANNEL_RU[d['channel']]}: {_who(d)}\n\n"
                                   + _dialog_for_mail(key, d)))
    await _notify(f"🆕 <b>Новая заявка</b> из {CHANNEL_RU[d['channel']]}\n{e(_who(d))}\n"
                  f"{e(res.summary)}\n\nЗаписал в CRM со статусом «новая».",
                  [("🙅 Не клиент", f"notclient:{key}:{oid}")])


async def not_client(key: str, order_id: str) -> None:
    """Владелец нажал «Не клиент»: убираем заявку и больше не проверяем этот диалог."""
    o = await storage.get_order(order_id)
    if o and o.get("status") == "new":
        await storage.delete_doc("orders", order_id)
    dialogs.update(key, order_id="", state="not_client")


# ---------- Сообщения владельца клиенту ----------

async def owner_message(channel: str, chat_id: str, text: str, name: str = "", username: str = "") -> None:
    key = f"{channel}:{chat_id}"
    async with _locks[key]:
        dialogs.ensure(key, channel, name, username)
        dialogs.add_message(key, True, text)
    phrase = settings.matched_phrase(text)
    if phrase:
        log.info("trigger «%s» in %s", phrase, key)
        asyncio.create_task(_safe(book(key)))


async def _safe(coro) -> None:
    try:
        await coro
    except Exception:
        log.exception("background task failed")
        await _notify("⚠️ Не получилось оформить заказ из переписки, ИИ или CRM не ответили. Посмотри логи.")


async def book(key: str) -> None:
    """Разбирает переписку и записывает заказ в CRM: обновляет заявку диалога или создаёт новый заказ."""
    async with _locks[key]:
        d = dialogs.get(key)
        current = await storage.get_order(d["order_id"]) if d["order_id"] else None
        draft = await llm.extract_order(llm.format_dialog(dialogs.history(key), d["name"]), d["name"], current)
        fields = {k: getattr(draft, k) for k in ORDER_KEYS if getattr(draft, k)}
        fields["status"] = "booked"
        if current:
            oid = current["id"]
            before = {k: current.get(k, "") for k in fields}
            await storage.update_order(oid, fields)
        else:
            fields["src"] = CHANNEL_RU[d["channel"]]
            oid = await storage.add_order(fields)
            before = None
        dialogs.update(key, order_id=oid, state="lead")
    if not current or current.get("status") != "booked":
        asyncio.create_task(storage.sync_to_sheets(fields))
    order = (current or {}) | fields
    missing = "Не нашёл в переписке: " + ", ".join(draft.missing) + "\n\n" if draft.missing else ""
    asyncio.create_task(mail.order("Записан", order, missing + _dialog_for_mail(key, d)))
    token = secrets.token_hex(4)
    dialogs.kv_set(f"card:{token}", {"key": key, "order_id": oid, "before": before})
    await _notify(card_text(order, draft.missing, d), card_buttons(token))


def card_text(order: dict, missing: list[str], d: dict, header: str = "✅ <b>Записал в CRM</b>") -> str:
    text = f"{header} · {e(_who(d))}\n\n{order_card(order)}"
    if missing:
        text += f"\n\n⚠️ Не нашёл в переписке: {e(', '.join(missing))}"
    return text


def card_buttons(token: str) -> list[tuple[str, str]]:
    return [("👌 Ок", f"card:ok:{token}"), ("✏️ Исправить", f"card:fix:{token}"),
            ("↩️ Отменить", f"card:undo:{token}")]


async def correct(token: str, correction: str) -> str | None:
    """Исправление карточки словами владельца. Возвращает новый текст карточки."""
    card = dialogs.kv_get(f"card:{token}")
    if not card:
        return None
    current = await storage.get_order(card["order_id"])
    if not current:
        return None
    d = dialogs.get(card["key"])
    draft = await llm.extract_order(llm.format_dialog(dialogs.history(card["key"]), d["name"]),
                                    d["name"], current, correction)
    fields = {k: getattr(draft, k) for k in ORDER_KEYS if getattr(draft, k) != (current.get(k) or "")}
    if fields:
        await storage.update_order(card["order_id"], fields)
        asyncio.create_task(mail.order("Исправлен", current | fields))
    return card_text(current | fields, draft.missing, d, "✏️ <b>Исправил</b>")


async def undo(token: str) -> bool:
    card = dialogs.kv_pop(f"card:{token}")
    if not card:
        return False
    if card["before"] is None:
        await storage.delete_doc("orders", card["order_id"])
        dialogs.update(card["key"], order_id="")
    else:
        await storage.update_order(card["order_id"], card["before"])
    return True
