"""Прогон логики без Telegram, Firebase и Claude: заглушки вместо них. Запуск: python -m tests.smoke"""
import asyncio
import json
import os
import sys
import tempfile
import types
from types import SimpleNamespace as NS

os.environ.update({"BOT_TOKEN": "1:x", "OWNER_IDS": "1", "DATA_DIR": tempfile.mkdtemp(), "SHEETS_URL": "",
                   "MAIL_TO": "me@yandex.ru", "SMTP_USER": "me@yandex.ru", "SMTP_PASSWORD": "x"})
fa = types.ModuleType("firebase_admin")
fa.initialize_app = lambda *a, **k: None
fs = types.ModuleType("firebase_admin.firestore")
fs.client = lambda: None
fa.firestore = fs
sys.modules.update({"firebase_admin": fa, "firebase_admin.firestore": fs})

from bot import agent, core, dialogs, llm, mail, settings, storage  # noqa: E402

MAIL: list[tuple[str, str]] = []
mail._send = lambda subject, body: MAIL.append((subject, body))

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

    # Письма: новая заявка, записан, исправлен, записан (ВК)
    await asyncio.sleep(0.3)
    subjects = [m[0] for m in MAIL]
    assert subjects[0].startswith("Новая заявка: Анна"), subjects
    assert "Записан: Анна, " in subjects[1] and "14:00" in subjects[1], subjects
    assert subjects[2].startswith("Исправлен: Анна") and subjects[3].startswith("Записан:") and len(MAIL) == 4, subjects
    assert "Переписка:" in MAIL[0][1] and "угловой диван" in MAIL[0][1], MAIL[0][1]
    assert "Телефон: +79001234567" in MAIL[1][1] and "Адрес: Ленина 5, кв 3" in MAIL[1][1], MAIL[1][1]


async def test_returning_client():
    core.notify = notify
    # Анна записана, визит уже прошёл
    await core.client_message("tg", "88", "Почистите диван? Ленина 5", "Анна")
    await core.owner_message("tg", "88", "Записал, спасибо!", "Анна")
    for _ in range(20):
        await asyncio.sleep(0)
    first = dialogs.get("tg:88")["order_id"]
    DB[first]["date"] = "2020-01-01"
    dialogs._db.execute("UPDATE messages SET ts = ts - 7 * 86400 WHERE key = 'tg:88'")  # это было неделю назад
    # Благодарность после визита: не заявка, но и не «не клиент»
    await core.client_message("tg", "88", "Спасибо, всё отлично", "Анна")
    d = dialogs.get("tg:88")
    assert d["state"] == "unknown" and not d["order_id"] and d["since"], d
    # Через неделю снова пишет про диван: новая заявка, старый заказ не тронут
    await core.client_message("tg", "88", "Здравствуйте, нужно ещё раз почистить диван", "Анна")
    second = dialogs.get("tg:88")["order_id"]
    assert second and second != first and DB[second]["status"] == "new", DB
    assert DB[first]["status"] == "booked" and DB[first]["date"] == "2020-01-01"
    # «Записал» в диалоге, где прошлый заказ уже позади, создаёт новый заказ
    await core.client_message("vk", "99", "Ленина 5, диван", "Олег")
    await core.owner_message("vk", "99", "Записал, спасибо!", "Олег")
    for _ in range(20):
        await asyncio.sleep(0)
    old = dialogs.get("vk:99")["order_id"]
    DB[old]["date"] = "2020-01-01"
    await core.owner_message("vk", "99", "Записал, спасибо!", "Олег")
    for _ in range(20):
        await asyncio.sleep(0)
    new = dialogs.get("vk:99")["order_id"]
    assert new != old and DB[old]["date"] == "2020-01-01" and DB[new]["status"] == "booked", DB

    # ИИ видит только новое обращение
    assert all("Записал" not in m["text"] for m in dialogs.history("tg:88")), dialogs.history("tg:88")


