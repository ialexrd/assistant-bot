import asyncio
import logging
import re
import secrets
from collections import defaultdict, deque
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import (BusinessConnection, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import agent, core, dialogs, llm, mail, settings, storage, stt, vk
from .config import cfg
from .fmt import DOW_SHORT, e, human_date, today
from .fmt import order as fmt_order
from .fmt import task as fmt_task

log = logging.getLogger("assistant")
bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML"),
          session=AiohttpSession(proxy=cfg.telegram_proxy) if cfg.telegram_proxy else None)
dp = Dispatcher()

_recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=3))  # последние обмены с агентом
_undo: dict[str, list] = {}  # токен -> изменения агента для кнопки «Отменить»


def is_owner(user_id: int) -> bool:
    return user_id in cfg.owner_ids


def kb(buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in buttons]])


async def notify_owners(text: str, buttons: list[tuple[str, str]]) -> None:
    for uid in cfg.owner_ids:
        try:
            await bot.send_message(uid, text, reply_markup=kb(buttons))
        except Exception:
            log.exception("notify %s failed", uid)


core.notify = notify_owners


# ---------- Переписка с клиентами через Telegram Business ----------

async def message_text(msg: Message, transcribe: bool) -> str:
    if msg.text:
        return msg.text
    if msg.voice or msg.video_note:
        media = msg.voice or msg.video_note
        if transcribe:
            try:
                buf = await bot.download(media)
                text = await stt.transcribe(buf.read(), media.duration or 0)
                if text.strip():
                    return f"[голосовое] {text}"
            except Exception:
                log.exception("stt failed")
        return "[голосовое сообщение]"
    if msg.contact:
        return f"[контакт] {msg.contact.first_name or ''} {msg.contact.phone_number}"
    if msg.location:
        return f"[геопозиция] {msg.location.latitude}, {msg.location.longitude}"
    kind = "фото" if msg.photo else "видео" if msg.video else "файл" if msg.document else "вложение"
    return f"[{kind}] {msg.caption or ''}".strip()


@dp.business_connection()
async def on_business_connection(conn: BusinessConnection):
    state = "подключён ✅" if conn.is_enabled else "отключён"
    log.info("business connection %s: %s", conn.user.id, state)
    if is_owner(conn.user.id):
        await bot.send_message(conn.user_chat_id, f"Бот {state} к вашему аккаунту Telegram. "
                               "Теперь я вижу переписку с клиентами.")
    else:
        await bot.send_message(conn.user_chat_id, f"Ваш id {conn.user.id} не указан в OWNER_IDS, "
                               "переписку я обрабатывать не буду.")


@dp.business_message()
async def on_business_message(msg: Message):
    if msg.sender_business_bot or msg.chat.type != "private" or not msg.from_user:
        return
    chat = msg.chat
    name = " ".join(x for x in (chat.first_name, chat.last_name) if x)
    outgoing = is_owner(msg.from_user.id)
    text = await message_text(msg, cfg.transcribe_client_voice or outgoing)
    if outgoing:
        await core.owner_message("tg", str(chat.id), text, name, chat.username or "")
    else:
        await core.client_message("tg", str(chat.id), text, name, chat.username or "")


# ---------- Карточки заказа: Ок / Исправить / Отменить ----------

