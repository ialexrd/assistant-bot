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


NUM_WORDS = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
             "восемь": 8, "девять": 9, "десять": 10, "четырнадцать": 14, "пару": 2}
MAX_DAYS = 31


def _date(base, dd: str, mm: str):
    d = base.replace(day=int(dd), month=int(mm))
    return d if d >= base - timedelta(days=60) else d.replace(year=d.year + 1)


def _count(t: str, unit: str) -> int | None:
    """«на 3 дня», «на две недели»: число перед словом unit."""
    m = re.search(rf"(\d+|{'|'.join(NUM_WORDS)})\s+{unit}", t)
    if not m:
        return None
    return int(m.group(1)) if m.group(1).isdigit() else NUM_WORDS[m.group(1)]


def parse_days(text: str, default_days: int = 1) -> list[str] | None:
    """Команда расписания, можно голосом: «расписание», «скрины на завтра», «отправь скрины мастеру на неделю»,
    «на 3 дня», «на две недели», «до 15.11», «с 03.11 по 10.11», «расписание 05.11».
    Если срок не назван — default_days дней начиная с сегодня. None — это не команда."""
    t = text.lower().replace("ё", "е")
    if "[скриншот]" in t or "[фото" in t or not re.search(r"расписан|скрин", t):
        return None  # скриншот переписки с подписью — это поручение, а не просьба о расписании
    base = today().date()
    dates = [_date(base, dd, mm) for dd, mm in re.findall(r"(\d{1,2})[./](\d{1,2})", t)]
    if len(dates) >= 2:
        start, end = min(dates[:2]), max(dates[:2])
    elif dates and re.search(r"\bдо\s+\d", t):
        start, end = base, dates[0]
    elif dates:
        start = end = dates[0]
    elif (n := _count(t, "дн")) or (n := _count(t, "дня")):
        start, end = base, base + timedelta(days=n - 1)
    elif n := _count(t, "недел"):
        start, end = base, base + timedelta(days=7 * n - 1)
    elif "месяц" in t:
        start, end = base, base + timedelta(days=29)
    elif "недел" in t:
        start, end = base, base + timedelta(days=6)
    elif "послезавтра" in t:
        start = end = base + timedelta(days=2)
    elif "завтра" in t:
        start = end = base + timedelta(days=1)
    elif "сегодня" in t:
        start = end = base
    else:
        start, end = base, base + timedelta(days=default_days - 1)
    n = min((end - start).days + 1, MAX_DAYS)
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(max(n, 1))]
