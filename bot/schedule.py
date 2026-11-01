"""Расписание картинкой: карточки заказов на день, как в мобильной версии CRM. Рисуем Pillow,
браузер на сервере не нужен. Шрифт DejaVu (sudo apt install fonts-dejavu-core)."""
import io
import re
from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageFont

from .fmt import DOW_SHORT, today

W, PAD = 1170, 48
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
BG, CARD, LINE = "#f4f4f2", "#ffffff", "#ececec"
TEXT, MUTED, SRC, ORANGE = "#1f1f1f", "#a8a8a8", "#9a9a9a", "#f2a422"
DAYS_ACC = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
PER_IMAGE = 8  # карточек на одной картинке, длинный список режем на несколько


def _font(size: int, bold: bool = False):
    try:
        return ImageFont.truetype(FONT_DIR + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size)
    except OSError:
        return ImageFont.load_default(size)


F_ADDR, F_DATE, F_NAME, F_SUM, F_VOL, F_SRC = _font(40), _font(40, True), _font(40), _font(40, True), _font(34), _font(34)
F_HEAD = _font(34)


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


def _num(v) -> bool:
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def _money(v) -> str:
    return f"{int(float(v)):,} ₽".replace(",", " ")


def mask_phone(phone: str) -> str:
    """Как в CRM: последние 4 цифры звёздочками."""
    return phone[:-4] + "****" if len(phone) > 4 else phone


def crm_date(day: str, time: str = "") -> str:
    """Как в CRM: «04.11.2026 (среду) 11:15», «… (Завтра)»."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    t = today().date()
    word = {t: "Сегодня", t + timedelta(days=1): "Завтра"}.get(d, DAYS_ACC[d.weekday()])
    return f"{d:%d.%m.%Y} ({word})" + (f" {time}" if time else "")


def day_title(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    t = today().date()
    rel = {t: "Сегодня", t + timedelta(days=1): "Завтра"}.get(d, "")
    return f"{rel + ', ' if rel else ''}{DOW_SHORT[d.weekday()].lower()} {d:%d.%m}"


def _card(draw, o: dict, width: int) -> list[tuple]:
    """Элементы карточки: ("line", текст, шрифт, цвет) или ("name", имя, телефон, сумма)."""
    rows = [("line", ln, F_ADDR, TEXT) for ln in _wrap(draw, o.get("addr") or "адрес не указан", F_ADDR, width)]
    rows.append(("line", crm_date(o["date"], o.get("time") or "") if o.get("date") else "дата не указана",
                 F_DATE, ORANGE))
    rows.append(("name", o.get("name") or "", mask_phone(o.get("phone") or ""),
                 _money(o["sum"]) if _num(o.get("sum")) else ""))
    vol = " ".join(x for x in (o.get("vol"), f"({o['note']})" if o.get("note") else "") if x)
    rows += [("line", ln, F_VOL, MUTED) for ln in _wrap(draw, vol, F_VOL, width)] if vol else []
    if o.get("src"):
        rows.append(("gap",))
        rows.append(("line", o["src"], F_SRC, SRC))
    return rows


def _row_h(r) -> int:
    return 28 if r[0] == "gap" else (F_NAME.size if r[0] == "name" else r[2].size) + 22


def render_list(orders: list[dict], title: str = "") -> list[bytes]:
    """Список карточек, как в мобильной CRM, без кнопок. Длинный список — несколько картинок."""
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    inner = W - PAD * 2 - 80
    images = []
    chunks = [orders[i:i + PER_IMAGE] for i in range(0, len(orders), PER_IMAGE)] or [[]]
    for n, chunk in enumerate(chunks):
        cards = [_card(tmp, o, inner) for o in chunk]
        head = 90 if title else 0
        h = PAD + head + sum(sum(_row_h(r) for r in c) + 70 for c in cards) + (100 if not chunk else 0) + PAD
        img = Image.new("RGB", (W, h), BG)
        d = ImageDraw.Draw(img)
        y = PAD
        if title:
            part = f"  ({n + 1}/{len(chunks)})" if len(chunks) > 1 else ""
            d.text((PAD + 8, y), title + part, font=F_HEAD, fill=SRC)
            y += head
        if not chunk:
            d.text((PAD + 8, y + 20), "Заказов нет", font=F_ADDR, fill=MUTED)
        body_top = y
        if cards:
            d.rounded_rectangle((PAD, body_top, W - PAD, h - PAD), radius=34, fill=CARD, outline=LINE, width=2)
        for k, rows in enumerate(cards):
            y += 36
            for r in rows:
                x = PAD + 48
                if r[0] == "line":
                    d.text((x, y), r[1], font=r[2], fill=r[3])
                elif r[0] == "name":
                    _, name, phone, money = r
                    for txt, f, c in ((name, F_NAME, TEXT), (phone, F_NAME, "#4a4a4a"), (money, F_SUM, TEXT)):
                        if txt:
                            d.text((x, y), txt, font=f, fill=c)
                            x += d.textlength(txt, font=f) + 34
                y += _row_h(r)
            y += 34
            if k < len(cards) - 1:
                d.line((PAD, y, W - PAD, y), fill=LINE, width=2)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        images.append(buf.getvalue())
    return images


def render_day(day: str, orders: list[dict]) -> bytes:
    return render_list(orders, f"Заказы · {day_title(day)}")[0]


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
