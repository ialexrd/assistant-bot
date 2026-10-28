"""ВКонтакте: сообщения сообщества через Bots Long Poll API.
message_new — пишет клиент, message_reply — отвечает владелец от имени сообщества."""
import asyncio
import logging

import httpx

from . import core, stt
from .config import cfg

log = logging.getLogger("vk")
API = "https://api.vk.com/method/"
V = "5.199"

_names: dict[int, tuple[str, str]] = {}


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
            parts.append(f"[голосовое] {text}" if text else "[голосовое сообщение]")
        else:
            parts.append({"photo": "[фото]", "video": "[видео]", "doc": "[файл]"}.get(kind, f"[{kind}]"))
    return " ".join(p for p in parts if p).strip()


async def handle(c: httpx.AsyncClient, update: dict) -> None:
    kind = update.get("type")
    if kind not in ("message_new", "message_reply"):
        return
    obj = update["object"]
    m = obj["message"] if kind == "message_new" else obj
    peer = m.get("peer_id", 0)
    if peer <= 0 or peer >= 2_000_000_000:  # только личные диалоги с людьми, без бесед
        return
    name, screen = await user_name(c, peer)
    outgoing = kind == "message_reply" or m.get("out") == 1
    text = await message_text(c, m, cfg.transcribe_client_voice or outgoing)
    if not text:
        return
    if outgoing:
        await core.owner_message("vk", str(peer), text, name, screen)
    else:
        await core.client_message("vk", str(peer), text, name, screen)


async def run() -> None:
    async with httpx.AsyncClient(timeout=40) as c:
        server = None
        while True:
            try:
                if server is None:
                    server = await call(c, "groups.getLongPollServer", group_id=cfg.vk_group_id)
                    log.info("VK long poll connected")
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
                for upd in data.get("updates", []):
                    try:
                        await handle(c, upd)
                    except Exception:
                        log.exception("VK update failed: %s", upd.get("type"))
            except Exception:
                log.exception("VK long poll error, reconnecting")
                server = None
                await asyncio.sleep(5)
