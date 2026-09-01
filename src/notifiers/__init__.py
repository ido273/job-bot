import os

from ..config import Config
from .base import Button, NotificationChannel
from .telegram import TelegramChannel
from .whatsapp import WhatsAppChannel

__all__ = ["Button", "NotificationChannel", "build_channel", "build_channels"]


def _build_telegram(config: Config) -> TelegramChannel:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        raise ValueError(
            "notifications.channels includes 'telegram' but TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_CHAT_ID are not set (env vars or .env file)."
        )
    return TelegramChannel(
        bot_token=bot_token,
        chat_id=chat_id,
        parse_mode=config.telegram_config.get("parse_mode", "HTML"),
    )


def _build_whatsapp(config: Config) -> WhatsAppChannel:
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
    recipient_number = os.environ.get("WHATSAPP_RECIPIENT_NUMBER", "")
    missing = [
        name
        for name, value in [
            ("WHATSAPP_ACCESS_TOKEN", access_token),
            ("WHATSAPP_PHONE_NUMBER_ID", phone_number_id),
            ("WHATSAPP_RECIPIENT_NUMBER", recipient_number),
        ]
        if not value
    ]
    if missing:
        raise ValueError(
            f"notifications.channels includes 'whatsapp' but {', '.join(missing)} "
            "not set (env vars or .env file)."
        )
    return WhatsAppChannel(
        access_token=access_token,
        phone_number_id=phone_number_id,
        recipient_number=recipient_number,
        api_version=config.whatsapp_config.get("api_version", "v21.0"),
    )


_BUILDERS = {
    "telegram": _build_telegram,
    "whatsapp": _build_whatsapp,
}


def build_channel(name: str, config: Config) -> NotificationChannel:
    builder = _BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"Unknown notification channel: {name!r}")
    return builder(config)


def build_channels(config: Config) -> list[NotificationChannel]:
    return [build_channel(name, config) for name in config.notification_channels]
