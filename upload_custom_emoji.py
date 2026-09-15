# -*- coding: utf-8 -*-
"""
Загрузка пака кастомных эмодзи магазина в Telegram.

Пак уже СГЕНЕРИРОВАН и лежит в stickers/emoji/ (100x100 PNG, дизайн
совпадает с иконками Mini App). Скрипт создаёт set типа custom_emoji,
принадлежащий боту — только такие эмодзи рендерятся в Mini App через
<tg-emoji> (см. webapp/app.js → USE_CUSTOM_EMOJI).

ИСПОЛЬЗОВАНИЕ:
    1. В .env добавьте:  OWNER_USER_ID=<ваш Telegram user ID>
       (узнать: @userinfobot). BOT_TOKEN уже должен быть.
    2. python upload_custom_emoji.py
    3. Скрипт напечатает готовые ID-карты: впишите их в
       webapp/app.js (CUSTOM_EMOJI_IDS) и emojis.py (set_emoji_id),
       затем поставьте USE_CUSTOM_EMOJI = true в webapp/app.js.

ВАЖНО ПРО ИМЕНА:
    Имя сета ОБЯЗАНО заканчиваться на  _by_<bot_username>
    (требование Telegram) — формируется автоматически из BOT_USERNAME.

ТРЕБОВАНИЯ К ФАЙЛАМ:
    PNG/WEBP ровно 100x100 px (static custom emoji), до 100 KB.
    Наш генератор: scripts/gen_emoji.py — соответствует.
"""

import asyncio
import logging
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from aiogram import Bot
from aiogram.enums import StickerFormat
from aiogram.types import InputSticker
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "Indiasubbot").lstrip("@")
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))
PROXY_URL = os.getenv("PROXY_URL", "")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Состав пака (файлы уже сгенерированы scripts/gen_emoji.py) ──
# имя → (файл, запасной Unicode-эмодзи)
STICKERS_TO_UPLOAD = [
    ("spotify",     "stickers/emoji/spotify.png",     "🎵"),
    ("youtube",     "stickers/emoji/youtube.png",     "▶️"),
    ("apple_music", "stickers/emoji/apple_music.png", "🎧"),
    ("chatgpt",     "stickers/emoji/chatgpt.png",     "🤖"),
    ("netflix",     "stickers/emoji/netflix.png",     "🎬"),
    ("ton",         "stickers/emoji/ton.png",         "💎"),
    ("usdt",        "stickers/emoji/usdt.png",        "💵"),
    ("card",        "stickers/emoji/card.png",        "💳"),
    ("stars",       "stickers/emoji/stars.png",       "⭐"),
    ("check",       "stickers/emoji/check.png",       "✅"),
    ("sparkle",     "stickers/emoji/sparkle.png",     "✨"),
    ("fire",        "stickers/emoji/fire.png",        "🔥"),
    ("gift",        "stickers/emoji/gift.png",        "🎁"),
]

# ── Карты имён для сгенерированных сниппетов ──
# app.js CUSTOM_EMOJI_IDS (webapp/app.js)
JS_KEYS = {
    "spotify": "spotify", "youtube": "youtube", "apple_music": "applemusic",
    "chatgpt": "openai", "netflix": "netflix", "ton": "ton",
    "usdt": "tether", "card": "card", "stars": "star", "check": "check",
    "sparkle": "sparkle", "fire": "fire", "gift": "gift",
}
# emojis.py (set_emoji_id)
PY_KEYS = {
    "spotify": "spotify", "youtube": "youtube", "apple_music": "apple",
    "chatgpt": "chatgpt", "netflix": "netflix", "ton": "diamond",
    "usdt": "dollar", "card": "credit_card", "stars": "star",
    "check": "check", "sparkle": "sparkle", "fire": "fire",
}


def validate_png(path: str) -> None:
    """Файл существует, 100x100, до 100 KB — требования custom emoji."""
    if not os.path.exists(path):
        raise RuntimeError(f"файл не найден: {path}")
    size = os.path.getsize(path)
    if size > 100 * 1024:
        raise RuntimeError(f"{path}: {size / 1024:.0f}KB > 100KB")
    with open(path, "rb") as f:
        header = f.read(33)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"{path}: не PNG")
    w = int.from_bytes(header[16:20], "big")
    h = int.from_bytes(header[20:24], "big")
    if (w, h) != (100, 100):
        raise RuntimeError(f"{path}: {w}x{h}, а нужно ровно 100x100")


