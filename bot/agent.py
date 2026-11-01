"""Агент для личного чата владельца с ботом: ИИ с инструментами CRM, планера, заметок и настроек."""
from . import llm, settings, storage

SYSTEM = f"""Ты личный ассистент владельца выездной химчистки мягкой мебели. {llm.BUSINESS}
Владелец пишет или говорит голосом короткие поручения. Выполняй их инструментами: заявки и заказы в CRM,
дела в планере, заметки и идеи, фразы-триггеры записи. Если для действия не хватает данных, сделай то,
что можно, и коротко спроси недостающее. Не выдумывай телефоны, адреса и суммы.

Статусы заказа: new новая заявка, thinking клиент думает, booked записан, done выполнен, lost отказ.
Направления дел в планере: sales клиенты и рассылки, blog блог и контент, work работа и развитие,
life жизнь, спорт, отдых. Если конец дела не назван, ставь на час позже начала.
Даты вида «завтра», «в четверг» переводи в ГГГГ-ММ-ДД. Ищи заказ перед изменением, если не знаешь его id.

Отвечай по-русски, коротко, обычным текстом без разметки: что сделал, с датами в виде «чт 30.10»."""

S = {"type": "string"}


def _tool(name: str, description: str, props: dict) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": list(props),
                             "additionalProperties": False}}


ORDER_PROPS = {
    "name": S, "phone": S, "addr": S,
    "date": {"type": "string", "description": "ГГГГ-ММ-ДД или пусто"},
    "time": {"type": "string", "description": "ЧЧ:ММ или пусто"},
    "sum": {"type": "string", "description": "число или пусто"},
    "src": {"type": "string", "description": "откуда клиент: Авито, ВК, Telegram, сарафан"},
    "spec": {"type": "string", "description": "мастер"},
    "vol": {"type": "string", "description": "что чистить"},
    "note": S,
    "status": {"type": "string", "enum": ["new", "thinking", "booked", "done", "lost"]},
}

TOOLS = [
    _tool("find_orders", "Найти заказы и заявки по имени или телефону клиента.", {"query": S}),
    _tool("list_orders", "Заказы с датой визита в диапазоне.", {"date_from": S, "date_to": S}),
    _tool("list_open_leads", "Незакрытые заявки: статусы new и thinking.", {}),
    _tool("create_order", "Создать заявку или заказ в CRM.", ORDER_PROPS),
    _tool("update_order", "Изменить заказ. Пустая строка в поле значит «не менять».",
          {"order_id": S, **ORDER_PROPS, "status": {"type": "string",
                                                     "enum": ["", "new", "thinking", "booked", "done", "lost"]}}),
    _tool("list_tasks", "Дела в планере за диапазон дат.", {"date_from": S, "date_to": S}),
    _tool("add_task", "Добавить дело в планер.", {
        "date": S, "start": {"type": "string", "description": "ЧЧ:ММ или пусто"}, "end": S, "title": S, "note": S,
        "area": {"type": "string", "enum": ["sales", "blog", "work", "life"]}}),
    _tool("update_task", "Изменить дело (перенести, переименовать, отметить). Пустая строка значит «не менять».", {
        "task_id": S, "date": S, "start": S, "end": S, "title": S, "note": S,
        "done": {"type": "string", "enum": ["", "yes", "no"]}}),
    _tool("add_note", "Сохранить заметку или идею.",
          {"kind": {"type": "string", "enum": ["note", "idea"]}, "text": S}),
    _tool("recent_notes", "Последние заметки и идеи.", {}),
    _tool("trigger_phrases", "Посмотреть или изменить фразы, по которым бот понимает, что клиент записан.", {
        "action": {"type": "string", "enum": ["list", "add", "remove"]}, "phrase": S}),
]


def _short_order(o: dict) -> dict:
    keys = ("id", "name", "phone", "addr", "date", "time", "sum", "vol", "status", "note")
    return {k: o.get(k, "") for k in keys if o.get(k)}


class Run:
    """Один запрос владельца: выполняет инструменты и запоминает изменения для кнопки «Отменить»."""

    def __init__(self):
        self.changes: list[tuple[str, str, dict | None]] = []  # (коллекция, id, прежние поля или None)
        self.new_orders: list[dict] = []

    async def call(self, name: str, a: dict):
        if name == "find_orders":
            found = await storage.find_orders(a["query"])
            return [_short_order(o) for o in found[:10]]
        if name == "list_orders":
            return [_short_order(o) for o in await storage.orders_between(a["date_from"], a["date_to"])]
        if name == "list_open_leads":
            return [_short_order(o) for o in await storage.open_leads()]
        if name == "create_order":
            oid = await storage.add_order(a)
            self.changes.append(("orders", oid, None))
            self.new_orders.append(a)
            return {"ok": True, "order_id": oid}
        if name == "update_order":
            before = await storage.get_order(a["order_id"])
            if not before:
                return {"error": "заказ не найден"}
            fields = {k: v for k, v in a.items() if k != "order_id" and v}
            await storage.update_order(a["order_id"], fields)
            self.changes.append(("orders", a["order_id"], {k: before.get(k, "") for k in fields}))
            return {"ok": True}
        if name == "list_tasks":
            return await storage.tasks_between(a["date_from"], a["date_to"])
        if name == "add_task":
            tid = await storage.add_task(a)
            self.changes.append(("planner_events", tid, None))
            return {"ok": True, "task_id": tid}
        if name == "update_task":
            fields = {k: v for k, v in a.items() if k not in ("task_id", "done") and v}
            if a["done"]:
                fields["done"] = a["done"] == "yes"
            before = await storage.get_task(a["task_id"])
            if not before:
                return {"error": "дело не найдено"}
            await storage.update_task(a["task_id"], fields)
            self.changes.append(("planner_events", a["task_id"], {k: before.get(k, "") for k in fields}))
            return {"ok": True}
        if name == "add_note":
            nid = await storage.add_note(a["kind"], a["text"])
            self.changes.append(("notes", nid, None))
            return {"ok": True}
        if name == "recent_notes":
            return [{"kind": n.get("kind"), "text": n.get("text")} for n in await storage.recent_notes()]
        if name == "trigger_phrases":
            if a["action"] == "add":
                settings.add_phrase(a["phrase"])
            elif a["action"] == "remove" and not settings.remove_phrase(a["phrase"]):
                return {"error": "такой фразы нет"}
            return {"phrases": settings.load()["trigger_phrases"]}
        return {"error": f"нет инструмента {name}"}


async def undo(changes: list) -> None:
    """Откатить изменения одного поручения (Run.changes) в обратном порядке."""
    for collection, doc_id, before in reversed(changes):
        if before is None:
            await storage.delete_doc(collection, doc_id)
        elif collection == "orders":
            await storage.update_order(doc_id, before)
        else:
            await storage.update_task(doc_id, before)


async def run(text: str, recent: list[tuple[str, str]]) -> tuple[str, Run]:
    """recent: последние обмены (запрос, ответ) для контекста «а ещё добавь телефон»."""
    ctx = ""
    if recent:
        ctx = "Предыдущие сообщения:\n" + "\n".join(f"Я: {q}\nТы: {r}" for q, r in recent) + "\n\n"
    r = Run()
    answer = await llm.run_tools(SYSTEM, f"{llm.now_line()}\n\n{ctx}Сообщение: {text}", TOOLS, r.call)
    return answer, r
