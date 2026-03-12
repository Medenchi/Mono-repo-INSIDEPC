"""
Inside PC — Бот + API + Портфолио.
"""

import asyncio
import json
import logging
import os

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, WebAppInfo, InputMediaPhoto,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

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

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


@app.get("/web")
@app.get("/web/")
@app.get("/web/index.html")
async def serve_index():
    p = os.path.join(WEB_DIR, "index.html")
    if not os.path.isfile(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="text/html")


@app.get("/web/admin")
@app.get("/web/admin.html")
async def serve_admin():
    p = os.path.join(WEB_DIR, "admin.html")
    if not os.path.isfile(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="text/html")


@app.get("/web/portfolio")
@app.get("/web/portfolio.html")
async def serve_portfolio_admin():
    p = os.path.join(WEB_DIR, "portfolio.html")
    if not os.path.isfile(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="text/html")


app.mount("/web/static", StaticFiles(directory=WEB_DIR), name="web_static")


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
    needs_quote = p.get("needs_quote", False)
    status = "pending_quote" if needs_quote else "pending_payment"
    oid = await create_order(
        data.user_id, data.service_type, data.has_parts_list,
        data.parts_data, data.description, p["byn"], p["rub"], status=status,
    )
    if needs_quote:
        try:
            await _handle_new_quote(oid, data.user_id, data.username)
        except Exception as e:
            log.error(f"quote init: {e}")
    return {"id": oid, "needs_quote": needs_quote, "bot_username": config.BOT_USERNAME}


@app.get("/api/orders/{user_id}")
async def api_user_orders(user_id: int):
    orders = await get_user_orders(user_id)
    out = []
    for o in orders:
        p = config.PRICES.get(o["service_type"], {})
        out.append({
            "id": o["id"], "service": p.get("name", "?"),
            "status": o["status"],
            "status_text": STATUS_NAMES.get(o["status"], o["status"]),
            "price_byn": o["price_byn"], "price_rub": o["price_rub"],
            "price_prefix": p.get("prefix", ""),
            "date": o["created_at"][:16],
        })
    return out


@app.get("/api/order/{order_id}")
async def api_order_detail(order_id: int):
    order = await get_order(order_id)
    if not order:
        raise HTTPException(404)
    user = await get_user(order["user_id"])
    parts = None
    if order["parts_data"]:
        try:
            parts = json.loads(order["parts_data"])
        except Exception:
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


# ---- PORTFOLIO API ----

class PortfolioIn(BaseModel):
    title: str = ""
    description: str = ""
    specs: str = ""
    price_byn: float = 0
    price_rub: float = 0
    category: str = ""


@app.get("/api/portfolio")
async def api_portfolio():
    items = await get_portfolio_all()
    out = []
    for item in items:
        try:
            photos = json.loads(item["photo_ids"])
        except Exception:
            photos = []
        out.append({
            "id": item["id"], "title": item["title"],
            "description": item["description"], "specs": item["specs"],
            "price_byn": item["price_byn"], "price_rub": item["price_rub"],
            "category": item["category"],
            "photos": photos, "photo_count": len(photos),
            "date": item["created_at"][:16],
        })
    return out


@app.get("/api/portfolio/{pid}")
async def api_portfolio_item(pid: int):
    item = await get_portfolio_item(pid)
    if not item:
        raise HTTPException(404)
    try:
        photos = json.loads(item["photo_ids"])
    except Exception:
        photos = []
    return {
        "id": item["id"], "title": item["title"],
        "description": item["description"], "specs": item["specs"],
        "price_byn": item["price_byn"], "price_rub": item["price_rub"],
        "category": item["category"], "photos": photos,
        "is_visible": item["is_visible"], "date": item["created_at"][:16],
    }


@app.post("/api/portfolio")
async def api_portfolio_create(data: PortfolioIn):
    pid = await add_portfolio_item(
        data.title, data.description, data.specs,
        data.price_byn, data.price_rub, data.category,
    )
    return {"id": pid}


@app.put("/api/portfolio/{pid}")
async def api_portfolio_update(pid: int, data: PortfolioIn):
    item = await get_portfolio_item(pid)
    if not item:
        raise HTTPException(404)
    await update_portfolio(pid,
        title=data.title, description=data.description,
        specs=data.specs, price_byn=data.price_byn,
        price_rub=data.price_rub, category=data.category,
    )
    return {"ok": True}


@app.delete("/api/portfolio/{pid}")
async def api_portfolio_delete(pid: int):
    await delete_portfolio(pid)
    return {"ok": True}


