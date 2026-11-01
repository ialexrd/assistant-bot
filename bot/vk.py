"""ВКонтакте: сообщения сообщества через Bots Long Poll API.
message_new — пишет клиент, message_reply — отвечает владелец от имени сообщества."""
import asyncio
import logging
import random
import re
from collections import defaultdict, deque

import httpx

from . import agent, core, mail, storage, stt
from .config import cfg

log = logging.getLogger("vk")
API = "https://api.vk.com/method/"
V = "5.199"

_names: dict[int, tuple[str, str]] = {}
_recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=3))  # последние поручения владельца
_last_changes: dict[int, list] = {}  # для «отмена»: изменения последнего поручения
UNDO_WORDS = {"отмена", "отмени", "отменить"}


async def call(c: httpx.AsyncClient, method: str, **params) -> dict:
    r = await c.post(API + method, data={**params, "access_token": cfg.vk_token, "v": V})
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"VK {method}: {data['error'].get('error_msg')}")
    return data["response"]


async def user_name(c: httpx.AsyncClient, user_id: int) -> tuple[str, str]:
    if user_id not in _names:
        try:
            u = (await call(c, "users.get", user_ids=user_id, fields="screen_name"))[0]
            _names[user_id] = (f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(),
                               u.get("screen_name", ""))
        except Exception:
            log.exception("users.get failed")
            return "", ""
    return _names[user_id]


async def message_text(c: httpx.AsyncClient, m: dict, transcribe: bool) -> str:
    parts = [m.get("text") or ""]
    for a in m.get("attachments") or []:
        kind = a.get("type")
        if kind == "audio_message":
            am = a["audio_message"]
            text = am.get("transcript") or ""
            if not text and transcribe and am.get("link_ogg"):
                try:
                    audio = (await c.get(am["link_ogg"])).content
                    text = await stt.transcribe(audio, am.get("duration", 0))
                except Exception:
                    log.exception("vk stt failed")
            log.info("VK голосовое %s с: %s", am.get("duration", 0), text or "не распознано")
            parts.append(f"[голосовое] {text}" if text else "[голосовое сообщение]")
        else:
            parts.append({"photo": "[фото]", "video": "[видео]", "doc": "[файл]"}.get(kind, f"[{kind}]"))
    for f in m.get("fwd_messages") or []:  # пересланные сообщения: владелец может переслать переписку
        fwd = await message_text(c, f, transcribe)
        if fwd:
            parts.append(f"[переслано] {fwd}")
    return " ".join(p for p in parts if p).strip()


async def send(c: httpx.AsyncClient, peer: int, text: str) -> None:
    for i in range(0, len(text), 4000):
        await call(c, "messages.send", peer_id=peer, message=text[i:i + 4000],
                   random_id=random.randint(1, 2**31 - 1))


async def notify_owners(text: str, buttons: list[tuple[str, str]]) -> None:
    """Уведомления бота владельцу в ВК (когда Telegram выключен). Кнопок в ВК нет, отвечать можно словами."""
    text = re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    if buttons:
        text += "\n\nПоправить или отменить можно словами, например: «исправь время на 15:00» или «Анна отказалась»."
    async with httpx.AsyncClient(timeout=30) as c:
        for uid in cfg.vk_owner_ids:
            try:
                await send(c, uid, text)
            except Exception:
                log.exception("VK notify %s failed", uid)


async def owner_command(c: httpx.AsyncClient, peer: int, text: str) -> None:
    """Владелец пишет сообществу со своей страницы: это поручение ассистенту."""
    if text.strip().lower() in UNDO_WORDS:
        changes = _last_changes.pop(peer, None)
        if changes:
            await agent.undo(changes)
            await send(c, peer, "↩️ Отменил последнее изменение.")
        else:
            await send(c, peer, "Нечего отменять.")
        return
    try:
        answer, run = await agent.run(text, list(_recent[peer]))
    except Exception:
        log.exception("agent failed")
        await send(c, peer, "ИИ не ответил, попробуйте ещё раз.")
        return
    _recent[peer].append((text, answer))
    for o in run.new_orders:
        if o.get("status") == "booked":
            asyncio.create_task(storage.sync_to_sheets(o))
        asyncio.create_task(mail.order("Записан" if o.get("status") == "booked" else "Новая заявка", o))
    if run.changes:
        _last_changes[peer] = run.changes
        answer += "\n\nНапишите «отмена», чтобы откатить."
    await send(c, peer, answer)


async def handle(c: httpx.AsyncClient, update: dict) -> None:
    kind = update.get("type")
    if kind not in ("message_new", "message_reply"):
        return
    obj = update["object"]
    m = obj["message"] if kind == "message_new" else obj
    peer = m.get("peer_id", 0)
    if peer <= 0 or peer >= 2_000_000_000:  # только личные диалоги с людьми, без бесед
        return
    outgoing = kind == "message_reply" or m.get("out") == 1
    if (m.get("text") or "").strip().lower() == "/id" and not outgoing:
        await send(c, peer, f"Ваш id ВКонтакте: {peer}")
        return
    if peer in cfg.vk_owner_ids:
        if not outgoing:  # исходящие в этом диалоге — ответы самого бота
            text = await message_text(c, m, True)
            if text:
                log.info("VK поручение: %s", text)
                await owner_command(c, peer, text)
        return
    name, screen = await user_name(c, peer)
    text = await message_text(c, m, cfg.transcribe_client_voice or outgoing)
    if not text:
        return
    if outgoing:
        await core.owner_message("vk", str(peer), text, name, screen)
    else:
        await core.client_message("vk", str(peer), text, name, screen)


async def group_id(c: httpx.AsyncClient) -> str:
    """Число из VK_GROUP_ID (club123, -123 и ссылка тоже подойдут), а если его нет — сообщество самого ключа."""
    m = re.fullmatch(r"(?:club|public|-)?(\d+)", cfg.vk_group_id.strip().rsplit("/", 1)[-1])
    if m:
        return m.group(1)
    res = await call(c, "groups.getById")
    groups = res.get("groups", res) if isinstance(res, dict) else res
    return str(groups[0]["id"])


async def run() -> None:
    async with httpx.AsyncClient(timeout=40) as c:
        server = None
        gid = ""
        delay = 5
        while True:
            try:
                if server is None:
                    gid = gid or await group_id(c)
                    server = await call(c, "groups.getLongPollServer", group_id=gid)
                    log.info("VK long poll connected, сообщество %s", gid)
                r = await c.get(server["server"], params={"act": "a_check", "key": server["key"],
                                                          "ts": server["ts"], "wait": 25})
                data = r.json()
                if "failed" in data:
                    if data["failed"] == 1:
                        server["ts"] = data["ts"]
                    else:
                        server = None
                    continue
                server["ts"] = data["ts"]
                delay = 5
                for upd in data.get("updates", []):
                    try:
                        await handle(c, upd)
                    except Exception:
                        log.exception("VK update failed: %s", upd.get("type"))
            except Exception:
                log.exception("VK long poll error, reconnecting in %s s", delay)
                server = None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
