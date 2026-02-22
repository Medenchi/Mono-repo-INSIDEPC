"""
Inside PC — Бот + API.
Топик создаётся при загрузке фото оплаты.
После подтверждения — все сообщения пользователя идут менеджеру.
"""

import json
import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, WebAppInfo,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
from database import *

log = logging.getLogger("insidepc")

STYLE_OK = True


def S(name: str) -> dict:
    return {"style": name} if STYLE_OK else {}


# ============================================================
#                         FASTAPI
# ============================================================

app = FastAPI(title="Inside PC API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/web", StaticFiles(directory="web", html=True), name="web")


class OrderIn(BaseModel):
    user_id: int
    username: str = ""
    full_name: str = ""
    service_type: str
    has_parts_list: bool = False
    parts_data: dict | None = None
    description: str = ""


@app.post("/api/order")
async def api_create_order(data: OrderIn):
    if data.service_type not in config.PRICES:
        raise HTTPException(400, "Неизвестная услуга")
    p = config.PRICES[data.service_type]
    await upsert_user(data.user_id, data.username, data.full_name)
    oid = await create_order(
        data.user_id, data.service_type, data.has_parts_list,
        data.parts_data, data.description, p["byn"], p["rub"]
    )
    order = await get_order(oid)
    prefix = p.get("prefix", "")
    return {
        "id": oid, "status": order["status"],
        "price_byn": p["byn"], "price_rub": p["rub"],
        "price_prefix": prefix,
        "payment_card": config.PAYMENT_CARD,
        "payment_holder": config.PAYMENT_HOLDER,
        "payment_bank": config.PAYMENT_BANK,
        "bot_username": config.BOT_USERNAME,
    }


@app.get("/api/status/{order_id}")
async def api_status(order_id: int):
    order = await get_order(order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    return {
        "order_id": order["id"], "status": order["status"],
        "status_text": STATUS_NAMES.get(order["status"], order["status"]),
        "service": config.PRICES.get(order["service_type"], {}).get("name", ""),
    }


@app.get("/api/orders/{user_id}")
async def api_user_orders(user_id: int):
    orders = await get_user_orders(user_id)
    result = []
    for o in orders:
        p = config.PRICES.get(o["service_type"], {})
        prefix = p.get("prefix", "")
        result.append({
            "id": o["id"],
            "service": p.get("name", "?"),
            "status": o["status"],
            "status_text": STATUS_NAMES.get(o["status"], o["status"]),
            "price_byn": o["price_byn"], "price_rub": o["price_rub"],
            "price_prefix": prefix,
            "date": o["created_at"][:16],
        })
    return result


@app.get("/api/order/{order_id}")
async def api_order_detail(order_id: int):
    order = await get_order(order_id)
    if not order:
        raise HTTPException(404, "Не найден")
    user = await get_user(order["user_id"])
    parts = None
    if order["parts_data"]:
        try:
            parts = json.loads(order["parts_data"])
        except:
            pass
    p = config.PRICES.get(order["service_type"], {})
    return {
        "id": order["id"], "user_id": order["user_id"],
        "username": user["username"] if user else "",
        "full_name": user["full_name"] if user else "",
        "service": p.get("name", "?"),
        "status": order["status"],
        "status_text": STATUS_NAMES.get(order["status"], order["status"]),
        "price_byn": order["price_byn"], "price_rub": order["price_rub"],
        "price_prefix": p.get("prefix", ""),
        "has_parts": order["has_parts"], "parts": parts,
        "description": order["description"], "date": order["created_at"][:16],
    }


@app.get("/api/prices")
async def api_prices():
    return config.PRICES


# ============================================================
#                      TELEGRAM BOT
# ============================================================

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)


class States(StatesGroup):
    waiting_photo = State()
    chatting = State()
    waiting_oid = State()


# ============================================================
#  КЛАВИАТУРЫ
# ============================================================