@app.get("/api/portfolio/{pid}/photo/{file_id}")
async def api_portfolio_photo_url(pid: int, file_id: str):
    """Получить URL фото через Bot API."""
    try:
        file = await bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{file.file_path}"
        return {"url": url}
    except Exception:
        raise HTTPException(404, "Фото не найдено")


@app.get("/api/prices")
async def api_prices():
    return config.PRICES


# ============================================================
#                      BOT
# ============================================================

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)


class States(StatesGroup):
    waiting_photo = State()
    chatting = State()
    waiting_oid = State()
    waiting_price = State()
    # Портфолио
    pf_title = State()
    pf_specs = State()
    pf_price = State()
    pf_desc = State()
    pf_photo = State()


# ============================================================
#  УТИЛИТЫ
# ============================================================

def _base():
    url = getattr(config, "WEBAPP_URL", "")
    return url.rstrip("/") if url else ""


def _admin_url(oid):
    b = _base()
    if not b:
        return None
    return f"{b}/admin.html?order_id={oid}" if b.endswith("/web") else f"{b}/web/admin.html?order_id={oid}"


def _portfolio_url():
    b = _base()
    if not b:
        return None
    return f"{b}/portfolio.html" if b.endswith("/web") else f"{b}/web/portfolio.html"


# ============================================================
#  SAFE SEND
# ============================================================

def _strip(mk):
    if not mk or not hasattr(mk, "inline_keyboard"):
        return mk
    rows = []
    for row in mk.inline_keyboard:
        nr = []
        for b in row:
            d = b.model_dump(exclude_none=True)
            d.pop("style", None)
            nr.append(InlineKeyboardButton(**d))
        rows.append(nr)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _retry(factory):
    global STYLE_OK
    try:
        return await factory(True)
    except TelegramBadRequest as e:
        if "invalid button style" in str(e).lower():
            STYLE_OK = False
            return await factory(False)
        raise


async def safe_send(cid, text, reply_markup=None, **kw):
    async def do(ok):
        return await bot.send_message(cid, text, reply_markup=reply_markup if ok else _strip(reply_markup), **kw)
    return await _retry(do)


async def safe_answer(msg, text, reply_markup=None, **kw):
    async def do(ok):
        return await msg.answer(text, reply_markup=reply_markup if ok else _strip(reply_markup), **kw)
    return await _retry(do)


async def safe_edit(msg, text, reply_markup=None, **kw):
    async def do(ok):
        return await msg.edit_text(text, reply_markup=reply_markup if ok else _strip(reply_markup), **kw)
    return await _retry(do)


async def safe_photo(cid, photo, caption=None, reply_markup=None, **kw):
    async def do(ok):
        return await bot.send_photo(cid, photo, caption=caption, reply_markup=reply_markup if ok else _strip(reply_markup), **kw)
    return await _retry(do)


# ============================================================
#  КЛАВИАТУРЫ
# ============================================================

def kb_start():
    b = _base()
    rows = []
    if b:
        rows.append([InlineKeyboardButton(text="Оформить заявку", web_app=WebAppInfo(url=b))])
    rows.append([InlineKeyboardButton(text="Мои заказы", callback_data="my_orders", **S("primary"))])
    rows.append([InlineKeyboardButton(text="Проверить статус", callback_data="check_status")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_orders(orders):
    rows = []
    for o in orders[:10]:
        s = STATUS_NAMES.get(o["status"], o["status"])
        n = config.PRICES.get(o["service_type"], {}).get("name", "?")
        st = {}
        if o["status"] in ("payment_confirmed", "completed"):
            st = S("success")
        elif o["status"] == "cancelled":
            st = S("danger")
        elif o["status"] == "in_progress":
            st = S("primary")
        rows.append([InlineKeyboardButton(text=f"#{o['id']} | {n} | {s}", callback_data=f"view:{o['id']}", **st)])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_pay(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"cpay:{oid}", **S("success"))],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rpay:{oid}", **S("danger"))],
    ])


def kb_admin_manage(oid):
    rows = [
        [InlineKeyboardButton(text="В работу", callback_data=f"ss:{oid}:in_progress", **S("primary")),
         InlineKeyboardButton(text="Завершить", callback_data=f"ss:{oid}:completed", **S("success"))],
        [InlineKeyboardButton(text="Отменить", callback_data=f"ss:{oid}:cancelled", **S("danger"))],
    ]
    link = _admin_url(oid)
    if link:
        rows.append([InlineKeyboardButton(text="Детали", url=link)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_quote(oid):
    rows = [
        [InlineKeyboardButton(text="Назначить цену", callback_data=f"quote:{oid}", **S("primary"))],
        [InlineKeyboardButton(text="Отменить", callback_data=f"ss:{oid}:cancelled", **S("danger"))],
    ]
    link = _admin_url(oid)
    if link:
        rows.append([InlineKeyboardButton(text="Детали", url=link)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="my_orders")]])


