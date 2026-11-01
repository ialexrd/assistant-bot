"""Запросы к ИИ: разбор переписки в заказ, проверка «клиент ли это», тексты-напоминания, агент с инструментами.
Провайдер выбирается в .env: LLM_PROVIDER=deepseek (по умолчанию) или anthropic (Claude)."""
import json
import logging
import re
from datetime import datetime
from typing import Awaitable, Callable

import httpx
from pydantic import BaseModel, Field, ValidationError

from .config import cfg

log = logging.getLogger("llm")

DOW = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

BUSINESS = ("Владелец ведёт выездную химчистку мягкой мебели (диваны, кресла, матрасы, ковры, стулья) "
            "в Калининграде. Клиенты пишут ему в Telegram и ВКонтакте.")


def now_line() -> str:
    now = datetime.now(cfg.tz)
    return f"Сейчас {now:%Y-%m-%d} ({DOW[now.weekday()]}), {now:%H:%M}, часовой пояс Калининград."


# Инструмент агента: имя, описание и JSON-схема аргументов.
Tool = dict
Execute = Callable[[str, dict], Awaitable[object]]

if cfg.llm_provider == "anthropic":
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key or None)
    # Запасная модель на случай отказа основной. На обычную работу не влияет.
    FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


# ---------- DeepSeek (API совместим с OpenAI) ----------

async def _deepseek(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{cfg.deepseek_base_url}/chat/completions", json={"model": cfg.deepseek_model, **payload},
                         headers={"Authorization": f"Bearer {cfg.deepseek_api_key}"})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]


def _example(schema: type[BaseModel]) -> str:
    """Описание полей для подсказки: DeepSeek не принимает JSON-схему, только режим «ответ JSON»."""
    props = schema.model_json_schema()
    defs = props.get("$defs", {})
    def describe(p):
        if "$ref" in p:
            p = defs[p["$ref"].split("/")[-1]]
        if p.get("type") == "object":
            return {k: describe(v) for k, v in p["properties"].items()}
        if p.get("type") == "array":
            return [describe(p["items"])]
        kind = {"string": "строка", "boolean": "true/false", "integer": "число"}.get(p.get("type"), "")
        return f"{kind}: {p['description']}" if p.get("description") else kind
    return json.dumps(describe(props), ensure_ascii=False, indent=1)


async def parse(system: str, user: str, schema: type[BaseModel], max_tokens: int = 4000) -> BaseModel:
    if cfg.llm_provider == "anthropic":
        resp = await client.beta.messages.parse(
            model=cfg.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema, output_config={"effort": cfg.effort}, **FALLBACK,
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise RuntimeError(f"Claude не вернул ответ: {resp.stop_reason}")
        return resp.parsed_output
    messages = [{"role": "system", "content": f"{system}\n\nОтветь только JSON с полями:\n{_example(schema)}"},
                {"role": "user", "content": user}]
    for attempt in range(2):
        msg = await _deepseek({"messages": messages, "max_tokens": max_tokens,
                               "response_format": {"type": "json_object"}})
        try:
            return schema.model_validate_json(msg["content"])
        except ValidationError as ex:
            log.warning("bad JSON from model (attempt %s): %s", attempt + 1, ex)
            messages += [{"role": "assistant", "content": msg["content"]},
                         {"role": "user", "content": f"Ответ не подходит под формат: {ex}. Пришли JSON заново."}]
    raise RuntimeError("ИИ дважды прислал неверный JSON")


def _arg(v, spec: dict):
    if spec.get("type") == "array":
        return v if isinstance(v, list) else []
    return "" if v is None else str(v)


async def run_tools(system: str, user: str, tools: list[Tool], execute: Execute, max_steps: int = 10) -> str:
    """Цикл агента: модель вызывает инструменты, пока не ответит текстом."""
    def result(out) -> str:
        return json.dumps(out, ensure_ascii=False, default=str)

    async def safe(name: str, args: dict) -> tuple[object, bool]:
        try:
            return await execute(name, args), False
        except Exception as ex:
            log.exception("tool %s failed", name)
            return {"error": str(ex)}, True

    if cfg.llm_provider == "anthropic":
        messages = [{"role": "user", "content": user}]
        for _ in range(max_steps):
            resp = await client.beta.messages.create(
                model=cfg.model, max_tokens=8000, system=system, messages=messages,
                tools=[{**t, "strict": True} for t in tools],
                output_config={"effort": cfg.effort}, **FALLBACK,
            )
            if resp.stop_reason == "refusal":
                return "ИИ отказался выполнять это поручение."
            messages.append({"role": "assistant", "content": resp.content})
            calls = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not calls:
                return "".join(b.text for b in resp.content if b.type == "text").strip() or "Готово."
            results = []
            for c in calls:
                out, err = await safe(c.name, c.input)
                results.append({"type": "tool_result", "tool_use_id": c.id, "is_error": err, "content": result(out)})
            messages.append({"role": "user", "content": results})
        return "Слишком много шагов, остановился. Уточни поручение."

    fns = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                             "parameters": t["input_schema"]}} for t in tools]
    props = {t["name"]: t["input_schema"]["properties"] for t in tools}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for _ in range(max_steps):
        msg = await _deepseek({"messages": messages, "tools": fns, "max_tokens": 4000})
        calls = msg.get("tool_calls") or []
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        if not calls:
            return (msg.get("content") or "").strip() or "Готово."
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            # Без строгой схемы модель может пропустить поле: недостающие считаем пустыми.
            args = {k: _arg(args.get(k), spec) for k, spec in props.get(name, {}).items()}
            out, _ = await safe(name, args)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result(out)})
    return "Слишком много шагов, остановился. Уточни поручение."


