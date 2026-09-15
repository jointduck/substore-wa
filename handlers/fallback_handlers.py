"""
Вежливый ответ на нажатия устаревших инлайн-кнопок.

Роутер подключён в main.py ПОСЛЕДНИМ (после admin_router →
support_router → user_router), поэтому сюда попадают ТОЛЬКО callback'и,
которые не совпали ни с одним хендлером выше:
  - кнопка от экрана прошлой версии бота (после обновления callback-схемы);
  - кнопка от сообщения, отправленного до рестарта, если обработчик
    сменился/удалился;
  - любые прочие «осиротевшие» callback'и.

Раньше такие нажатия молча висели: у кнопки крутились «часики», юзер
думал, что бот завис. Теперь бот сразу отвечает попапом.

ВАЖНО: кнопки рабочих флоу (каталог, оплаты, тикеты и т.д.) совпадают
с хендлерами выше и сюда НЕ попадают — даже если заказ уже оплачен или
отменён, юзер получит осмысленный ответ от настоящего обработчика
(«Оплата уже подтверждена!», «Заказ отменён...»). Fallback — только
для действительно неизвестных кнопок.
"""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from utils.safe_callback import safe_answer

logger = logging.getLogger(__name__)

router = Router(name="fallback")


@router.callback_query(F.data == "noop")
async def placeholder_button(callback: CallbackQuery):
    """Кнопка-заглушка («Нет заказов» и т.п.) — действия нет по дизайну.

    Молча снимаем «часики»: попап «экран устарел» здесь лгал бы —
    экран на самом деле актуален, просто нажимать не на что.
    """
    await safe_answer(callback)


@router.callback_query(F.data == "back_menu")
async def back_to_admin_menu(callback: CallbackQuery):
    """«Назад» с экранов админ-редактирования (edit_catalog_kb и др.).

    Возвращаем админ-меню (reply-клавиатуру нельзя прикрепить к
    edit_text — отправляем новое сообщение; админский чат дедуп
    не трогает).
    """
    from emojis import ce
    from keyboards.keyboards import admin_menu_kb

    try:
        await callback.message.answer(
            f"{ce('gear')} <b>Админ-панель</b>\n\n"
            f"Выберите раздел на клавиатуре ниже.",
            reply_markup=admin_menu_kb(),
            parse_mode="HTML",
        )
    except Exception:
        pass  # заблокирован/удалён чат — просто снимем «часики» ниже
    await safe_answer(callback)


@router.callback_query()
async def stale_button_pressed(callback: CallbackQuery):
    """Ни один хендлер не совпал — кнопка устарела. Отвечаем вежливо."""
    data = callback.data or ""
    logger.info(f"Stale callback ignored by handlers: {data[:64]}")
    await safe_answer(
        callback,
        "Этот экран устарел — откройте меню заново.",
    )