def kb_start():
    url = config.WEBAPP_URL
    buttons = []
    if url:
        buttons.append([InlineKeyboardButton(
            text="Оформить заявку", web_app=WebAppInfo(url=url),
        )])
    buttons.append([InlineKeyboardButton(
        text="Мои заказы", callback_data="my_orders", **S("primary"),
    )])
    buttons.append([InlineKeyboardButton(
        text="Проверить статус", callback_data="check_status",
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_orders(orders):
    rows = []
    for o in orders[:10]:
        s = STATUS_NAMES.get(o["status"], o["status"])
        n = config.PRICES.get(o["service_type"], {}).get("name", "?")
        if o["status"] in ("payment_confirmed", "completed"):
            st = S("success")
        elif o["status"] == "cancelled":
            st = S("danger")
        elif o["status"] == "in_progress":
            st = S("primary")
        else:
            st = {}
        rows.append([InlineKeyboardButton(
            text=f"#{o['id']} | {n} | {s}",
            callback_data=f"view:{o['id']}", **st,
        )])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_pay(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить оплату", callback_data=f"cpay:{oid}", **S("success"))],
        [InlineKeyboardButton(text="Отклонить", callback_data=f"rpay:{oid}", **S("danger"))],
    ])


def kb_admin_manage(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="В работу", callback_data=f"ss:{oid}:in_progress", **S("primary")),
            InlineKeyboardButton(text="Завершить", callback_data=f"ss:{oid}:completed", **S("success")),
        ],
        [InlineKeyboardButton(text="Отменить", callback_data=f"ss:{oid}:cancelled", **S("danger"))],
    ])


def kb_admin_order_info(oid):
    url = config.WEBAPP_URL
    if not url:
        return None
    info_url = url.replace("/web", f"/web/admin.html?order_id={oid}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Детали заказа", web_app=WebAppInfo(url=info_url))],
    ])


def kb_view_order():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад к заказам", callback_data="my_orders")],
    ])


def kb_cancel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="home", **S("danger"))],
    ])


# ============================================================
#  Безопасная отправка
# ============================================================

def _strip(markup):
    if not markup or not hasattr(markup, 'inline_keyboard') or not markup.inline_keyboard:
        return markup
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            d = btn.model_dump(exclude_none=True)
            d.pop("style", None)
            new_row.append(InlineKeyboardButton(**d))
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _retry_no_style(coro_factory):
    """Вызывает корутину, при ошибке style — повторяет без."""
    global STYLE_OK
    try:
        return await coro_factory(True)
    except TelegramBadRequest as e:
        if "invalid button style" in str(e).lower():
            STYLE_OK = False
            log.warning("Style отключён")
            return await coro_factory(False)
        raise


async def safe_answer(msg, text, reply_markup=None, **kw):
    async def do(ok):
        rm = reply_markup if ok else _strip(reply_markup)
        return await msg.answer(text, reply_markup=rm, **kw)
    return await _retry_no_style(do)


async def safe_edit(msg, text, reply_markup=None, **kw):
    async def do(ok):
        rm = reply_markup if ok else _strip(reply_markup)
        return await msg.edit_text(text, reply_markup=rm, **kw)
    return await _retry_no_style(do)


async def safe_bot_msg(chat_id, text, reply_markup=None, **kw):
    async def do(ok):
        rm = reply_markup if ok else _strip(reply_markup)
        return await bot.send_message(chat_id, text, reply_markup=rm, **kw)
    return await _retry_no_style(do)


async def safe_bot_photo(chat_id, photo, caption=None, reply_markup=None, **kw):
    async def do(ok):
        rm = reply_markup if ok else _strip(reply_markup)
        return await bot.send_photo(chat_id, photo, caption=caption, reply_markup=rm, **kw)
    return await _retry_no_style(do)


# ============================================================
#  Создание топика (при загрузке фото оплаты)
# ============================================================

