"""Скриншоты переписки в текст: Яндекс Vision OCR. DeepSeek картинки не читает, поэтому сначала распознаём текст.
Пузыри слева помечаем как сообщения клиента, справа как ваши, чтобы ИИ понял, кто что написал."""
import base64

import httpx

from .config import cfg

URLS = ("https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText",
        "https://ai.api.cloud.yandex.net/ocr/v1/recognizeText")


def _box(block: dict) -> tuple[float, float, float]:
    v = block["boundingBox"]["vertices"]
    xs, ys = [float(p.get("x", 0)) for p in v], [float(p.get("y", 0)) for p in v]
    return min(xs), max(xs), min(ys)


def chat_text(annotation: dict) -> str:
    """Текст скриншота по блокам сверху вниз; слева «Клиент», справа «Я»."""
    width = float(annotation.get("width") or 0)
    rows = []
    for b in annotation.get("blocks") or []:
        text = " ".join(line.get("text", "") for line in b.get("lines") or []).strip()
        if not text:
            continue
        left, right, top = _box(b)
        who = ""
        if width:
            if right > width * 0.88:      # ваши сообщения прижаты к правому краю
                who = "Я: "
            elif left < width * 0.1:      # сообщения клиента начинаются у левого края
                who = "Клиент: "
        rows.append((top, who + text))
    return "\n".join(t for _, t in sorted(rows)) or (annotation.get("fullText") or "").strip()


async def recognize(image: bytes, mime: str = "JPEG") -> str:
    body = {"mimeType": mime, "languageCodes": ["ru", "en"], "model": "page",
            "content": base64.b64encode(image).decode()}
    headers = {"Authorization": f"Api-Key {cfg.yandex_api_key}", "x-folder-id": cfg.yandex_folder_id}
    async with httpx.AsyncClient(timeout=60) as c:
        for url in URLS:
            r = await c.post(url, json=body, headers=headers)
            if r.status_code != 404:
                break
        r.raise_for_status()
        return chat_text(r.json().get("result", r.json()).get("textAnnotation") or {})