def format_dialog(history: list[dict], client_name: str) -> str:
    lines = []
    for m in history:
        who = "Я" if m["outgoing"] else (client_name or "Клиент")
        when = datetime.fromtimestamp(m["ts"], cfg.tz).strftime("%d.%m %H:%M")
        lines.append(f"[{when}] {who}: {m['text']}")
    return "\n".join(lines)


# ---------- Заказ из переписки ----------

class PriceItem(BaseModel):
    item: str = Field(description="Что чистим: «диван», «подушки»")
    qty: int = Field(description="Сколько штук, если цена за штуку; иначе 1")
    min: int = Field(description="Цена «от» за штуку в рублях")
    max: int = Field(description="Цена «до» за штуку; если цена одна, то же число")


def _money(n: float) -> str:
    return f"{round(n):,}".replace(",", " ")


def price_summary(items) -> tuple[str, str]:
    """Строки цен для заметки и средняя сумма: половина от суммы всех «от» и всех «до»."""
    rows, lo, hi = [], 0, 0
    for p in items:
        p = p if isinstance(p, dict) else p.model_dump()
        qty = max(int(p.get("qty") or 1), 1)
        a, b = sorted((int(p.get("min") or 0), int(p.get("max") or p.get("min") or 0)))
        if not b:
            continue
        price = _money(a) if a == b else f"{_money(a)}–{_money(b)}"
        rows.append(f"{p.get('item') or 'позиция'}: {price} ₽" + (f"/шт × {qty}" if qty > 1 else ""))
        lo, hi = lo + a * qty, hi + b * qty
    if not rows:
        return "", ""
    total = _money(lo) if lo == hi else f"{_money(lo)}–{_money(hi)}"
    rows.append(f"Итого: {total} ₽, в сумму поставил среднее {_money((lo + hi) / 2)} ₽")
    return "Цены: " + "; ".join(rows), str(round((lo + hi) / 2))


def with_prices(note: str, items, sum_: str) -> tuple[str, str]:
    """Добавляет цены в заметку; если итоговой суммы нет, ставит среднее из вилки."""
    text, avg = price_summary(items or [])
    if not text:
        return note, sum_
    note = re.sub(r"\s*Цены: .*$", "", note or "", flags=re.S).strip()
    return (f"{note}\n{text}" if note else text), (sum_ or avg)