@dp.callback_query(F.data.startswith("card:"))
async def on_card(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        return
    _, action, token = cb.data.split(":", 2)
    if action == "ok":
        dialogs.kv_pop(f"card:{token}")
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.answer("Отлично")
    elif action == "fix":
        if dialogs.kv_get(f"card:{token}") is None:
            await cb.answer("Карточка уже закрыта")
            return
        dialogs.kv_set(f"fix:{cb.from_user.id}", token)
        await cb.message.answer("Напишите или скажите голосом, что исправить. Например: «время 15:00, сумма 4500».")
        await cb.answer()
    elif action == "undo":
        ok = await core.undo(token)
        if ok:
            await cb.message.edit_text(cb.message.html_text + "\n\n<i>↩️ Отменено, CRM вернул как было</i>")
        await cb.answer("Отменено" if ok else "Уже нельзя отменить")


@dp.callback_query(F.data.startswith("notclient:"))
async def on_not_client(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        return
    _, channel, chat_id, oid = cb.data.split(":", 3)
    await core.not_client(f"{channel}:{chat_id}", oid)
    await cb.message.edit_text(cb.message.html_text + "\n\n<i>Убрал из CRM, этот диалог больше не трогаю</i>")
    await cb.answer()


async def apply_fix(msg: Message, token: str, text: str) -> None:
    dialogs.kv_pop(f"fix:{msg.from_user.id}")
    try:
        card = await core.correct(token, text)
    except Exception:
        log.exception("correction failed")
        await msg.answer("Не получилось исправить, ИИ не ответил. Нажмите «Исправить» ещё раз.")
        return
    if card is None:
        await msg.answer("Эта карточка уже закрыта или заказ удалён.")
        return
    await msg.answer(card, reply_markup=kb(core.card_buttons(token)))


# ---------- Личный чат владельца с ботом ----------

async def handle_owner_text(msg: Message, text: str) -> None:
    token = dialogs.kv_get(f"fix:{msg.from_user.id}")
    if token:
        await apply_fix(msg, token, text)
        return
    await bot.send_chat_action(msg.chat.id, "typing")
    try:
        answer, run = await agent.run(text, list(_recent[msg.from_user.id]))
    except Exception:
        log.exception("agent failed")
        await msg.answer("ИИ не ответил, попробуйте ещё раз.")
        return
    _recent[msg.from_user.id].append((text, answer))
    for o in run.new_orders:
        if o.get("status") == "booked":
            asyncio.create_task(storage.sync_to_sheets(o))
        asyncio.create_task(mail.order("Записан" if o.get("status") == "booked" else "Новая заявка", o))
    markup = None
    if run.changes:
        undo_token = secrets.token_hex(4)
        _undo[undo_token] = run.changes
        markup = kb([("↩️ Отменить", f"undo:{undo_token}")])
    await msg.answer(answer, parse_mode=None, reply_markup=markup)


@dp.callback_query(F.data.startswith("undo:"))
async def on_undo(cb: CallbackQuery):
    changes = _undo.pop(cb.data.split(":", 1)[1], None)
    if not changes or not is_owner(cb.from_user.id):
        await cb.answer("Уже нельзя отменить")
        return
    await agent.undo(changes)
    await cb.message.edit_text(e(cb.message.text) + "\n\n<i>↩️ Отменено</i>")
    await cb.answer("Отменено")


@dp.message(Command("id"))
async def cmd_id(msg: Message):
    await msg.answer(f"Ваш Telegram id: <code>{msg.from_user.id}</code>\nId этого чата: <code>{msg.chat.id}</code>")


@dp.message(Command("start", "help"))
async def cmd_start(msg: Message):
    if not is_owner(msg.from_user.id):
        await msg.answer(f"Привет! Передайте владельцу бота свой id: <code>{msg.from_user.id}</code>")
        return
    phrases = ", ".join(f"«{p}»" for p in settings.load()["trigger_phrases"])
    await msg.answer(
        "Я вижу вашу переписку с клиентами. Новые обращения сам заношу в CRM как заявки. "
        f"Когда вы пишете клиенту {phrases}, оформляю заказ по переписке и присылаю сюда на проверку.\n\n"
        "Сюда можно писать или говорить голосом, например:\n"
        "• «Запиши Иванова на четверг в 14, два кресла, 3500»\n"
        "• «Анна отказалась»\n"
        "• «Поставь на завтра в 10 рассылку клинингу»\n"
        "• «Добавь фразу-триггер „ждём вас“»\n\n"
        "Команды: /today /tomorrow /week /orders /leads /notes /phrases /summary")


@dp.message(Command("phrases"))
async def cmd_phrases(msg: Message):
    if is_owner(msg.from_user.id):
        phrases = "\n".join(f"• {e(p)}" for p in settings.load()["trigger_phrases"])
        await msg.answer(f"<b>Фразы, после которых оформляю заказ:</b>\n{phrases}\n\n"
                         "Чтобы изменить, напишите: «добавь фразу …» или «убери фразу …».")


async def show(msg: Message, what: str) -> None:
    t = today().date()
    if what in ("today", "tomorrow"):
        d = str(t if what == "today" else t + timedelta(days=1))
        await msg.answer(await day_digest(d, f"<b>{'Сегодня' if what == 'today' else 'Завтра'}</b>"))
    elif what == "week":
        tasks = await storage.tasks_between(str(t), str(t + timedelta(days=6)))
        out, cur = [], None
        for x in tasks:
            if x["date"] != cur:
                cur = x["date"]
                out.append(f"\n<b>{human_date(cur)}</b>")
            out.append(fmt_task(x))
        await msg.answer("<b>Неделя</b>" + ("\n".join(out) or "\nпусто"))
    elif what == "orders":
        orders = await storage.orders_between(str(t), str(t + timedelta(days=7)))
        body = "\n\n".join(f"<b>{human_date(o['date'])}</b> {fmt_order(o)}" for o in orders)
        await msg.answer("<b>Заказы на неделю</b>\n\n" + (body or "нет"))
    elif what == "leads":
        await send_leads(msg.chat.id)
    elif what == "notes":
        notes = await storage.recent_notes()
        body = "\n".join(f"{'💡' if n.get('kind') == 'idea' else '📝'} {e(n['text'])}" for n in notes)
        await msg.answer("<b>Последние заметки</b>\n" + (body or "пусто"))


SHOW_CMDS = ("today", "tomorrow", "week", "orders", "leads", "notes")


@dp.message(Command(*SHOW_CMDS))
async def cmd_show(msg: Message):
    if is_owner(msg.from_user.id):
        await show(msg, msg.text.split()[0].lstrip("/").split("@")[0])


@dp.message(Command("summary"))
async def cmd_summary(msg: Message):
    if is_owner(msg.from_user.id):
        await master_summary_job()


@dp.message(F.chat.type == "private", F.voice | F.audio)
async def on_voice(msg: Message):
    if not is_owner(msg.from_user.id):
        return
    media = msg.voice or msg.audio
    try:
        buf = await bot.download(media)
        text = await stt.transcribe(buf.read(), media.duration or 0)
    except Exception:
        log.exception("stt failed")
        text = ""
    if not text.strip():
        await msg.answer("Не смог распознать голосовое.")
        return
    await msg.answer(f"🎙 <i>{e(text)}</i>")
    await handle_owner_text(msg, text)


@dp.message(F.chat.type == "private", F.text)
async def on_text(msg: Message):
    if is_owner(msg.from_user.id):
        await handle_owner_text(msg, msg.text)


# ---------- Незакрытые заявки и плановые рассылки ----------

async def day_digest(d: str, header: str) -> str:
    tasks = await storage.tasks_between(d, d)
    orders = await storage.orders_on(d)
    parts = [header, "\n🗓 <b>Планер</b>\n" + ("\n".join(fmt_task(x) for x in tasks) or "пусто")]
    if orders:
        parts.append("\n🧽 <b>Заказы</b>\n" + "\n\n".join(fmt_order(o) for o in orders))
    return "\n".join(parts)


def lead_kb(oid: str) -> InlineKeyboardMarkup:
    return kb([("✅ Записался", f"lead:booked:{oid}"), ("🤔 Думает", f"lead:thinking:{oid}"),
               ("❌ Отказ", f"lead:lost:{oid}")])


async def send_leads(chat_id: int | str) -> None:
    leads = await storage.open_leads()
    if not leads:
        await bot.send_message(chat_id, "Незакрытых заявок нет 👌")
        return
    for l in leads:
        l["created"] = datetime.fromtimestamp(l.get("createdAt", 0) / 1000, cfg.tz).strftime("%d.%m")
    try:
        texts = await llm.followup_texts(leads)
    except Exception:  # текст не критичен, список всё равно отправим
        log.exception("followup texts failed")
        texts = {}
    await bot.send_message(chat_id, f"<b>Незакрытые заявки: {len(leads)}</b>\n"
                                    "Ниже готовые тексты, нажмите на текст, чтобы скопировать.")
    for l in leads:
        head = (f"<b>{e(l.get('name'))}</b> · {e(l.get('phone') or l.get('src'))} · с {l['created']}, "
                f"{storage.STATUS_RU.get(l.get('status'), '')}")
        if l.get("vol"):
            head += f"\n🛋 {e(l['vol'])}"
        text = texts.get(l["id"])
        body = f"\n\n<code>{e(text)}</code>" if text else ""
        await bot.send_message(chat_id, head + body, reply_markup=lead_kb(l["id"]))


@dp.callback_query(F.data.startswith("lead:"))
async def on_lead(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        return
    _, status, oid = cb.data.split(":", 2)
    await storage.update_order(oid, {"status": status})
    label = storage.STATUS_RU[status]
    await cb.message.edit_text(cb.message.html_text + f"\n\n<i>→ {label}</i>")
    await cb.answer(label)


async def morning_job() -> None:
    d = str(today().date())
    for uid in cfg.owner_ids:
        try:
            await bot.send_message(uid, await day_digest(d, "☀️ <b>Доброе утро! План на сегодня</b>"))
            await send_leads(uid)
        except Exception:
            log.exception("morning job failed for %s", uid)


async def master_summary_job() -> None:
    if not cfg.master_chat_id:
        await notify_owners("MASTER_CHAT_ID не задан, сводку мастеру отправить некуда.", [])
        return
    d = today().date() + timedelta(days=1)
    orders = await storage.orders_on(str(d))
    if not orders:
        text = f"Заказов на завтра ({d:%d.%m}) нет."
    else:
        total = sum(int(o["sum"]) for o in orders if str(o.get("sum", "")).isdigit())
        text = (f"🧽 <b>Заказы на завтра, {DOW_SHORT[d.weekday()]} {d:%d.%m}</b> ({len(orders)})\n\n"
                + "\n\n".join(fmt_order(o) for o in orders)
                + (f"\n\nИтого: {total:,} ₽".replace(",", " ") if total else ""))
    await bot.send_message(cfg.master_chat_id, text)
    await notify_owners("Мастеру отправлена сводка:\n\n" + text, [])


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    sched = AsyncIOScheduler(timezone=cfg.tz)
    h, m = map(int, cfg.morning_time.split(":"))
    sched.add_job(morning_job, "cron", hour=h, minute=m)
    h, m = map(int, cfg.master_summary_time.split(":"))
    sched.add_job(master_summary_job, "cron", hour=h, minute=m)
    if not cfg.telegram:
        # Без Telegram: только ВК, CRM и почта. Уведомления пишем в журнал.
        async def log_notify(text: str, buttons: list[tuple[str, str]]) -> None:
            log.info("Уведомление: %s", re.sub(r"<[^>]+>", "", text))
        core.notify = vk.notify_owners if cfg.vk_owner_ids else log_notify
        log.info("TELEGRAM=off: работаю только с ВКонтакте")
        await vk.run()
        return
    if cfg.vk_owner_ids:
        # Уведомления и в Telegram, и в ВК, пока владелец пользуется обоими
        async def notify_both(text: str, buttons: list[tuple[str, str]]) -> None:
            await notify_owners(text, buttons)
            await vk.notify_owners(text, buttons)
        core.notify = notify_both
    sched.start()
    vk_task = asyncio.create_task(vk.run()) if cfg.vk_token else None
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception:
        # Ошибка Telegram (неверный токен, нет связи) не должна останавливать ВКонтакте
        log.exception("Telegram не запустился, работаю дальше только с ВКонтакте")
        if not vk_task:
            raise
        if cfg.vk_owner_ids:
            core.notify = vk.notify_owners
        await vk_task


if __name__ == "__main__":
    asyncio.run(main())
