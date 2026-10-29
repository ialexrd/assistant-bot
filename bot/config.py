import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class Config:
    # Telegram
    bot_token: str = os.getenv("BOT_TOKEN", "")
    owner_ids: frozenset = _ids(os.getenv("OWNER_IDS", ""))
    master_chat_id: str = os.getenv("MASTER_CHAT_ID", "")
    tz: ZoneInfo = ZoneInfo(os.getenv("TZ", "Europe/Kaliningrad"))
    morning_time: str = os.getenv("MORNING_TIME", "09:00")
    master_summary_time: str = os.getenv("MASTER_SUMMARY_TIME", "20:00")

    # ВКонтакте: бот сообщества через Bots Long Poll
    vk_token: str = os.getenv("VK_GROUP_TOKEN", "")
    vk_group_id: str = os.getenv("VK_GROUP_ID", "")

    # ИИ: deepseek | anthropic
    llm_provider: str = os.getenv("LLM_PROVIDER", "deepseek")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    # Claude (если LLM_PROVIDER=anthropic)
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    effort: str = os.getenv("ANTHROPIC_EFFORT", "medium")

    # Распознавание голоса: yandex | openai | off
    stt_provider: str = os.getenv("STT_PROVIDER", "yandex")
    yandex_api_key: str = os.getenv("YANDEX_API_KEY", "")
    yandex_folder_id: str = os.getenv("YANDEX_FOLDER_ID", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    transcribe_client_voice: bool = os.getenv("TRANSCRIBE_CLIENT_VOICE", "1") == "1"

    sheets_url: str = os.getenv("SHEETS_URL", "")
    data_dir: Path = Path(os.getenv("DATA_DIR", "data"))


cfg = Config()
cfg.data_dir.mkdir(parents=True, exist_ok=True)
