"""Расписание картинкой: карточки заказов на день, как в мобильной версии CRM. Рисуем Pillow,
браузер на сервере не нужен. Шрифт DejaVu (sudo apt install fonts-dejavu-core)."""
import io
import re
from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageFont

from .fmt import DOW_SHORT, today

W, PAD = 1080, 48
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
BG, CARD, LINE = "#f5f5f3", "#ffffff", "#e6e6e3"
TEXT, MUTED, ACCENT, GREEN = "#1a1a1a", "#7a7a7a", "#c0392b", "#1e8449"


def _font(size: int, bold: bool = False):
    try:
        return ImageFont.truetype(FONT_DIR + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size)
    except OSError:
        return ImageFont.load_default(size)


F_TITLE, F_SUB = _font(46, True), _font(30)
F_ADDR, F_TIME, F_TEXT, F_SMALL = _font(36, True), _font(34, True), _font(31), _font(27)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if draw.textlength(test, font=font) <= width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    return lines + [cur] if cur else lines


def _card_rows(o: dict) -> list[tuple[str, object, str]]:
    """Строки карточки: (текст, шрифт, цвет)."""
    rows = [(o.get("time") or "время не указано", F_TIME, ACCENT if not o.get("time") else TEXT),
            (o.get("addr") or "адрес не указан", F_ADDR, TEXT)]
    who = " · ".join(x for x in (o.get("name"), o.get("phone")) if x)
    if who:
        rows.append((who, F_TEXT, TEXT))
    if o.get("vol"):
        rows.append((o["vol"], F_TEXT, MUTED))
    meta = " · ".join(x for x in (f"{int(float(o['sum'])):,} ₽".replace(",", " ") if _num(o.get("sum")) else "",
                                   f"мастер {o['spec']}" if o.get("spec") else "", o.get("src") or "") if x)
    if meta:
        rows.append((meta, F_SMALL, GREEN))
    return rows


def _num(v) -> bool:
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def day_title(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    t = today().date()
    rel = {t: "Сегодня", t + timedelta(days=1): "Завтра"}.get(d, "")
    return f"{rel + ', ' if rel else ''}{DOW_SHORT[d.weekday()].lower()} {d:%d.%m}"


def render_day(day: str, orders: list[dict]) -> bytes:
    """PNG с заказами на день."""
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    inner = W - PAD * 2 - 56
    cards = []
    for o in orders:
        lines = [(ln, f, c) for text, f, c in _card_rows(o) for ln in _wrap(tmp, text, f, inner)]
        cards.append((lines, sum(f.size + 16 for _, f, _ in lines) + 48))
    total = sum(o_sum for o_sum in (float(o["sum"]) for o in orders if _num(o.get("sum"))))
    h = PAD + 60 + 50 + 40 + sum(ch + 24 for _, ch in cards) + (120 if not orders else 0) + PAD
    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)
    d.text((PAD, PAD), f"Расписание · {day_title(day)}", font=F_TITLE, fill=TEXT)
    sub = f"Заказов: {len(orders)}" + (f" · сумма {int(total):,} ₽".replace(",", " ") if total else "")
    d.text((PAD, PAD + 70), sub, font=F_SUB, fill=MUTED)
    y = PAD + 150
    if not orders:
        d.text((PAD, y + 20), "Заказов нет", font=F_TEXT, fill=MUTED)
    for lines, ch in cards:
        d.rounded_rectangle((PAD, y, W - PAD, y + ch), radius=22, fill=CARD, outline=LINE, width=2)
        ty = y + 24
        for text, f, c in lines:
            d.text((PAD + 28, ty), text, font=f, fill=c)
            ty += f.size + 16
        y += ch + 24
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def parse_days(text: str) -> list[str] | None:
    """Команда расписания: «расписание», «скрины на завтра», «отправь скрины мастеру на неделю», «расписание 05.11».
    Можно голосом. None — это не команда."""
    t = text.lower().replace("ё", "е")
    if "[скриншот]" in t or "[фото" in t or not re.search(r"расписан|скрин", t):
        return None  # скриншот переписки с подписью — это поручение, а не просьба о расписании
    rest = t
    base = today().date()
    if not rest or "сегодня" in rest:
        days = [base]
    elif "послезавтра" in rest:
        days = [base + timedelta(days=2)]
    elif "завтра" in rest:
        days = [base + timedelta(days=1)]
    elif "недел" in rest:
        days = [base + timedelta(days=i) for i in range(7)]
    elif m := re.search(r"(\d{1,2})[./](\d{1,2})", rest):
        day = base.replace(day=int(m.group(1)), month=int(m.group(2)))
        days = [day if day >= base - timedelta(days=60) else day.replace(year=day.year + 1)]
    else:
        days = [base]
    return [d.strftime("%Y-%m-%d") for d in days]
