"""Голосовые в текст. У DeepSeek и Claude нет распознавания речи, поэтому здесь Яндекс SpeechKit или OpenAI Whisper."""
import asyncio
import tempfile
from pathlib import Path

import httpx

from .config import cfg


async def _yandex_stt_chunk(client: httpx.AsyncClient, audio: bytes) -> str:
    r = await client.post(
        "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
        params={"lang": "ru-RU", "folderId": cfg.yandex_folder_id, "format": "oggopus"},
        headers={"Authorization": f"Api-Key {cfg.yandex_api_key}"},
        content=audio,
    )
    r.raise_for_status()
    return r.json().get("result", "")


async def _split_ogg(audio: bytes, seconds: int = 28) -> list[bytes]:
    """Синхронный SpeechKit принимает до 30 секунд, поэтому длинное голосовое режем ffmpeg."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in.ogg"
        src.write_bytes(audio)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", str(src), "-f", "segment",
            "-segment_time", str(seconds), "-c", "copy", str(Path(d) / "part%03d.ogg"),
        )
        await proc.wait()
        parts = sorted(Path(d).glob("part*.ogg"))
        return [p.read_bytes() for p in parts] or [audio]


async def transcribe(audio: bytes, duration: int) -> str:
    if cfg.stt_provider == "off":
        return ""
    async with httpx.AsyncClient(timeout=60) as client:
        if cfg.stt_provider == "openai":
            r = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
                files={"file": ("voice.ogg", audio, "audio/ogg")},
                data={"model": "whisper-1", "language": "ru"},
            )
            r.raise_for_status()
            return r.json()["text"]
        chunks = await _split_ogg(audio) if duration > 29 else [audio]
        texts = [await _yandex_stt_chunk(client, c) for c in chunks]
        return " ".join(t for t in texts if t)
