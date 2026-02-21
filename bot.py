"""
Inside PC — Telegram бот (aiogram 3.25) + FastAPI в одном файле.

Бот: /start, приём фото оплаты, чат с менеджером, статусы.
API: POST /api/order, GET /api/status/{id}, GET /api/prices.
Статика Mini App раздаётся через FastAPI.
"""

import json
import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, WebAppInfo,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import *
from database import *

log = logging.getLogger("insidepc")

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
    """Mini App отправляет сюда данные заявки."""
    if data.service_type not in PRICES:
        raise HTTPException(400, "Неизвестная услуга")

    p = PRICES[data.service_type]
    await upsert_user(data.user_id, data.username, data.full_name)

    oid = await create_order(
        data.user_id, data.service_type, data.has_parts_list,
        data.parts_data, data.description, p["byn"], p["rub"]
    )

    # Уведомляем через бота
    try:
        await send_user_invoice(data.user_id, oid)
        await send_manager_alert(oid, data.user_id)
    except Exception as e:
        log.error(f"Ошибка уведомления: {e}")

    order = await get_order(oid)
    return {"id": oid, "status": order["status"], "price_byn": p["byn"], "price_rub": p["rub"]}


@app.get("/api/status/{order_id}")
async def api_status(order_id: int):
    """Статус заказа."""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    return {
        "order_id": order["id"],
        "status": order["status"],
        "status_text": STATUS_NAMES.get(order["status"], order["status"]),
        "service": PRICES.get(order["service_type"], {}).get("name", ""),
    }


@app.get("/api/prices")
async def api_prices():
    return PRICES


# ============================================================
#                      TELEGRAM BOT
# ============================================================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)


# --- FSM ---
class States(StatesGroup):
    waiting_photo = State()   # Ждём фото оплаты
    chatting = State()        # Чат с менеджером
    waiting_oid = State()     # Ввод номера заказа


# --- Клавиатуры ---
def kb_start():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оформить заявку", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="Мои заказы", callback_data="my_orders")],
        [InlineKeyboardButton(text="Проверить статус", callback_data="check_status")],
    ])


def kb_orders(orders):
    rows = []
    for o in orders[:10]:
        s = STATUS_NAMES.get(o["status"], o["status"])
        n = PRICES.get(o["service_type"], {}).get("name", "?")
        rows.append([InlineKeyboardButton(text=f"#{o['id']} | {n} | {s}", callback_data=f"view:{o['id']}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_pay(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить оплату", callback_data=f"cpay:{oid}")],
        [InlineKeyboardButton(text="Отклонить", callback_data=f"rpay:{oid}")],
    ])


def kb_admin_manage(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="В работу", callback_data=f"ss:{oid}:in_progress"),
         InlineKeyboardButton(text="Завершить", callback_data=f"ss:{oid}:completed")],
        [InlineKeyboardButton(text="Отменить", callback_data=f"ss:{oid}:cancelled")],
    ])


# --- Уведомления (вызываются из API) ---
async def send_user_invoice(uid, oid):
    """Отправляет пользователю реквизиты для оплаты."""
    order = await get_order(oid)
    sn = PRICES[order["service_type"]]["name"]
    text = (
        f"<b><tg-emoji id=\"{E['doc']}\">_</tg-emoji> Inside PC — Заявка #{oid}</b>\n\n"
        f"<b>Услуга:</b> {sn}\n"
        f"<b>Стоимость:</b> {order['price_byn']} BYN / {order['price_rub']} RUB\n\n"
        f"<b>Реквизиты для оплаты:</b>\n"
        f"Банк: {PAYMENT_BANK}\n"
        f"Карта: <code>{PAYMENT_CARD}</code>\n"
        f"Получатель: {PAYMENT_HOLDER}\n\n"
        f"<tg-emoji id=\"{E['money']}\">_</tg-emoji> "
        f"Переведите сумму и отправьте <b>фото чека</b> сюда."
    )
    await bot.send_message(uid, text)