async def create_order_topic(oid, uid, username=""):
    """Создаёт топик в группе менеджеров и отправляет инфо."""
    if not config.MANAGER_GROUP_ID:
        return None

    order = await get_order(oid)
    if not order:
        return None

    sn = config.PRICES[order["service_type"]]["name"]
    uname = f"@{username}" if username else f"ID:{uid}"

    tid = None
    try:
        topic = await bot.create_forum_topic(
            chat_id=config.MANAGER_GROUP_ID, name=f"{uname} | {sn}"
        )
        tid = topic.message_thread_id
        await save_topic(tid, oid, uid)
        log.info(f"Топик: {uname} | {sn}, tid={tid}")
    except Exception as e:
        log.error(f"Топик: {e}")
        return None

    # Инфо о заказе
    parts_text = ""
    if order["has_parts"] and order["parts_data"]:
        try:
            parts = json.loads(order["parts_data"])
            parts_text = "\n<b>Комплектующие:</b>\n" + "".join(
                f"  - {k}: {v}\n" for k, v in parts.items() if v
            )
        except:
            pass
    desc = f"\n<b>Описание:</b> {order['description']}" if order["description"] else ""
    prefix = config.PRICES.get(order["service_type"], {}).get("prefix", "")

    text = (
        f"<b>НОВАЯ ЗАЯВКА #{oid}</b>\n\n"
        f"Клиент: {uname}\nУслуга: {sn}\n"
        f"Стоимость: {prefix}{order['price_byn']} BYN / {prefix}{order['price_rub']} RUB"
        f"{parts_text}{desc}"
    )

    extra = {"message_thread_id": tid}
    try:
        await safe_bot_msg(config.MANAGER_GROUP_ID, text, **extra)
        await safe_bot_msg(config.MANAGER_GROUP_ID, "Управление:", reply_markup=kb_admin_manage(oid), **extra)
        info_kb = kb_admin_order_info(oid)
        if info_kb:
            await safe_bot_msg(config.MANAGER_GROUP_ID, "Подробнее:", reply_markup=info_kb, **extra)
    except Exception as e:
        log.error(f"В группу: {e}")

    return tid


# ============================================================
#                    ХЭНДЛЕРЫ
# ============================================================

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext, command: CommandObject):
    await upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")

    # Диплинк: /start pay_3
    args = command.args
    if args and args.startswith("pay_"):
        try:
            oid = int(args.replace("pay_", ""))
            order = await get_order(oid)
            if order and order["user_id"] == msg.from_user.id and order["status"] == "pending_payment":
                await state.set_state(States.waiting_photo)
                await state.update_data(order_id=oid)
                await safe_answer(
                    msg,
                    f"<b>Inside PC — Заказ #{oid}</b>\n\n"
                    f"Отправьте скриншот / фото чека оплаты прямо сюда.\n\n"
                    f"<b>Реквизиты:</b>\n"
                    f"Банк: {config.PAYMENT_BANK}\n"
                    f"Карта: <code>{config.PAYMENT_CARD}</code>\n"
                    f"Получатель: {config.PAYMENT_HOLDER}",
                    reply_markup=kb_cancel(),
                )
                return
        except:
            pass

    # Сбрасываем состояние но НЕ active_order
    await state.clear()

    # Проверяем есть ли активный заказ в работе
    active_oid = await get_active_order(msg.from_user.id)
    if active_oid:
        order = await get_order(active_oid)
        if order and order["status"] in ("in_progress", "payment_confirmed"):
            await safe_answer(
                msg,
                f"<b>Inside PC</b>\n\n"
                f"У вас активный заказ #{active_oid}.\n"
                f"Все сообщения отправляются менеджеру.\n\n"
                f"Используйте /stop чтобы выйти из чата.",
                reply_markup=kb_start(),
            )
            return

    await safe_answer(
        msg,
        "<b>Inside PC</b>\n\n"
        "Сборка, апгрейд и консультации по ПК.\n"
        "Нажмите <b>Оформить заявку</b> чтобы начать.",
        reply_markup=kb_start(),
    )