class OrderDraft(BaseModel):
    name: str = Field(description="Имя клиента. Если в переписке не названо, имя из профиля")
    phone: str = Field(description="Телефон как написан, иначе пусто")
    addr: str = Field(description="Адрес: улица, дом, квартира, район или посёлок")
    date: str = Field(description="Дата визита ГГГГ-ММ-ДД, иначе пусто")
    time: str = Field(description="Время визита ЧЧ:ММ, иначе пусто")
    sum: str = Field(description="Только если в переписке названа одна точная итоговая сумма; при ценах «от и до» пусто")
    vol: str = Field(description="Что чистим, коротко: «угловой диван + 2 кресла»")
    note: str = Field(description="Важные детали: пятна, животные, этаж, домофон, как связаться. Коротко. Цены сюда не пиши")
    prices: list[PriceItem] = Field(default_factory=list, description="Каждая позиция с ценой из переписки: что, сколько штук, цена от и до за штуку. Нет цен — пустой список")
    missing: list[str] = Field(description="Чего не хватает из: имя, телефон, адрес, дата, время, сумма")


EXTRACT_SYSTEM = f"""{BUSINESS}
По переписке владельца с клиентом заполни карточку заказа. Бери только то, о чём договорились в итоге:
если дату, время или сумму меняли, бери последний согласованный вариант. Даты вида «завтра», «в четверг»
переводи в ГГГГ-ММ-ДД относительно даты сообщения, в котором они написаны. Ничего не придумывай:
если чего-то нет в переписке, оставь пустым и укажи в missing."""


async def extract_order(dialog_text: str, profile_name: str, current: dict | None = None,
                        correction: str = "") -> OrderDraft:
    parts = [now_line(), f"Имя в профиле клиента: {profile_name or 'неизвестно'}",
             f"Переписка:\n{dialog_text or '(нет сообщений)'}"]
    if current:
        fields = {k: current.get(k, "") for k in OrderDraft.model_fields if k != "missing"}
        parts.append("Текущая карточка заказа:\n" + json.dumps(fields, ensure_ascii=False))
    if correction:
        parts.append(f"Владелец просит исправить карточку так: {correction}\n"
                     "Примени исправление к текущей карточке, остальное оставь как есть.")
    draft = await parse(EXTRACT_SYSTEM, "\n\n".join(parts), OrderDraft)
    if draft.prices:
        draft.note, draft.sum = with_prices(draft.note, draft.prices, draft.sum)
        if draft.sum:
            draft.missing = [m for m in draft.missing if m != "сумма"]
    return draft


# ---------- Новый клиент или нет ----------

class LeadCheck(BaseModel):
    is_client: bool = Field(description="Человек интересуется чисткой мебели или другой услугой владельца")
    unclear: bool = Field(description="Пока нельзя понять: только приветствие или общий вопрос")
    name: str = Field(description="Имя, если назвал")
    phone: str = Field(description="Телефон, если написал")
    addr: str = Field(description="Адрес или район, если написал")
    vol: str = Field(description="Что хочет почистить, коротко")
    summary: str = Field(description="Суть обращения одной фразой")


LEAD_SYSTEM = f"""{BUSINESS}
Тебе показывают начало переписки с человеком, который написал владельцу. Определи, клиент ли это
(спрашивает про чистку, цену, запись, сроки) или нет (друг, родственник, поставщик, спам, реклама).
Если по сообщениям пока нельзя понять, поставь unclear."""


async def check_lead(dialog_text: str, profile_name: str) -> LeadCheck:
    user = f"Имя в профиле: {profile_name or 'неизвестно'}\n\nПереписка:\n{dialog_text}"
    return await parse(LEAD_SYSTEM, user, LeadCheck, 2000)


# ---------- Тексты для незакрытых заявок ----------

class Followup(BaseModel):
    id: str
    text: str


class Followups(BaseModel):
    messages: list[Followup]


FOLLOWUP_SYSTEM = f"""{BUSINESS}
Владелец хочет вернуть клиентов, которые обратились, но не записались. Для каждого напиши короткое,
тёплое, неназойливое сообщение (2–3 предложения) от первого лица, на «Вы», без шаблонных фраз и без
лишних смайликов. Упомяни, что хотели почистить, и предложи записаться на ближайшие дни."""


async def followup_texts(leads: list[dict]) -> dict[str, str]:
    if not leads:
        return {}
    payload = [{"id": l["id"], "name": l.get("name", ""), "vol": l.get("vol", ""),
                "note": l.get("note", ""), "created": l.get("created", "")} for l in leads]
    res = await parse(FOLLOWUP_SYSTEM, now_line() + "\n\n" + json.dumps(payload, ensure_ascii=False),
                      Followups, 6000)
    return {m.id: m.text for m in res.messages}
