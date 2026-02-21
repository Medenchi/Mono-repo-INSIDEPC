"""
Inside PC — вся работа с SQLite.
"""
import aiosqlite
import json
from config import DATABASE_PATH


async def init_db():
    """Создаёт таблицы при первом запуске."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                service_type TEXT NOT NULL,
                has_parts INTEGER DEFAULT 0,
                parts_data TEXT,
                description TEXT,
                status TEXT DEFAULT 'pending_payment',
                payment_photo TEXT,
                topic_id INTEGER,
                price_byn REAL,
                price_rub REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS topic_links (
                topic_id INTEGER PRIMARY KEY,
                order_id INTEGER,
                user_id INTEGER
            )
        """)
        await db.commit()


async def upsert_user(uid, username, full_name):
    """Сохраняет или обновляет пользователя."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO users VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=?, full_name=?",
            (uid, username, full_name, username, full_name)
        )
        await db.commit()


async def create_order(uid, service, has_parts, parts, desc, byn, rub):
    """Создаёт заказ, возвращает его ID."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "INSERT INTO orders (user_id,service_type,has_parts,parts_data,description,price_byn,price_rub) VALUES (?,?,?,?,?,?,?)",
            (uid, service, int(has_parts), json.dumps(parts, ensure_ascii=False) if parts else None, desc, byn, rub)
        )
        await db.commit()
        return cur.lastrowid


async def get_order(oid):
    """Получает заказ по ID."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE id=?", (oid,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_orders(uid):
    """Все заказы пользователя."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC", (uid,))
        return [dict(r) for r in await cur.fetchall()]


async def update_status(oid, status):
    """Обновляет статус заказа."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
        await db.commit()


async def save_payment_photo(oid, file_id):
    """Сохраняет file_id фото оплаты."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE orders SET payment_photo=?, status='payment_uploaded' WHERE id=?", (file_id, oid))
        await db.commit()


async def save_topic(topic_id, order_id, user_id):
    """Связывает топик группы с заказом."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO topic_links VALUES (?,?,?)", (topic_id, order_id, user_id))
        await db.execute("UPDATE orders SET topic_id=? WHERE id=?", (topic_id, order_id))
        await db.commit()


async def get_topic_link(topic_id):
    """Кто привязан к топику."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM topic_links WHERE topic_id=?", (topic_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_topic_by_order(oid):
    """Топик по заказу."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM topic_links WHERE order_id=?", (oid,))
        row = await cur.fetchone()
        return dict(row) if row else None


# --- Хелперы ---
STATUS_NAMES = {
    "pending_payment": "Ожидает оплаты",
    "payment_uploaded": "Фото загружено, проверяем",
    "payment_confirmed": "Оплата подтверждена",
    "in_progress": "В работе",
    "completed": "Завершён",
    "cancelled": "Отменён",
}
