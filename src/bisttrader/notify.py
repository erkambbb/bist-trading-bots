"""Telegram bildirimleri (ortam değişkenleri: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def send(text: str, silent: bool = False) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(text)
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "disable_notification": silent}, timeout=10)
        return r.ok
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram hatası: %s", e)
        print(text)
        return False