@router.message(Command("stop"))
async def cmd_stop(msg: Message, state: FSMContext):
    """Выход из режима чата с менеджером."""
    await state.clear()
    await set_active_order(msg.from_user.id, 0)
    await safe_answer(
        msg,
        "<b>Inside PC</b>\n\nВы вышли из чата с менеджером.\nВыберите действие:",
        reply_markup=kb_start(),
    )


@router.callback_query(F.data == "home")
async def go_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await safe_edit(cb.message, "<b>Inside PC</b>\nВыберите действие:", reply_markup=kb_start())
    except:
        await safe_answer(cb.message, "<b>Inside PC</b>\nВыберите действие:", reply_markup=kb_start())
    await cb.answer()


@router.callback_query(F.data == "my_orders")
async def my_orders_cb(cb: CallbackQuery):
    orders = await get_user_orders(cb.from_user.id)
    if not orders:
        await safe_edit(cb.message, "У вас пока нет заказов.", reply_markup=kb_start())
        await cb.answer()
        return
    await safe_edit(cb.message, "<b>Ваши заказы:</b>", reply_markup=kb_orders(orders))
    await cb.answer()


@router.callback_query(F.data.startswith("view:"))
async def view_order_cb(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[1])
    order = await get_order(oid)
    if not order:
        await cb.answer("Не найден", show_alert=True)
        return
    p = config.PRICES.get(order["service_type"], {})
    sn = p.get("name", "?")
    prefix = p.get("prefix", "")
    st = STATUS_NAMES.get(order["status"], order["status"])
    text = (
        f"<b>Inside PC — Заказ #{oid}</b>\n\n"
        f"Услуга: {sn}\n"
        f"Стоимость: {prefix}{order['price_byn']} BYN / {prefix}{order['price_rub']} RUB\n"
        f"Статус: {st}\nДата: {order['created_at'][:16]}"
    )
    if order["status"] == "pending_payment":
        text += (
            f"\n\n<b>Для оплаты:</b>\n"
            f"Банк: {config.PAYMENT_BANK}\n"
            f"Карта: <code>{config.PAYMENT_CARD}</code>\n"
            f"Получатель: {config.PAYMENT_HOLDER}\n\n"
            f"Сохраните скриншот оплаты и отправьте его сюда."
        )
        await state.set_state(States.waiting_photo)
        await state.update_data(order_id=oid)
    elif order["status"] in ("payment_confirmed", "in_progress"):
        text += "\n\nМожете написать менеджеру прямо сюда."
        await state.set_state(States.chatting)
        await state.update_data(order_id=oid)
    await cb.message.edit_text(text, reply_markup=kb_view_order())
    await cb.answer()