def kb_cancel():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="home", **S("danger"))]])


# Портфолио
def kb_pf_item(pid):
    rows = [
        [InlineKeyboardButton(text="Название", callback_data=f"pf:title:{pid}"),
         InlineKeyboardButton(text="Характеристики", callback_data=f"pf:specs:{pid}")],
        [InlineKeyboardButton(text="Цена", callback_data=f"pf:price:{pid}"),
         InlineKeyboardButton(text="Описание", callback_data=f"pf:desc:{pid}")],
        [InlineKeyboardButton(text="Добавить фото", callback_data=f"pf:photo:{pid}", **S("primary"))],
        [InlineKeyboardButton(text="Удалить работу", callback_data=f"pf:del:{pid}", **S("danger"))],
        [InlineKeyboardButton(text="Назад к списку", callback_data="pf:list")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_pf_manage():
    url = _portfolio_url()
    rows = [
        [InlineKeyboardButton(text="Добавить работу", callback_data="pf:new", **S("success"))],
        [InlineKeyboardButton(text="Список работ", callback_data="pf:list", **S("primary"))],
    ]
    if url:
        rows.append([InlineKeyboardButton(text="Открыть панель", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
#  ТЕКСТ ЗАКАЗА
# ============================================================

def _order_text(oid, order, uname, is_quote=False):
    sn = config.PRICES.get(order["service_type"], {}).get("name", "?")
    prefix = config.PRICES.get(order["service_type"], {}).get("prefix", "")
    label = "ЗАЯВКА НА ОЦЕНКУ" if is_quote else "НОВАЯ ЗАЯВКА"
    text = f"<b>{label} #{oid}</b>\n\nКлиент: {uname}\nУслуга: {sn}\n"
    if is_quote:
        text += f"Мин. стоимость: {prefix}{order['price_byn']} BYN / {prefix}{order['price_rub']} RUB\n\n<b>Назначьте цену кнопкой.</b>"
    else:
        text += f"Стоимость: {order['price_byn']} BYN / {order['price_rub']} RUB"
    if order["has_parts"] and order["parts_data"]:
        try:
            parts = json.loads(order["parts_data"])
            lines = "".join(f"  — {k}: {v}\n" for k, v in parts.items() if v)
            if lines:
                text += f"\n\n<b>Комплектующие:</b>\n{lines}"
        except Exception:
            pass
    if order["description"]:
        text += f"\n\n<b>Описание:</b>\n{order['description']}"
    return text


# ============================================================
#  ТОПИКИ
# ============================================================

async def _create_topic(oid, uid, username=""):
    if not config.MANAGER_GROUP_ID:
        return None
    order = await get_order(oid)
    if not order:
        return None
    sn = config.PRICES.get(order["service_type"], {}).get("name", "?")
    uname = f"@{username}" if username else f"ID:{uid}"
    try:
        t = await bot.create_forum_topic(chat_id=config.MANAGER_GROUP_ID, name=f"{uname} | {sn}")
        tid = t.message_thread_id
        await save_topic(tid, oid, uid)
    except Exception as e:
        log.error(f"topic: {e}")
        return None
    text = _order_text(oid, order, uname)
    try:
        await safe_send(config.MANAGER_GROUP_ID, text, reply_markup=kb_admin_manage(oid), message_thread_id=tid)
    except Exception as e:
        log.error(f"topic msg: {e}")
    return tid


async def _handle_new_quote(oid, uid, username=""):
    if not config.MANAGER_GROUP_ID:
        return
    order = await get_order(oid)
    if not order:
        return
    sn = config.PRICES.get(order["service_type"], {}).get("name", "?")
    uname = f"@{username}" if username else f"ID:{uid}"
    try:
        t = await bot.create_forum_topic(chat_id=config.MANAGER_GROUP_ID, name=f"{uname} | {sn}", icon_color=7322096)
        tid = t.message_thread_id
        await save_topic(tid, oid, uid)
    except Exception as e:
        log.error(f"quote topic: {e}")
        return
    text = _order_text(oid, order, uname, is_quote=True)
    try:
        await safe_send(config.MANAGER_GROUP_ID, text, reply_markup=kb_quote(oid), message_thread_id=tid)
    except Exception as e:
        log.error(f"quote msg: {e}")
    try:
        await bot.send_message(uid,
            f"<b>Inside PC — Заявка #{oid}</b>\n\nЗаявка на апгрейд отправлена менеджеру.\nМы рассчитаем стоимость и отправим реквизиты сюда.")
    except Exception as e:
        log.error(f"quote user: {e}")


async def create_portfolio_topic():
    """Создаёт тему 'Портфолио' в группе менеджеров."""
    if not config.MANAGER_GROUP_ID:
        return
    try:
        t = await bot.create_forum_topic(chat_id=config.MANAGER_GROUP_ID, name="Портфолио", icon_color=16766590)
        tid = t.message_thread_id
        await safe_send(
            config.MANAGER_GROUP_ID,
            "<b>Управление портфолио</b>\n\nИспользуйте кнопки ниже для добавления и редактирования работ.",
            reply_markup=kb_pf_manage(),
            message_thread_id=tid,
        )
        log.info(f"Портфолио топик: tid={tid}")
        return tid
    except Exception as e:
        log.error(f"portfolio topic: {e}")
        return None


# ============================================================
#  ПЕРЕСЫЛКА
# ============================================================

async def relay_to_topic(msg, oid):
    link = await get_topic_by_order(oid)
    if not link:
        return False
    tid = link["topic_id"]
    kw = {"message_thread_id": tid}
    try:
        if msg.photo:
            await bot.send_photo(config.MANAGER_GROUP_ID, msg.photo[-1].file_id, caption=f"<b>Клиент:</b>\n{msg.caption or ''}", **kw)
        elif msg.video:
            await bot.send_video(config.MANAGER_GROUP_ID, msg.video.file_id, caption=f"<b>Клиент:</b>\n{msg.caption or ''}", **kw)
        elif msg.document:
            await bot.send_document(config.MANAGER_GROUP_ID, msg.document.file_id, caption=f"<b>Клиент:</b>\n{msg.caption or ''}", **kw)
        elif msg.voice:
            await bot.send_voice(config.MANAGER_GROUP_ID, msg.voice.file_id, caption="<b>Клиент</b>", **kw)
        elif msg.video_note:
            await bot.send_video_note(config.MANAGER_GROUP_ID, msg.video_note.file_id, **kw)
        elif msg.sticker:
            await bot.send_sticker(config.MANAGER_GROUP_ID, msg.sticker.file_id, **kw)
        elif msg.text:
            await bot.send_message(config.MANAGER_GROUP_ID, f"<b>Клиент:</b>\n\n{msg.text}", **kw)
        else:
            await bot.forward_message(config.MANAGER_GROUP_ID, msg.chat.id, msg.message_id, **kw)
        return True
    except Exception as e:
        log.error(f"relay: {e}")
        return False


async def relay_to_user(msg, uid):
    try:
        if msg.photo:
            await bot.send_photo(uid, msg.photo[-1].file_id, caption=f"<b>Inside PC:</b>\n{msg.caption or ''}")
        elif msg.video:
            await bot.send_video(uid, msg.video.file_id, caption=f"<b>Inside PC:</b>\n{msg.caption or ''}")
        elif msg.document:
            await bot.send_document(uid, msg.document.file_id, caption=f"<b>Inside PC:</b>\n{msg.caption or ''}")
        elif msg.voice:
            await bot.send_voice(uid, msg.voice.file_id, caption="<b>Inside PC:</b>")
        elif msg.video_note:
            await bot.send_video_note(uid, msg.video_note.file_id)
        elif msg.sticker:
            await bot.send_sticker(uid, msg.sticker.file_id)
        elif msg.text:
            await bot.send_message(uid, f"<b>Inside PC:</b>\n\n{msg.text}")
        else:
            await bot.forward_message(uid, msg.chat.id, msg.message_id)
        return True
    except Exception as e:
        log.error(f"relay user: {e}")
        return False


# ============================================================
#  ХЭНДЛЕРЫ
# ============================================================

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext, command: CommandObject):
    await upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.full_name or "")
    args = command.args
    if args and args.startswith("pay_"):
        try:
            oid = int(args[4:])
            order = await get_order(oid)
            if order and order["user_id"] == msg.from_user.id:
                if order["status"] == "pending_payment":
                    await state.set_state(States.waiting_photo)
                    await state.update_data(order_id=oid)
                    await safe_answer(msg,
                        f"<b>Inside PC — Заказ #{oid}</b>\n\n"
                        f"<b>К оплате: {order['price_byn']} BYN / {order['price_rub']} RUB</b>\n\n"
                        f"<b>Реквизиты:</b>\nБанк: {config.PAYMENT_BANK}\n"
                        f"Карта: <code>{config.PAYMENT_CARD}</code>\n"
                        f"Получатель: {config.PAYMENT_HOLDER}\n\n"
                        f"Переведите и отправьте скриншот чека.",
                        reply_markup=kb_cancel())
                    return
                elif order["status"] == "pending_quote":
                    await safe_answer(msg, f"<b>Заказ #{oid}</b>\n\nОжидает оценки менеджером.", reply_markup=kb_start())
                    return
        except (ValueError, TypeError):
            pass
    await state.clear()
    active = await get_active_order(msg.from_user.id)
    if active:
        order = await get_order(active)
        if order and order["status"] in ("in_progress", "payment_confirmed"):
            await safe_answer(msg, f"<b>Inside PC</b>\n\nАктивный заказ #{active}.\n/stop — выйти.", reply_markup=kb_start())
            return
    await safe_answer(msg, "<b>Inside PC</b>\n\nСборка, апгрейд и консультации.\nНажмите <b>Оформить заявку</b>.", reply_markup=kb_start())


@router.message(Command("stop"))
async def cmd_stop(msg: Message, state: FSMContext):
    await state.clear()
    await set_active_order(msg.from_user.id, 0)
    await safe_answer(msg, "<b>Inside PC</b>\n\nВы вышли из чата.", reply_markup=kb_start())


@router.message(Command("portfolio"))
async def cmd_portfolio(msg: Message):
    """Создать тему портфолио (только для админов)."""
    if msg.chat.id == config.MANAGER_GROUP_ID:
        await create_portfolio_topic()


@router.callback_query(F.data == "home")
async def go_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await safe_edit(cb.message, "<b>Inside PC</b>\nВыберите:", reply_markup=kb_start())
    except Exception:
        await safe_answer(cb.message, "<b>Inside PC</b>\nВыберите:", reply_markup=kb_start())
    await cb.answer()


@router.callback_query(F.data == "my_orders")
async def my_orders(cb: CallbackQuery):
    orders = await get_user_orders(cb.from_user.id)
    text = "<b>Ваши заказы:</b>" if orders else "Заказов нет."
    kb = kb_orders(orders) if orders else kb_start()
    try:
        await safe_edit(cb.message, text, reply_markup=kb)
    except Exception:
        await safe_answer(cb.message, text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("view:"))
async def view_order(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[1])
    order = await get_order(oid)
    if not order:
        await cb.answer("Не найден", show_alert=True)
        return
    p = config.PRICES.get(order["service_type"], {})
    st = STATUS_NAMES.get(order["status"], order["status"])
    text = f"<b>Заказ #{oid}</b>\n\nУслуга: {p.get('name','?')}\nСтоимость: {p.get('prefix','')}{order['price_byn']} BYN / {p.get('prefix','')}{order['price_rub']} RUB\nСтатус: {st}"
    if order["status"] == "pending_payment":
        text += f"\n\nКарта: <code>{config.PAYMENT_CARD}</code>\nОтправьте скриншот."
        await state.set_state(States.waiting_photo)
        await state.update_data(order_id=oid)
    elif order["status"] in ("payment_confirmed", "in_progress"):
        text += "\n\nПишите — сообщения идут менеджеру."
        await state.set_state(States.chatting)
        await state.update_data(order_id=oid)
    try:
        await cb.message.edit_text(text, reply_markup=kb_back())
    except Exception:
        await cb.message.answer(text, reply_markup=kb_back())
    await cb.answer()


@router.callback_query(F.data == "check_status")
async def ask_oid(cb: CallbackQuery, state: FSMContext):
    try:
        await cb.message.edit_text("Введите номер заказа:")
    except Exception:
        await cb.message.answer("Введите номер заказа:")
    await state.set_state(States.waiting_oid)
    await cb.answer()


@router.message(States.waiting_oid, F.text)
async def process_oid(msg: Message, state: FSMContext):
    try:
        oid = int(msg.text.strip().replace("#", ""))
    except ValueError:
        await msg.answer("Число.")
        return
    order = await get_order(oid)
    if not order or order["user_id"] != msg.from_user.id:
        await safe_answer(msg, "Не найден.", reply_markup=kb_start())
        await state.clear()
        return
    await safe_answer(msg, f"<b>#{oid}</b>\nСтатус: {STATUS_NAMES.get(order['status'], order['status'])}", reply_markup=kb_start())
    await state.clear()


# ФОТО ОПЛАТЫ
@router.message(States.waiting_photo, F.photo)
async def recv_photo(msg: Message, state: FSMContext):
    data = await state.get_data()
    oid = data.get("order_id")
    if not oid:
        p = await get_latest_pending_order(msg.from_user.id)
        oid = p["id"] if p else None
    if not oid:
        await safe_answer(msg, "Нет заказов.", reply_markup=kb_start())
        await state.clear()
        return
    fid = msg.photo[-1].file_id
    await save_payment_photo(oid, fid)
    user = await get_user(msg.from_user.id)
    existing = await get_topic_by_order(oid)
    tid = existing["topic_id"] if existing else None
    if not tid:
        tid = await _create_topic(oid, msg.from_user.id, user["username"] if user else "")
    if config.MANAGER_GROUP_ID and tid:
        try:
            await safe_photo(config.MANAGER_GROUP_ID, fid, caption=f"<b>Фото оплаты #{oid}</b>", reply_markup=kb_admin_pay(oid), message_thread_id=tid)
        except Exception as e:
            log.error(f"photo mgr: {e}")
    await safe_answer(msg, f"<b>Скриншот получен!</b>\nЗаказ #{oid} — ожидайте.", reply_markup=kb_start())
    await state.clear()


# ЦЕНА
@router.callback_query(F.data.startswith("quote:"))
async def quote_start(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[1])
    order = await get_order(oid)
    if not order or order["status"] != "pending_quote":
        await cb.answer("Уже оценён", show_alert=True)
        return
    await state.set_state(States.waiting_price)
    await state.update_data(quote_oid=oid, quote_tid=getattr(cb.message, "message_thread_id", None))
    extra = {"message_thread_id": getattr(cb.message, "message_thread_id", None)} if getattr(cb.message, "message_thread_id", None) else {}
    await cb.message.answer(f"<b>Цена для #{oid}</b>\n\n<code>BYN RUB</code>\nПример: <code>50 1500</code>", **extra)
    await cb.answer()


@router.message(States.waiting_price, F.text)
async def quote_process(msg: Message, state: FSMContext):
    data = await state.get_data()
    oid = data.get("quote_oid")
    tid = data.get("quote_tid")
    if not oid:
        await state.clear()
        return
    if msg.text.strip().lower() in ("отмена", "cancel"):
        await state.clear()
        await msg.answer("Отменено.")
        return
    parts = msg.text.strip().replace(",", " ").replace("/", " ").split()
    if len(parts) != 2:
        await msg.answer("<code>BYN RUB</code>")
        return
    try:
        byn, rub = float(parts[0]), float(parts[1])
    except ValueError:
        await msg.answer("Числа.")
        return
    if byn <= 0 or rub <= 0:
        await msg.answer("> 0")
        return
    await set_order_price(oid, byn, rub)
    order = await get_order(oid)
    sn = config.PRICES.get(order["service_type"], {}).get("name", "?")
    try:
        await bot.send_message(order["user_id"],
            f"<b>Inside PC — Заказ #{oid}</b>\n\nМенеджер рассчитал стоимость:\n<b>{byn} BYN / {rub} RUB</b>\n\n"
            f"<b>Реквизиты:</b>\nБанк: {config.PAYMENT_BANK}\nКарта: <code>{config.PAYMENT_CARD}</code>\nПолучатель: {config.PAYMENT_HOLDER}\n\nПереведите и нажмите кнопку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Загрузить скриншот", url=f"https://t.me/{config.BOT_USERNAME}?start=pay_{oid}")],
            ]))
    except Exception as e:
        log.error(f"quote user: {e}")
    extra = {"message_thread_id": tid} if tid else {}
    try:
        await safe_send(config.MANAGER_GROUP_ID, f"<b>Цена #{oid}:</b> {byn}/{rub}\nКлиент уведомлён.", reply_markup=kb_admin_manage(oid), **extra)
    except Exception as e:
        log.error(f"quote grp: {e}")
    await msg.answer(f"Цена назначена для #{oid}.")
    await state.clear()