async def send_manager_alert(oid, uid):
    """Создаёт топик в группе менеджеров и шлёт инфо о заказе."""
    if not MANAGER_GROUP_ID:
        return
    order = await get_order(oid)
    sn = PRICES[order["service_type"]]["name"]

    # Создаём топик
    try:
        topic = await bot.create_forum_topic(MANAGER_GROUP_ID, f"Inside PC #{oid} | {sn}")
        tid = topic.message_thread_id
        await save_topic(tid, oid, uid)
    except:
        tid = None

    # Формируем текст
    parts_text = ""
    if order["has_parts"] and order["parts_data"]:
        parts = json.loads(order["parts_data"])
        parts_text = "\n<b>Комплектующие:</b>\n" + "".join(
            f"  - {k}: {v}\n" for k, v in parts.items() if v
        )

    desc = f"\n<b>Описание:</b> {order['description']}" if order["description"] else ""

    text = (
        f"<b><tg-emoji id=\"{E['bell']}\">_</tg-emoji> Inside PC — Новая заявка #{oid}</b>\n\n"
        f"<tg-emoji id=\"{E['user']}\">_</tg-emoji> Клиент: {uid}\n"
        f"Услуга: {sn}\n"
        f"Стоимость: {order['price_byn']} BYN / {order['price_rub']} RUB"
        f"{parts_text}{desc}"
    )

    kwargs = {"chat_id": MANAGER_GROUP_ID, "text": text, "reply_markup": kb_admin_manage(oid)}
    if tid:
        kwargs["message_thread_id"] = tid
    await bot.send_message(**kwargs)


# --- Команда /start ---
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    await msg.answer(
        f"<b><tg-emoji id=\"{E['pc']}\">_</tg-emoji> Inside PC</b>\n\n"
        f"<tg-emoji id=\"{E['tools']}\">_</tg-emoji> Сборка, апгрейд и консультации по ПК.\n"
        f"Нажмите <b>Оформить заявку</b> чтобы начать.",
        reply_markup=kb_start()
    )


@router.callback_query(F.data == "home")
async def go_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        f"<b><tg-emoji id=\"{E['pc']}\">_</tg-emoji> Inside PC</b>\nВыберите действие:",
        reply_markup=kb_start()
    )
    await cb.answer()


# --- Мои заказы ---
@router.callback_query(F.data == "my_orders")
async def my_orders(cb: CallbackQuery):
    orders = await get_user_orders(cb.from_user.id)
    if not orders:
        await cb.message.edit_text("У вас пока нет заказов.", reply_markup=kb_start())
        await cb.answer()
        return
    await cb.message.edit_text(
        f"<b><tg-emoji id=\"{E['doc']}\">_</tg-emoji> Ваши заказы:</b>",
        reply_markup=kb_orders(orders)
    )
    await cb.answer()


# --- Просмотр заказа ---
@router.callback_query(F.data.startswith("view:"))
async def view_order_cb(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[1])
    order = await get_order(oid)
    if not order:
        await cb.answer("Не найден", show_alert=True)
        return

    sn = PRICES.get(order["service_type"], {}).get("name", "?")
    st = STATUS_NAMES.get(order["status"], order["status"])
    text = (
        f"<b>Inside PC — Заказ #{oid}</b>\n\n"
        f"Услуга: {sn}\n"
        f"Стоимость: {order['price_byn']} BYN / {order['price_rub']} RUB\n"
        f"Статус: {st}\n"
        f"Дата: {order['created_at'][:16]}"
    )

    if order["status"] == "pending_payment":
        text += "\n\nОтправьте фото чека оплаты прямо сюда."
        await state.set_state(States.waiting_photo)
        await state.update_data(order_id=oid)

    elif order["status"] in ("payment_confirmed", "in_progress"):
        text += "\n\nМожете написать сообщение менеджеру прямо сюда."
        await state.set_state(States.chatting)
        await state.update_data(order_id=oid)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад к заказам", callback_data="my_orders")]
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