def print_snippets(name_to_id: dict[str, str]) -> None:
    """Готовые ID-карты для app.js и emojis.py."""
    js_lines, py_lines = [], []
    for name, eid in name_to_id.items():
        if not eid:
            continue
        if name in JS_KEYS:
            js_lines.append(f'    {JS_KEYS[name]}: "{eid}",')
        if name in PY_KEYS:
            py_lines.append(f'    "{PY_KEYS[name]}": "{eid}",')

    print("\n" + "=" * 64)
    print("1) webapp/app.js — замените карту CUSTOM_EMOJI_IDS и")
    print("   поставьте USE_CUSTOM_EMOJI = true:")
    print("=" * 64)
    print("  var USE_CUSTOM_EMOJI = true;")
    print("  var CUSTOM_EMOJI_IDS = {")
    for line in js_lines:
        print(line)
    print("  };")

    print("\n" + "=" * 64)
    print("2) emojis.py — обновите ID (после словарей выполните):")
    print("=" * 64)
    print("  bulk_set_emoji_ids({")
    for line in py_lines:
        print(line)
    print("  })")
    print("=" * 64)
    print("\nЗатем перезапустите бота. Готово: эмодзи будут видны")
    print("в Mini App (у всех пользователей) и в сообщениях бота.")


async def upload_sticker_pack() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан в .env!")
        return
    if not OWNER_USER_ID:
        logger.error("OWNER_USER_ID не задан в .env! (ваш Telegram user ID, @userinfobot)")
        return

    # Требование Telegram: имя сета custom_emoji заканчивается на _by_<username>
    pack_name = f"substore_by_{BOT_USERNAME}"
    title = "SUBSTORE Shop"

    for _, path, _ in STICKERS_TO_UPLOAD:
        validate_png(path)
    logger.info(f"Все {len(STICKERS_TO_UPLOAD)} файлов прошли проверку (100x100, PNG)")

    from aiogram.client.session.aiohttp import AiohttpSession
    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
    bot = Bot(token=BOT_TOKEN, session=session)

    try:
        # Сет уже существует? — просто собираем ID
        try:
            existing = await bot.get_sticker_set(pack_name)
            logger.info(f"Сет '{pack_name}' уже существует ({len(existing.stickers)} шт.)")
            name_to_id = {}
            for i, sticker in enumerate(existing.stickers):
                name = STICKERS_TO_UPLOAD[i][0] if i < len(STICKERS_TO_UPLOAD) else f"emoji_{i}"
                name_to_id[name] = sticker.custom_emoji_id or ""
                logger.info(f"  {name}: {sticker.custom_emoji_id}")
            print_snippets(name_to_id)
            return
        except Exception:
            pass  # сета нет — создаём

        input_stickers = [
            InputSticker(
                sticker=open(path, "rb").read(),
                emoji_list=[fallback],
                format=StickerFormat.STATIC,
            )
            for _, path, fallback in STICKERS_TO_UPLOAD
        ]

        logger.info(f"Создаём custom_emoji сет '{pack_name}' ({len(input_stickers)} шт.)…")
        await bot.create_new_sticker_set(
            user_id=OWNER_USER_ID,
            name=pack_name,
            title=title,
            stickers=input_stickers,
            sticker_type="custom_emoji",  # StickerType.CUSTOM_EMOJI
        )
        logger.info("Сет создан! Ждём обработки Telegram…")
        await asyncio.sleep(2)

        pack = await bot.get_sticker_set(pack_name)
        name_to_id = {}
        for i, sticker in enumerate(pack.stickers):
            name = STICKERS_TO_UPLOAD[i][0] if i < len(STICKERS_TO_UPLOAD) else f"emoji_{i}"
            name_to_id[name] = sticker.custom_emoji_id or ""
            logger.info(f"  {name}: {sticker.custom_emoji_id}")
        print_snippets(name_to_id)

    finally:
        if session:
            await bot.session.close()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print(__doc__)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        for _, path, fallback in STICKERS_TO_UPLOAD:
            try:
                validate_png(path)
                print(f"  OK  {path}  ({os.path.getsize(path) / 1024:.1f}KB)  fb={fallback}")
            except RuntimeError as e:
                print(f"  FAIL {e}")
        return
    asyncio.run(upload_sticker_pack())


if __name__ == "__main__":
    main()
