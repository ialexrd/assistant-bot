"""Прогон логики без Telegram, Firebase и Claude: заглушки вместо них. Запуск: python -m tests.smoke"""
import asyncio
import os
import sys
import tempfile
import types
from types import SimpleNamespace as NS

os.environ.update({"BOT_TOKEN": "1:x", "OWNER_IDS": "1", "DATA_DIR": tempfile.mkdtemp(), "SHEETS_URL": ""})
fa = types.ModuleType("firebase_admin")
fa.initialize_app = lambda *a, **k: None
fs = types.ModuleType("firebase_admin.firestore")
fs.client = lambda: None
fa.firestore = fs
sys.modules.update({"firebase_admin": fa, "firebase_admin.firestore": fs})

from bot import agent, core, dialogs, llm, settings, storage  # noqa: E402

# ---------- Заглушка CRM ----------
DB: dict[str, dict] = {}


async def add_order(d):
    i = f"o{len(DB) + 1}"
    DB[i] = {k: v for k, v in d.items() if v} | {"status": d.get("status") or "new"}
    return i


async def get_order(i):
    return {"id": i, **DB[i]} if i in DB else None


async def update_order(i, f):
    DB[i].update(f)


async def delete_doc(col, i):
    DB.pop(i, None)


async def noop(*a, **k):
    pass


storage.add_order, storage.get_order, storage.update_order = add_order, get_order, update_order
storage.delete_doc, storage.sync_to_sheets = delete_doc, noop

# ---------- Заглушка уведомлений ----------
SENT: list[tuple[str, list]] = []


async def notify(text, buttons):
    SENT.append((text, buttons))


core.notify = notify


# ---------- Заглушки Claude ----------
async def check_lead(dialog, name):
    is_client = "диван" in dialog
    return llm.LeadCheck(is_client=is_client, unclear=dialog.rstrip().endswith("Здравствуйте"), name="", phone="",
                         addr="", vol="диван" if is_client else "", summary="Хочет почистить диван")


async def extract_order(dialog, name, current=None, correction=""):
    assert "Записал, спасибо" in dialog and "Ленина 5" in dialog, dialog
    d = dict(name="Анна", phone="+79001234567", addr="Ленина 5, кв 3", date="2026-10-30", time="14:00",
             sum="4500", vol="угловой диван", note="", missing=[])
    if correction:
        d["time"] = "15:00"
    return llm.OrderDraft(**d)


llm.check_lead, llm.extract_order = check_lead, extract_order


def test_phrases():
    assert settings.matched_phrase("Записал, спасибо!") == "записал"
    assert settings.matched_phrase("Анна, записала вас на четверг 🙂")
    assert settings.matched_phrase("Хорошо, Вы записаны")
    assert not settings.matched_phrase("Могу записать вас на четверг?")
    assert not settings.matched_phrase("Спасибо, до свидания")
    settings.add_phrase("Ждём вас")
    assert settings.matched_phrase("ждем вас завтра") == "ждём вас"
    assert settings.remove_phrase("ждём вас")


async def test_flow():
    core.notify = notify  # bot.main подставляет отправку в Telegram, здесь собираем в список
    # Друг пишет: не клиент, заявки нет
    await core.client_message("tg", "50", "Привет, как дела? Пиво в субботу?", "Петя")
    assert not DB and dialogs.get("tg:50")["state"] == "not_client"

    # Клиент: сначала просто «Здравствуйте», потом про диван
    await core.client_message("tg", "77", "Здравствуйте", "Анна", "anna")
    assert not DB and dialogs.get("tg:77")["state"] == "unknown"
    await core.client_message("tg", "77", "Сколько стоит почистить угловой диван?", "Анна", "anna")
    assert DB["o1"]["status"] == "new" and DB["o1"]["src"] == "Telegram", DB
    assert "Новая заявка" in SENT[-1][0]

    # Договорились, владелец пишет «Записал, спасибо!»
    await core.client_message("tg", "77", "Давайте в четверг в 14, Ленина 5 кв 3, +79001234567", "Анна", "anna")
    await core.owner_message("tg", "77", "Записал, спасибо!", "Анна", "anna")
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(DB) == 1 and DB["o1"]["status"] == "booked" and DB["o1"]["time"] == "14:00", DB
    text, buttons = SENT[-1]
    assert "Записал в CRM" in text and [b[0] for b in buttons] == ["👌 Ок", "✏️ Исправить", "↩️ Отменить"]
    token = buttons[1][1].split(":")[2]

    # Исправление словами
    card = await core.correct(token, "время 15:00")
    assert DB["o1"]["time"] == "15:00" and "Исправил" in card

    # Отмена возвращает заявку в «новую»
    assert await core.undo(token)
    assert DB["o1"]["status"] == "new"

    # Тот же триггер в чате без заявки создаёт заказ сразу
    await core.client_message("vk", "300", "Ленина 5", "Олег")
    await core.owner_message("vk", "300", "Записал, спасибо!", "Олег")
    for _ in range(20):
        await asyncio.sleep(0)
    assert DB["o2"]["status"] == "booked" and DB["o2"]["src"] == "ВКонтакте", DB


async def test_agent():
    calls = []

    async def create(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return NS(stop_reason="tool_use", content=[
                NS(type="tool_use", id="t1", name="create_order", input={
                    "name": "Иванов", "phone": "", "addr": "", "date": "2026-10-30", "time": "14:00", "sum": "3500",
                    "src": "", "spec": "", "vol": "два кресла", "note": "", "status": "booked"})])
        assert kw["messages"][-1]["content"][0]["type"] == "tool_result"
        return NS(stop_reason="end_turn", content=[NS(type="text", text="Записал Иванова на чт 30.10 14:00")])

    llm.client = NS(beta=NS(messages=NS(create=create)))
    answer, run = await agent.run("Запиши Иванова на четверг в 14, два кресла, 3500", [])
    assert "Иванова" in answer and run.changes and run.new_orders[0]["status"] == "booked"
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in agent.TOOLS)


def test_telegram_wiring():
    from bot import main
    used = set(main.dp.resolve_used_update_types())
    assert {"business_message", "business_connection", "message", "callback_query"} <= used, used


test_phrases()
test_telegram_wiring()
asyncio.run(test_flow())
asyncio.run(test_agent())
print("OK")