async def test_vk_owner():
    from bot import vk
    sent, agent_calls = [], []

    async def call(c, method, **p):
        if method == "messages.send":
            sent.append((p["peer_id"], p["message"]))
        return {}

    async def run(text, recent):
        agent_calls.append(text)
        return "Записал Наталью на пн 02.11 11:30", NS(new_orders=[], changes=[("orders", "o9", None)])

    real_call, real_run = vk.call, agent.run
    vk.call, agent.run = call, run
    object.__setattr__(vk.cfg, "vk_owner_ids", frozenset({555}))
    upd = lambda kind, peer, text: {"type": kind, "object": {"message": {"peer_id": peer, "text": text}}
                                                              if kind == "message_new" else
                                                              {"peer_id": peer, "text": text, "out": 1}}
    await vk.handle(None, upd("message_new", 777, "/id"))
    assert sent[-1] == (777, "Ваш id ВКонтакте: 777")
    await vk.handle(None, upd("message_new", 555, "Запиши Наталью на завтра 11:30, 3000"))
    assert agent_calls == ["Запиши Наталью на завтра 11:30, 3000"] and "отмена" in sent[-1][1]
    assert dialogs.get("vk:555") is None  # поручение владельца — не заявка клиента
    await vk.handle(None, upd("message_reply", 555, "Записал Наталью"))  # ответ самого бота
    assert len(agent_calls) == 1
    DB["o9"] = {"name": "Наталья"}
    await vk.handle(None, upd("message_new", 555, "Отмена"))
    assert "o9" not in DB and sent[-1][1].startswith("↩️")
    vk.call, agent.run = real_call, real_run


async def test_agent():
    calls = []

    async def deepseek(payload):
        calls.append(payload)
        assert payload["tools"][0]["type"] == "function"
        if len(calls) == 1:
            args = {"name": "Иванов", "date": "2026-10-30", "time": "14:00", "sum": "3500",
                    "vol": "два кресла", "status": "booked"}  # часть полей пропущена, как бывает у модели
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "create_order", "arguments": json.dumps(args)}}]}
        assert payload["messages"][-1]["role"] == "tool"
        return {"role": "assistant", "content": "Записал Иванова на чт 30.10 14:00"}

    llm._deepseek = deepseek
    answer, run = await agent.run("Запиши Иванова на четверг в 14, два кресла, 3500", [])
    assert "Иванова" in answer and run.changes and run.new_orders[0]["status"] == "booked"
    assert run.new_orders[0]["phone"] == ""


async def test_parse_retry():
    replies = iter(['{"name": "Анна"}', json.dumps({"is_client": True, "unclear": False, "name": "Анна", "phone": "",
                                                    "addr": "", "vol": "диван", "summary": "диван"})])

    async def deepseek(payload):
        assert payload["response_format"] == {"type": "json_object"} and "is_client" in payload["messages"][0]["content"]
        return {"role": "assistant", "content": next(replies)}

    llm._deepseek = deepseek
    res = await llm.parse("system", "user", llm.LeadCheck)
    assert res.is_client and res.vol == "диван"


def test_ocr_sides():
    from bot import ocr
    box = lambda x1, x2: {"vertices": [{"x": str(x1), "y": "0"}, {"x": str(x2), "y": "40"}]}
    ann = {"width": "1000", "blocks": [
        {"boundingBox": box(50, 500), "lines": [{"text": "Ломоносова 18"}]},
        {"boundingBox": box(150, 950), "lines": [{"text": "Завтра 11:30 удобно?"}]}]}
    assert ocr.chat_text(ann).splitlines() == ["Клиент: Ломоносова 18", "Я: Завтра 11:30 удобно?"]


def test_telegram_wiring():
    from bot import main
    used = set(main.dp.resolve_used_update_types())
    assert {"business_message", "business_connection", "message", "callback_query"} <= used, used


async def test_without_telegram():
    from bot import main, vk
    ran = []

    async def vk_run():
        ran.append(1)
        await core.notify("<b>Новая заявка</b>", [])

    vk.run, main.cfg = vk_run, main.cfg.__class__(telegram=False)
    await main.main()
    assert ran and core.notify is not notify


test_phrases()
test_telegram_wiring()
test_ocr_sides()
asyncio.run(test_flow())
asyncio.run(test_returning_client())
asyncio.run(test_vk_owner())
asyncio.run(test_agent())
asyncio.run(test_parse_retry())
asyncio.run(test_without_telegram())
print("OK")
