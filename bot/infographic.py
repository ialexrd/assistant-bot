"""Карточки для соцсетей и Авито: плашка, крупный заголовок, подзаголовок, фото и подвал с телефоном."""
import io
import os
import re

from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field

from . import llm

W, H, M = 1080, 1350, 56
BG, INK, GRAY, YELLOW = "#f4f3ef", "#151515", "#5a5a5a", "#ffd426"
FONTS = os.path.join(os.path.dirname(__file__), "fonts")
TRIGGER = re.compile(r"инфографик", re.I)

BRAND_NAME = os.getenv("BRAND_NAME", "Химчистка мягкой мебели")
BRAND_LINE = os.getenv("BRAND_LINE", "выезд на дом")
BRAND_PHONE = os.getenv("BRAND_PHONE", "+7 (912) 307-94-00")


def _f(weight: int, size: int):
    return ImageFont.truetype(os.path.join(FONTS, f"Onest-{weight}.ttf"), size)


class Card(BaseModel):
    tag: str = Field(description="Плашка сверху, 1–3 слова, например «Профессиональная сушка»")
    title: str = Field(description="Заголовок 3–7 слов. Главную часть оберни в ==…==, например "
                                   "«Посушим вашу мебель ==после чистки==»")
    subtitle: str = Field(description="Подзаголовок одно предложение до 90 символов, важное в **…**")
    footer: str = Field(description="Строка под названием в подвале, 2–5 слов, например «выезд на дом · сушка после чистки»")


SYSTEM = f"""{llm.BUSINESS}
Владелец присылает фото и тему. Придумай текст для квадратной рекламной карточки в соцсети и на Авито:
коротко, по делу, без восклицательных знаков и штампов, на «вы». Если владелец сам написал заголовок или
текст, используй его слова."""


async def make_text(brief: str) -> Card:
    return await llm.parse(SYSTEM, f"Тема от владельца: {brief}", Card, 1500)


def _tokens(text: str, mark: str) -> list[tuple[str, bool, bool]]:
    """«a ==b c==, d» → (слово, выделено, приклеено к предыдущему без пробела)."""
    text = re.sub(r"(\d) (?=\d{3}\b)|(\d) (?=₽)", lambda m: (m[1] or m[2]) + "\u00a0", text)
    out, prev = [], ""
    for i, part in enumerate(text.split(mark)):
        for j, w in enumerate(x for x in re.split(r"[ \t\n]+", part) if x):
            glue = j == 0 and bool(out) and part[:1] not in " \t\n" and prev[-1:] not in " \t\n"
            out.append((w, i % 2 == 1, glue))
        prev = part
    return out


def _wrap(draw, tokens, fonts, width) -> list[list[tuple[str, bool]]]:
    lines, cur = [], []
    for t in tokens:
        test = cur + [t]
        if cur and not t[2] and _line_w(draw, test, fonts) > width:
            lines.append(cur)
            cur = [t]
        else:
            cur = test
    return lines + ([cur] if cur else [])


def _line_w(draw, line, fonts) -> float:
    space = draw.textlength(" ", font=fonts[False])
    return sum(draw.textlength(w, font=fonts[b]) + (0 if g or k == 0 else space)
               for k, (w, b, g) in enumerate(line))


def render(card: Card, photo: bytes | None) -> bytes:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # плашка
    f_tag = _f(700, 22)
    tag = card.tag.upper()
    tw = d.textlength(tag, font=f_tag) + len(tag) * 1.2
    d.rounded_rectangle((M, 52, M + tw + 40, 96), radius=22, fill=YELLOW)
    x = M + 20
    for ch in tag:  # небольшая разрядка
        d.text((x, 61), ch, font=f_tag, fill=INK)
        x += d.textlength(ch, font=f_tag) + 1.2
    # заголовок: выделенные слова подчёркнуты жёлтым маркером
    size = 76
    f_title = {False: _f(800, size), True: _f(800, size)}
    lines = _wrap(d, _tokens(card.title, "=="), f_title, W - 2 * M)
    while len(lines) > 3 and size > 52:
        size -= 6
        f_title = {False: _f(800, size), True: _f(800, size)}
        lines = _wrap(d, _tokens(card.title, "=="), f_title, W - 2 * M)
    y, lh = 122, int(size * 1.0)
    space = d.textlength(" ", font=f_title[False])
    for line in lines:
        x = M
        for i, (w, hl, glue) in enumerate(line):
            if glue and i:
                x -= space
            ww = d.textlength(w, font=f_title[hl])
            nxt_hl = i + 1 < len(line) and line[i + 1][1]
            if hl:
                d.rectangle((x, y + size * 0.84, x + ww + (space if nxt_hl else 0), y + size * 1.02), fill=YELLOW)
            d.text((x, y), w, font=f_title[hl], fill=INK)
            x += ww + space
        y += lh
    # подзаголовок с жирными словами
    f_sub = {False: _f(400, 31), True: _f(700, 31)}
    y += 22
    for line in _wrap(d, _tokens(card.subtitle, "**"), f_sub, W - 2 * M - 120):
        x = M
        for i, (w, b, glue) in enumerate(line):
            if glue and i:
                x -= d.textlength(" ", font=f_sub[False])
            d.text((x, y), w, font=f_sub[b], fill=INK if b else GRAY)
            x += d.textlength(w + " ", font=f_sub[b])
        y += 40
    # подвал
    fy = H - 128
    d.rectangle((36, fy, 41, fy + 74), fill=YELLOW)
    d.text((64, fy - 2), BRAND_NAME, font=_f(700, 33), fill=INK)
    d.text((64, fy + 46), card.footer or BRAND_LINE, font=_f(400, 22), fill=GRAY)
    f_ph = _f(700, 46)
    pw = d.textlength(BRAND_PHONE, font=f_ph)
    d.text((W - 36 - pw, fy - 6), BRAND_PHONE, font=f_ph, fill=INK)
    d.rectangle((W - 36 - pw, fy + 58, W - 36, fy + 64), fill=YELLOW)
    # фото на всё оставшееся место
    top, bottom = y + 30, fy - 26
    if photo and bottom - top > 200:
        box = (W - 72, bottom - top)
        pic = ImageOps.fit(ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB"), box,
                          Image.LANCZOS)
        mask = Image.new("L", box, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, *box), radius=30, fill=255)
        img.paste(pic, (36, top), mask)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


async def make(brief: str, photo: bytes | None) -> bytes:
    brief = re.sub(r"(сделай|собери|нужна)?\s*инфографик\w*", "", brief, flags=re.I).strip(" :,.-—\n") or "чистка мягкой мебели на дому"
    return render(await make_text(brief), photo)
