"""Настройки, которые меняются из чата с ботом: фразы-триггеры записи клиента."""
import json
import re

from .config import cfg

PATH = cfg.data_dir / "settings.json"

DEFAULTS = {
    # Фраза считается сработавшей, если все её слова есть в сообщении (регистр, ё/е и знаки не важны).
    "trigger_phrases": [
        "записал",
        "записала",
        "записали",
        "записываю",
        "вы записаны",
    ],
    # Создавать заявку «новая», когда незнакомый клиент пишет впервые.
    "auto_leads": True,
}


def load() -> dict:
    try:
        return DEFAULTS | json.loads(PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)


def save(data: dict) -> None:
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _words(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    return re.findall(r"[a-zа-я0-9]+", text)


def matched_phrase(text: str) -> str | None:
    """Возвращает фразу-триггер, которая нашлась в сообщении, или None."""
    words = _words(text)
    for phrase in load()["trigger_phrases"]:
        need = _words(phrase)
        if need and all(w in words for w in need):
            return phrase
    return None


def add_phrase(phrase: str) -> None:
    data = load()
    phrase = phrase.strip().lower()
    if phrase and phrase not in data["trigger_phrases"]:
        data["trigger_phrases"].append(phrase)
        save(data)


def remove_phrase(phrase: str) -> bool:
    data = load()
    phrase = phrase.strip().lower()
    if phrase not in data["trigger_phrases"]:
        return False
    data["trigger_phrases"].remove(phrase)
    save(data)
    return True