# --- Проверка статуса по номеру ---
@router.callback_query(F.data == "check_status")
async def ask_oid(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text(
        f"<tg-emoji id=\"{E['doc']}\">_</tg-emoji> Введите номер заказа:"
    )
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
        await msg.answer("Заказ не найден.", reply_markup=kb_start())
        await state.clear()
        return
    st = STATUS_NAMES.get(order["status"], order["status"])
    await msg.answer(f"<b>Inside PC — Заказ #{oid}</b>\nСтатус: {st}", reply_markup=kb_start())
    await state.clear()


# --- Приём фото оплаты ---
@router.message(States.waiting_photo, F.photo)
async def recv_photo(msg: Message, state: FSMContext):
    data = await state.get_data()
    oid = data.get("order_id")
    if not oid:
        await msg.answer("Ошибка. Начните заново.", reply_markup=kb_start())
        await state.clear()
        return

    fid = msg.photo[-1].file_id
    await save_payment_photo(oid, fid)

    # Пересылаем менеджерам
    order = await get_order(oid)
    if MANAGER_GROUP_ID and order.get("topic_id"):
        await bot.send_photo(
            MANAGER_GROUP_ID, fid,
            caption=f"<b>Inside PC — Фото оплаты, заказ #{oid}</b>",
            message_thread_id=order["topic_id"],
            reply_markup=kb_admin_pay(oid)
        )

    await msg.answer(
        f"<tg-emoji id=\"{E['ok']}\">_</tg-emoji> <b>Фото получено!</b>\n"
        f"Заказ #{oid} — менеджер проверит оплату.",
        reply_markup=kb_start()
    )
    await state.clear()


# --- Чат пользователь -> менеджер ---
@router.message(States.chatting, F.text)
async def user_to_manager(msg: Message, state: FSMContext):
    data = await state.get_data()
    oid = data.get("order_id")
    if not oid:
        return

    link = await get_topic_by_order(oid)
    if not link:
        await msg.answer("Топик ещё не создан. Дождитесь подтверждения оплаты.")
        return

    await bot.send_message(
        MANAGER_GROUP_ID,
        message_thread_id=link["topic_id"],
        text=f"<b><tg-emoji id=\"{E['user']}\">_</tg-emoji> Клиент:</b>\n\n{msg.text}"
    )
    await msg.answer(f"<tg-emoji id=\"{E['ok']}\">_</tg-emoji> Отправлено менеджеру.")


# --- Чат менеджер -> пользователь (сообщения из топика) ---
@router.message(F.chat.id == MANAGER_GROUP_ID, F.message_thread_id, F.text)
async def manager_to_user(msg: Message):
    if msg.from_user.is_bot:
        return
    link = await get_topic_link(msg.message_thread_id)
    if not link:
        return
    await bot.send_message(
        link["user_id"],
        f"<b><tg-emoji id=\"{E['tools']}\">_</tg-emoji> Inside PC:</b>\n\n{msg.text}"
    )


# --- Админ: подтвердить/отклонить оплату ---
@router.callback_query(F.data.startswith("cpay:"))
async def confirm_pay(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    await update_status(oid, "payment_confirmed")
    order = await get_order(oid)
    await bot.send_message(
        order["user_id"],
        f"<tg-emoji id=\"{E['ok']}\">_</tg-emoji> <b>Inside PC — Оплата заказа #{oid} подтверждена!</b>\n"
        f"Менеджер свяжется с вами в этом чате."
    )
    await cb.message.edit_caption(caption=f"<b>Inside PC — Заказ #{oid} — ОПЛАТА ПОДТВЕРЖДЕНА</b>")
    await cb.answer("Подтверждено")


@router.callback_query(F.data.startswith("rpay:"))
async def reject_pay(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    await update_status(oid, "pending_payment")
    order = await get_order(oid)
    await bot.send_message(
        order["user_id"],
        f"<tg-emoji id=\"{E['bell']}\">_</tg-emoji> <b>Inside PC — Оплата заказа #{oid} не подтверждена.</b>\n"
        f"Проверьте реквизиты и отправьте фото заново."
    )
    await cb.message.edit_caption(caption=f"<b>Inside PC — Заказ #{oid} — ОПЛАТА ОТКЛОНЕНА</b>")
    await cb.answer("Отклонено")


# --- Админ: смена статуса ---
@router.callback_query(F.data.startswith("ss:"))
async def set_status_cb(cb: CallbackQuery):
    parts = cb.data.split(":")
    oid, new_st = int(parts[1]), parts[2]
    await update_status(oid, new_st)
    order = await get_order(oid)
    st_text = STATUS_NAMES.get(new_st, new_st)

    await bot.send_message(
        order["user_id"],
        f"<tg-emoji id=\"{E['bell']}\">_</tg-emoji> <b>Inside PC — Заказ #{oid}</b>\nНовый статус: {st_text}"
    )
    await cb.message.answer(f"Inside PC — Статус #{oid} изменён: {st_text}", reply_markup=kb_admin_manage(oid))
    await cb.answer(st_text)