@router.callback_query(F.data == "check_status")
async def ask_oid(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Введите номер заказа:")
    await state.set_state(States.waiting_oid)
    await cb.answer()


@router.message(States.waiting_oid, F.text)
async def process_oid(msg: Message, state: FSMContext):
    try:
        oid = int(msg.text.strip().replace("#", ""))
    except ValueError:
        await msg.answer("Введите число.")
        return
    order = await get_order(oid)
    if not order or order["user_id"] != msg.from_user.id:
        await safe_answer(msg, "Заказ не найден.", reply_markup=kb_start())
        await state.clear()
        return
    st = STATUS_NAMES.get(order["status"], order["status"])
    await safe_answer(msg, f"<b>Inside PC — Заказ #{oid}</b>\nСтатус: {st}", reply_markup=kb_start())
    await state.clear()


# ============================================================
#  ФОТО ОПЛАТЫ — создаёт топик
# ============================================================

@router.message(States.waiting_photo, F.photo)
async def recv_photo(msg: Message, state: FSMContext):
    data = await state.get_data()
    oid = data.get("order_id")
    if not oid:
        # Может быть фото без контекста — ищем последний неоплаченный
        pending = await get_latest_pending_order(msg.from_user.id)
        if pending:
            oid = pending["id"]
        else:
            await safe_answer(msg, "Нет заказов для оплаты. /start", reply_markup=kb_start())
            await state.clear()
            return

    fid = msg.photo[-1].file_id
    await save_payment_photo(oid, fid)

    # Получаем данные
    order = await get_order(oid)
    user = await get_user(msg.from_user.id)
    username = user["username"] if user else ""

    # Создаём топик СЕЙЧАС (при загрузке фото)
    tid = order.get("topic_id")
    if not tid:
        tid = await create_order_topic(oid, msg.from_user.id, username)

    # Пересылаем фото в топик
    if config.MANAGER_GROUP_ID and tid:
        try:
            await safe_bot_photo(
                config.MANAGER_GROUP_ID, fid,
                caption=f"<b>Фото оплаты — заказ #{oid}</b>",
                reply_markup=kb_admin_pay(oid),
                message_thread_id=tid,
            )
        except Exception as e:
            log.error(f"Фото менеджерам: {e}")

    await safe_answer(
        msg,
        f"<b>Скриншот получен!</b>\n\n"
        f"Заказ #{oid} — менеджер проверит оплату и свяжется с вами.\n"
        f"Ожидайте подтверждения.",
        reply_markup=kb_start(),
    )
    await state.clear()


# ============================================================
#  ЧАТ: пользователь -> менеджер (FSM состояние)
# ============================================================

@router.message(States.chatting, F.text)
async def chat_text(msg: Message, state: FSMContext):
    data = await state.get_data()
    oid = data.get("order_id")
    if not oid:
        return
    link = await get_topic_by_order(oid)
    if not link:
        await msg.answer("Топик не создан. Дождитесь подтверждения.")
        return
    try:
        await bot.send_message(config.MANAGER_GROUP_ID, message_thread_id=link["topic_id"],
            text=f"<b>Клиент:</b>\n\n{msg.text}")
        await msg.answer("Отправлено менеджеру.")
    except Exception as e:
        log.error(f"chat_text: {e}")


@router.message(States.chatting, F.photo)
async def chat_photo(msg: Message, state: FSMContext):
    data = await state.get_data()
    oid = data.get("order_id")
    if not oid:
        return
    link = await get_topic_by_order(oid)
    if not link:
        return
    try:
        await bot.send_photo(config.MANAGER_GROUP_ID, msg.photo[-1].file_id,
            caption=f"<b>Фото от клиента:</b>\n{msg.caption or ''}",
            message_thread_id=link["topic_id"])
        await msg.answer("Фото отправлено.")
    except Exception as e:
        log.error(f"chat_photo: {e}")


@router.message(States.chatting, F.video)
async def chat_video(msg: Message, state: FSMContext):
    data = await state.get_data()
    oid = data.get("order_id")
    if not oid:
        return
    link = await get_topic_by_order(oid)
    if not link:
        return
    try:
        await bot.send_video(config.MANAGER_GROUP_ID, msg.video.file_id,
            caption=f"<b>Видео от клиента:</b>\n{msg.caption or ''}",
            message_thread_id=link["topic_id"])
        await msg.answer("Видео отправлено.")
    except Exception as e:
        log.error(f"chat_video: {e}")


@router.message(States.chatting, F.document)
async def chat_doc(msg: Message, state: FSMContext):
    data = await state.get_data()
    oid = data.get("order_id")
    if not oid:
        return
    link = await get_topic_by_order(oid)
    if not link:
        return
    try:
        await bot.send_document(config.MANAGER_GROUP_ID, msg.document.file_id,
            caption=f"<b>Файл от клиента:</b>\n{msg.caption or ''}",
            message_thread_id=link["topic_id"])
        await msg.answer("Файл отправлен.")
    except Exception as e:
        log.error(f"chat_doc: {e}")


# ============================================================
#  АВТО-ПЕРЕСЫЛКА: все сообщения пользователя с active_order
#  (без FSM, работает всегда после подтверждения)
# ============================================================

@router.message(F.chat.type == "private", F.text)
async def auto_relay_text(msg: Message, state: FSMContext):
    """Если у пользователя есть active_order — пересылаем менеджеру."""
    # Проверяем что нет активного FSM состояния
    current = await state.get_state()
    if current:
        return  # FSM обработает

    oid = await get_active_order(msg.from_user.id)
    if not oid:
        return  # Нет активного заказа — игнорируем

    link = await get_topic_by_order(oid)
    if not link:
        return

    try:
        await bot.send_message(config.MANAGER_GROUP_ID, message_thread_id=link["topic_id"],
            text=f"<b>Клиент:</b>\n\n{msg.text}")
    except Exception as e:
        log.error(f"auto_text: {e}")


@router.message(F.chat.type == "private", F.photo)
async def auto_relay_photo(msg: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        return

    oid = await get_active_order(msg.from_user.id)
    if not oid:
        return

    link = await get_topic_by_order(oid)
    if not link:
        return

    try:
        await bot.send_photo(config.MANAGER_GROUP_ID, msg.photo[-1].file_id,
            caption=f"<b>Фото от клиента:</b>\n{msg.caption or ''}",
            message_thread_id=link["topic_id"])
    except Exception as e:
        log.error(f"auto_photo: {e}")


@router.message(F.chat.type == "private", F.video)
async def auto_relay_video(msg: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        return

    oid = await get_active_order(msg.from_user.id)
    if not oid:
        return

    link = await get_topic_by_order(oid)
    if not link:
        return

    try:
        await bot.send_video(config.MANAGER_GROUP_ID, msg.video.file_id,
            caption=f"<b>Видео от клиента:</b>\n{msg.caption or ''}",
            message_thread_id=link["topic_id"])
    except Exception as e:
        log.error(f"auto_video: {e}")


@router.message(F.chat.type == "private", F.document)
async def auto_relay_doc(msg: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        return

    oid = await get_active_order(msg.from_user.id)
    if not oid:
        return

    link = await get_topic_by_order(oid)
    if not link:
        return

    try:
        await bot.send_document(config.MANAGER_GROUP_ID, msg.document.file_id,
            caption=f"<b>Файл от клиента:</b>\n{msg.caption or ''}",
            message_thread_id=link["topic_id"])
    except Exception as e:
        log.error(f"auto_doc: {e}")


# ============================================================
#  МЕНЕДЖЕР -> ПОЛЬЗОВАТЕЛЬ (из топика)
# ============================================================

@router.message(F.message_thread_id, F.text)
async def mgr_text(msg: Message):
    if msg.chat.id != config.MANAGER_GROUP_ID or msg.from_user.is_bot:
        return
    link = await get_topic_link(msg.message_thread_id)
    if not link:
        return
    try:
        await bot.send_message(link["user_id"], f"<b>Inside PC:</b>\n\n{msg.text}")
    except Exception as e:
        log.error(f"mgr_text: {e}")


@router.message(F.message_thread_id, F.photo)
async def mgr_photo(msg: Message):
    if msg.chat.id != config.MANAGER_GROUP_ID or msg.from_user.is_bot:
        return
    link = await get_topic_link(msg.message_thread_id)
    if not link:
        return
    try:
        await bot.send_photo(link["user_id"], msg.photo[-1].file_id,
            caption=f"<b>Inside PC:</b>\n{msg.caption or ''}")
    except Exception as e:
        log.error(f"mgr_photo: {e}")


@router.message(F.message_thread_id, F.video)
async def mgr_video(msg: Message):
    if msg.chat.id != config.MANAGER_GROUP_ID or msg.from_user.is_bot:
        return
    link = await get_topic_link(msg.message_thread_id)
    if not link:
        return
    try:
        await bot.send_video(link["user_id"], msg.video.file_id,
            caption=f"<b>Inside PC:</b>\n{msg.caption or ''}")
    except Exception as e:
        log.error(f"mgr_video: {e}")


@router.message(F.message_thread_id, F.document)
async def mgr_doc(msg: Message):
    if msg.chat.id != config.MANAGER_GROUP_ID or msg.from_user.is_bot:
        return
    link = await get_topic_link(msg.message_thread_id)
    if not link:
        return
    try:
        await bot.send_document(link["user_id"], msg.document.file_id,
            caption=f"<b>Inside PC:</b>\n{msg.caption or ''}")
    except Exception as e:
        log.error(f"mgr_doc: {e}")


# ============================================================
#  АДМИН: ОПЛАТА И СТАТУСЫ
# ============================================================

@router.callback_query(F.data.startswith("cpay:"))
async def confirm_pay(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    await update_status(oid, "payment_confirmed")
    order = await get_order(oid)
    try:
        await bot.send_message(order["user_id"],
            f"<b>Inside PC — Оплата заказа #{oid} подтверждена!</b>\n"
            f"Менеджер свяжется с вами в этом чате.")
    except Exception as e:
        log.error(f"cpay: {e}")
    try:
        await cb.message.edit_caption(caption=f"<b>Заказ #{oid} — ПОДТВЕРЖДЕНО</b>")
    except:
        await cb.message.answer(f"<b>Заказ #{oid} — ПОДТВЕРЖДЕНО</b>")
    await cb.answer("Подтверждено")


@router.callback_query(F.data.startswith("rpay:"))
async def reject_pay(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    await update_status(oid, "pending_payment")
    order = await get_order(oid)
    try:
        await bot.send_message(order["user_id"],
            f"<b>Inside PC — Оплата #{oid} не подтверждена.</b>\n"
            f"Проверьте реквизиты и отправьте скриншот заново.\n\n"
            f"Карта: <code>{config.PAYMENT_CARD}</code>\n"
            f"Получатель: {config.PAYMENT_HOLDER}")
    except Exception as e:
        log.error(f"rpay: {e}")
    try:
        await cb.message.edit_caption(caption=f"<b>Заказ #{oid} — ОТКЛОНЕНО</b>")
    except:
        await cb.message.answer(f"<b>Заказ #{oid} — ОТКЛОНЕНО</b>")
    await cb.answer("Отклонено")


@router.callback_query(F.data.startswith("ss:"))
async def set_status_cb(cb: CallbackQuery):
    parts = cb.data.split(":")
    oid, new_st = int(parts[1]), parts[2]
    await update_status(oid, new_st)
    order = await get_order(oid)
    st_text = STATUS_NAMES.get(new_st, new_st)

    if new_st == "in_progress":
        # Активируем авто-пересылку для пользователя
        await set_active_order(order["user_id"], oid)
        try:
            await bot.send_message(order["user_id"],
                f"<b>Inside PC — Заказ #{oid} взят в работу!</b>\n\n"
                f"<b>Внимание!</b> Все ваши сообщения в этом чате "
                f"теперь будут отправляться менеджеру.\n\n"
                f"Используйте /stop чтобы выйти из режима чата.")
        except Exception as e:
            log.error(f"ss in_progress: {e}")

    elif new_st in ("completed", "cancelled"):
        # Деактивируем авто-пересылку
        await set_active_order(order["user_id"], 0)
        try:
            await bot.send_message(order["user_id"],
                f"<b>Inside PC — Заказ #{oid}</b>\nСтатус: {st_text}\n\n"
                f"Чат с менеджером завершён.")
        except Exception as e:
            log.error(f"ss end: {e}")
    else:
        try:
            await bot.send_message(order["user_id"],
                f"<b>Inside PC — Заказ #{oid}</b>\nНовый статус: {st_text}")
        except Exception as e:
            log.error(f"ss: {e}")

    await safe_bot_msg(config.MANAGER_GROUP_ID,
        f"Статус #{oid}: {st_text}", reply_markup=kb_admin_manage(oid))
    await cb.answer(st_text)