# ============================================================
#  ПОРТФОЛИО — ХЭНДЛЕРЫ
# ============================================================

@router.callback_query(F.data == "pf:new")
async def pf_new(cb: CallbackQuery, state: FSMContext):
    pid = await add_portfolio_item()
    await state.set_state(States.pf_title)
    await state.update_data(pf_id=pid)
    await cb.message.answer(f"<b>Новая работа #{pid}</b>\n\nВведите название:")
    await cb.answer()


@router.callback_query(F.data == "pf:list")
async def pf_list(cb: CallbackQuery):
    items = await get_portfolio_all()
    if not items:
        await cb.message.answer("Портфолио пусто. Нажмите <b>Добавить работу</b>.", reply_markup=kb_pf_manage())
        await cb.answer()
        return
    rows = []
    for item in items:
        title = item["title"] or f"Без названия #{item['id']}"
        try:
            pc = len(json.loads(item["photo_ids"]))
        except Exception:
            pc = 0
        rows.append([InlineKeyboardButton(text=f"#{item['id']} | {title} | {pc} фото", callback_data=f"pf:edit:{item['id']}")])
    rows.append([InlineKeyboardButton(text="Добавить работу", callback_data="pf:new", **S("success"))])
    await cb.message.answer("<b>Портфолио:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("pf:edit:"))
async def pf_edit(cb: CallbackQuery):
    pid = int(cb.data.split(":")[2])
    item = await get_portfolio_item(pid)
    if not item:
        await cb.answer("Не найдено", show_alert=True)
        return
    try:
        photos = json.loads(item["photo_ids"])
    except Exception:
        photos = []
    text = (
        f"<b>Работа #{pid}</b>\n\n"
        f"Название: {item['title'] or '—'}\n"
        f"Характеристики: {item['specs'] or '—'}\n"
        f"Цена: {item['price_byn']} BYN / {item['price_rub']} RUB\n"
        f"Описание: {item['description'] or '—'}\n"
        f"Фото: {len(photos)} шт."
    )
    await cb.message.answer(text, reply_markup=kb_pf_item(pid))
    await cb.answer()


@router.callback_query(F.data.startswith("pf:title:"))
async def pf_set_title(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.set_state(States.pf_title)
    await state.update_data(pf_id=pid)
    await cb.message.answer("Введите название:")
    await cb.answer()


@router.message(States.pf_title, F.text)
async def pf_title_input(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = data["pf_id"]
    await update_portfolio(pid, title=msg.text.strip())
    await msg.answer(f"Название обновлено.", reply_markup=kb_pf_item(pid))
    await state.clear()


@router.callback_query(F.data.startswith("pf:specs:"))
async def pf_set_specs(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.set_state(States.pf_specs)
    await state.update_data(pf_id=pid)
    await cb.message.answer("Введите характеристики:")
    await cb.answer()


@router.message(States.pf_specs, F.text)
async def pf_specs_input(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = data["pf_id"]
    await update_portfolio(pid, specs=msg.text.strip())
    await msg.answer("Характеристики обновлены.", reply_markup=kb_pf_item(pid))
    await state.clear()


@router.callback_query(F.data.startswith("pf:price:"))
async def pf_set_price(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.set_state(States.pf_price)
    await state.update_data(pf_id=pid)
    await cb.message.answer("Цена: <code>BYN RUB</code>\nПример: <code>3000 80000</code>")
    await cb.answer()


@router.message(States.pf_price, F.text)
async def pf_price_input(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = data["pf_id"]
    parts = msg.text.strip().replace(",", " ").replace("/", " ").split()
    if len(parts) != 2:
        await msg.answer("<code>BYN RUB</code>")
        return
    try:
        byn, rub = float(parts[0]), float(parts[1])
    except ValueError:
        await msg.answer("Числа.")
        return
    await update_portfolio(pid, price_byn=byn, price_rub=rub)
    await msg.answer("Цена обновлена.", reply_markup=kb_pf_item(pid))
    await state.clear()


@router.callback_query(F.data.startswith("pf:desc:"))
async def pf_set_desc(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.set_state(States.pf_desc)
    await state.update_data(pf_id=pid)
    await cb.message.answer("Введите описание:")
    await cb.answer()


@router.message(States.pf_desc, F.text)
async def pf_desc_input(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = data["pf_id"]
    await update_portfolio(pid, description=msg.text.strip())
    await msg.answer("Описание обновлено.", reply_markup=kb_pf_item(pid))
    await state.clear()


@router.callback_query(F.data.startswith("pf:photo:"))
async def pf_add_photo(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.set_state(States.pf_photo)
    await state.update_data(pf_id=pid)
    await cb.message.answer("Отправьте фото (можно несколько). Когда закончите — напишите <b>готово</b>.")
    await cb.answer()


@router.message(States.pf_photo, F.photo)
async def pf_photo_input(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = data["pf_id"]
    fid = msg.photo[-1].file_id
    await add_portfolio_photo(pid, fid)
    item = await get_portfolio_item(pid)
    try:
        cnt = len(json.loads(item["photo_ids"]))
    except Exception:
        cnt = 0
    await msg.answer(f"Фото добавлено. Всего: {cnt}. Ещё или <b>готово</b>.")


@router.message(States.pf_photo, F.text)
async def pf_photo_done(msg: Message, state: FSMContext):
    if msg.text.strip().lower() in ("готово", "done", "стоп"):
        data = await state.get_data()
        pid = data["pf_id"]
        await state.clear()
        await msg.answer("Фото сохранены.", reply_markup=kb_pf_item(pid))
    else:
        await msg.answer("Отправьте фото или напишите <b>готово</b>.")


@router.callback_query(F.data.startswith("pf:del:"))
async def pf_delete(cb: CallbackQuery):
    pid = int(cb.data.split(":")[2])
    await delete_portfolio(pid)
    await cb.message.answer(f"Работа #{pid} удалена.", reply_markup=kb_pf_manage())
    await cb.answer()


# ============================================================
#  ЧАТ / АВТО-РЕЛЕ / МЕНЕДЖЕР
# ============================================================

@router.message(States.chatting, F.chat.type == "private")
async def chat_any(msg: Message, state: FSMContext):
    data = await state.get_data()
    oid = data.get("order_id")
    if oid and await relay_to_topic(msg, oid):
        await msg.answer("Отправлено.")


@router.message(F.chat.type == "private", ~F.text.startswith("/"))
async def auto_relay(msg: Message, state: FSMContext):
    if await state.get_state():
        return
    oid = await get_active_order(msg.from_user.id)
    if oid:
        await relay_to_topic(msg, oid)


@router.message(F.message_thread_id)
async def mgr_any(msg: Message):
    if msg.chat.id != config.MANAGER_GROUP_ID or msg.from_user.is_bot:
        return
    link = await get_topic_link(msg.message_thread_id)
    if link:
        await relay_to_user(msg, link["user_id"])


# ОПЛАТА / СТАТУСЫ
@router.callback_query(F.data.startswith("cpay:"))
async def confirm_pay(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    await update_status(oid, "payment_confirmed")
    order = await get_order(oid)
    try:
        await bot.send_message(order["user_id"], f"<b>Оплата #{oid} подтверждена!</b>")
    except Exception:
        pass
    try:
        await cb.message.edit_caption(caption=f"<b>#{oid} — ПОДТВЕРЖДЕНО</b>")
    except Exception:
        try:
            await cb.message.edit_text(f"<b>#{oid} — ПОДТВЕРЖДЕНО</b>")
        except Exception:
            pass
    await cb.answer("OK")


@router.callback_query(F.data.startswith("rpay:"))
async def reject_pay(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    await update_status(oid, "pending_payment")
    order = await get_order(oid)
    try:
        await bot.send_message(order["user_id"], f"<b>Оплата #{oid} отклонена.</b>\nПроверьте реквизиты.")
    except Exception:
        pass
    try:
        await cb.message.edit_caption(caption=f"<b>#{oid} — ОТКЛОНЕНО</b>")
    except Exception:
        try:
            await cb.message.edit_text(f"<b>#{oid} — ОТКЛОНЕНО</b>")
        except Exception:
            pass
    await cb.answer("OK")


@router.callback_query(F.data.startswith("ss:"))
async def set_status(cb: CallbackQuery):
    parts = cb.data.split(":")
    oid, ns = int(parts[1]), parts[2]
    await update_status(oid, ns)
    order = await get_order(oid)
    st = STATUS_NAMES.get(ns, ns)
    if ns == "in_progress":
        await set_active_order(order["user_id"], oid)
        try:
            await bot.send_message(order["user_id"], f"<b>Заказ #{oid} в работе!</b>\nВсе сообщения идут менеджеру.\n/stop — выйти.")
        except Exception:
            pass
    elif ns in ("completed", "cancelled"):
        await set_active_order(order["user_id"], 0)
        try:
            await bot.send_message(order["user_id"], f"<b>Заказ #{oid}</b>\nСтатус: {st}")
        except Exception:
            pass
    tid = getattr(cb.message, "message_thread_id", None)
    extra = {"message_thread_id": tid} if tid else {}
    try:
        await safe_send(config.MANAGER_GROUP_ID, f"#{oid}: {st}", reply_markup=kb_admin_manage(oid), **extra)
    except Exception:
        pass
    await cb.answer(st)