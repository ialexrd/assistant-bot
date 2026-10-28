"""Разовые запросы к Claude: разбор переписки в заказ, проверка «клиент ли это», тексты-напоминания."""
import json
from datetime import datetime

import anthropic
from pydantic import BaseModel, Field

from .config import cfg

client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key or None)

DOW = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

BUSINESS = ("Владелец ведёт выездную химчистку мягкой мебели (диваны, кресла, матрасы, ковры, стулья) "
            "в Калининграде. Клиенты пишут ему в Telegram и ВКонтакте.")


def now_line() -> str:
    now = datetime.now(cfg.tz)
    return f"Сейчас {now:%Y-%m-%d} ({DOW[now.weekday()]}), {now:%H:%M}, часовой пояс Калининград."


# Запасная модель на случай отказа основной. Включено по умолчанию, на обычную работу не влияет.
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


async def parse(system: str, user: str, schema: type[BaseModel], max_tokens: int = 4000) -> BaseModel:
    resp = await client.beta.messages.parse(
        model=cfg.model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema, output_config={"effort": cfg.effort}, **FALLBACK,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"Claude не вернул ответ: {resp.stop_reason}")
    return resp.parsed_output


def format_dialog(history: list[dict], client_name: str) -> str:
    lines = []
    for m in history:
        who = "Я" if m["outgoing"] else (client_name or "Клиент")
        when = datetime.fromtimestamp(m["ts"], cfg.tz).strftime("%d.%m %H:%M")
        lines.append(f"[{when}] {who}: {m['text']}")
    return "\n".join(lines)


# ---------- Заказ из переписки ----------

class OrderDraft(BaseModel):
    name: str = Field(description="Имя клиента. Если в переписке не названо, имя из профиля")
    phone: str = Field(description="Телефон как написан, иначе пусто")
    addr: str = Field(description="Адрес: улица, дом, квартира, район или посёлок")
    date: str = Field(description="Дата визита ГГГГ-ММ-ДД, иначе пусто")
    time: str = Field(description="Время визита ЧЧ:ММ, иначе пусто")
    sum: str = Field(description="Итоговая согласованная сумма, только число, иначе пусто")
    vol: str = Field(description="Что чистим, коротко: «угловой диван + 2 кресла»")
    note: str = Field(description="Важные детали: пятна, животные, этаж, домофон, как связаться. Коротко")
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
    return await parse(EXTRACT_SYSTEM, "\n\n".join(parts), OrderDraft)


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
