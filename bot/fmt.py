"""Оформление сообщений бота (HTML для Telegram)."""
import html
from datetime import datetime, timedelta

from .config import cfg

DOW_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
CHANNEL_RU = {"tg": "Telegram", "vk": "ВКонтакте", "max": "Макс"}


def e(s) -> str:
    return html.escape(str(s or ""))


def today() -> datetime:
    return datetime.now(cfg.tz)


def human_date(d: str) -> str:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return d or "без даты"
    t = today().date()
    if dt.date() == t:
        return "сегодня"
    if dt.date() == t + timedelta(days=1):
        return "завтра"
    return f"{DOW_SHORT[dt.weekday()]} {dt:%d.%m}"


def order(o: dict, with_phone: bool = True) -> str:
    parts = [f"<b>{e(o.get('time') or '—')}</b> {e(o.get('name'))}"]
    if with_phone and o.get("phone"):
        parts.append(e(o["phone"]))
    lines = [" · ".join(parts)]
    if o.get("addr"):
        lines.append(f"📍 {e(o['addr'])}")
    if o.get("vol"):
        lines.append(f"🛋 {e(o['vol'])}")
    if o.get("sum"):
        lines.append(f"💰 {e(o['sum'])} ₽")
    if o.get("note"):
        lines.append(f"📝 {e(o['note'])}")
    return "\n".join(lines)


def order_card(o: dict) -> str:
    when = f"{human_date(o['date'])} {o.get('time') or ''}".strip() if o.get("date") else "дата не указана"
    return f"🗓 <b>{e(when)}</b>\n{order(o)}"


def task(t: dict) -> str:
    mark = "✅" if t.get("done") else "▫️"
    time_ = f"{t['start']}–{t['end']} " if t.get("start") else ""
    note = f" <i>({e(t['note'])})</i>" if t.get("note") else ""
    return f"{mark} {e(time_)}{e(t.get('title'))}{note}"
