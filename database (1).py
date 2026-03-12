"""
Inside PC — SQLite.
+ Портфолио
"""
import aiosqlite
import json
from config import DATABASE_PATH


async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                active_order INTEGER DEFAULT 0
            )
        """)
        for col, default in [("active_order", "0")]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT {default}")
                await db.commit()
            except Exception:
                pass

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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT DEFAULT '',
                description TEXT DEFAULT '',
                specs TEXT DEFAULT '',
                price_byn REAL DEFAULT 0,
                price_rub REAL DEFAULT 0,
                photo_ids TEXT DEFAULT '[]',
                category TEXT DEFAULT '',
                is_visible INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()


# ============================================================
#  USERS
# ============================================================

async def upsert_user(uid, username, full_name):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=?, full_name=?
        """, (uid, username, full_name, username, full_name))
        await db.commit()


async def get_user(uid):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_active_order(uid, oid):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE users SET active_order=? WHERE user_id=?", (oid, uid))
        await db.commit()


async def get_active_order(uid):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("SELECT active_order FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


# ============================================================
#  ORDERS
# ============================================================

async def create_order(uid, service, has_parts, parts, desc, byn, rub, status="pending_payment"):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "INSERT INTO orders (user_id, service_type, has_parts, parts_data, "
            "description, price_byn, price_rub, status) VALUES (?,?,?,?,?,?,?,?)",
            (uid, service, int(has_parts),
             json.dumps(parts, ensure_ascii=False) if parts else None,
             desc, byn, rub, status),
        )
        await db.commit()
        return cur.lastrowid


async def get_order(oid):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE id=?", (oid,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_orders(uid):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC", (uid,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_latest_pending_order(uid):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='pending_payment' "
            "ORDER BY created_at DESC LIMIT 1", (uid,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_status(oid, status):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
        await db.commit()


async def set_order_price(order_id, price_byn, price_rub):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE orders SET price_byn=?, price_rub=?, status='pending_payment' WHERE id=?",
            (price_byn, price_rub, order_id),
        )
        await db.commit()


async def save_payment_photo(oid, file_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE orders SET payment_photo=? WHERE id=?", (file_id, oid))
        await db.commit()


# ============================================================
#  TOPICS
# ============================================================

async def save_topic(topic_id, order_id, user_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO topic_links VALUES (?,?,?)", (topic_id, order_id, user_id))
        await db.execute("UPDATE orders SET topic_id=? WHERE id=?", (topic_id, order_id))
        await db.commit()


async def get_topic_link(topic_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM topic_links WHERE topic_id=?", (topic_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_topic_by_order(oid):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM topic_links WHERE order_id=?", (oid,))
        row = await cur.fetchone()
        return dict(row) if row else None


# ============================================================
#  PORTFOLIO
# ============================================================

async def add_portfolio_item(title="", description="", specs="", price_byn=0, price_rub=0, category=""):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "INSERT INTO portfolio (title, description, specs, price_byn, price_rub, category) "
            "VALUES (?,?,?,?,?,?)",
            (title, description, specs, price_byn, price_rub, category),
        )
        await db.commit()
        return cur.lastrowid


async def get_portfolio_item(pid):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM portfolio WHERE id=?", (pid,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_portfolio_all():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM portfolio WHERE is_visible=1 ORDER BY created_at DESC")
        return [dict(r) for r in await cur.fetchall()]


async def update_portfolio(pid, **fields):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for k, v in fields.items():
            await db.execute(f"UPDATE portfolio SET {k}=? WHERE id=?", (v, pid))
        await db.commit()


async def delete_portfolio(pid):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM portfolio WHERE id=?", (pid,))
        await db.commit()


async def add_portfolio_photo(pid, file_id):
    item = await get_portfolio_item(pid)
    if not item:
        return
    try:
        photos = json.loads(item["photo_ids"])
    except Exception:
        photos = []
    photos.append(file_id)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE portfolio SET photo_ids=? WHERE id=?", (json.dumps(photos), pid))
        await db.commit()


async def remove_portfolio_photo(pid, index):
    item = await get_portfolio_item(pid)
    if not item:
        return
    try:
        photos = json.loads(item["photo_ids"])
    except Exception:
        photos = []
    if 0 <= index < len(photos):
        photos.pop(index)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE portfolio SET photo_ids=? WHERE id=?", (json.dumps(photos), pid))
        await db.commit()


# ============================================================
#  СТАТУСЫ
# ============================================================

STATUS_NAMES = {
    "pending_quote": "Ожидает оценки",
    "pending_payment": "Ожидает оплаты",
    "payment_confirmed": "Оплата подтверждена",
    "in_progress": "В работе",
    "completed": "Завершён",
    "cancelled": "Отменён",
}