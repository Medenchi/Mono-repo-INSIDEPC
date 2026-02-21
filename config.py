"""
Конфигурация Inside PC — все настройки в одном месте.
"""
import os

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "СЮДА_ТОКЕН_БОТА")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
MANAGER_GROUP_ID = int(os.getenv("MANAGER_GROUP_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://yourdomain.com/web")

# API
API_HOST = "0.0.0.0"
API_PORT = 8080

# БД
DATABASE_PATH = "insidepc.db"

# Реквизиты оплаты
PAYMENT_CARD = "1234 5678 9012 3456"
PAYMENT_HOLDER = "IVANOV IVAN"
PAYMENT_BANK = "Belarusbank"

# Цены
PRICES = {
    "consultation": {"name": "Консультация / Оценка сборки", "byn": 10, "rub": 270},
    "build": {"name": "Сборка ПК", "byn": 50, "rub": 1500},
    "upgrade": {"name": "Апгрейд ПК", "byn": 30, "rub": 900},
}

# ID кастомных эмодзи (замени на свои через @Stickers)
E = {
    "pc": "5368324170671202286",
    "tools": "5377637553811258641",
    "ok": "5382322526934102736",
    "bell": "5368324170671202286",
    "doc": "5377637553811258641",
    "money": "5368324170671202286",
    "user": "5377637553811258641",
}
