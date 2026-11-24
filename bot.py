import asyncio
import logging
import os
import time

import aiohttp
import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

load_dotenv()  # lokal .env faylni o'qiydi (Railway'da Variables ishlatiladi)
logging.basicConfig(level=logging.INFO)

# ================== SOZLAMALAR (Railway Variables orqali) ==================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])          # sizning Telegram ID'ingiz
TON_WALLET = os.getenv("TON_WALLET", "")         # TON hamyon manzilingiz
CARD_NUMBER = os.getenv("CARD_NUMBER", "0000 0000 0000 0000")
CARD_OWNER = os.getenv("CARD_OWNER", "Ism Familiya")
TONCENTER_KEY = os.getenv("TONCENTER_KEY", "")   # ixtiyoriy, limitni oshiradi
DB_PATH = os.getenv("DB_PATH", "orders.db")

# Narxlarni o'zingiz o'zgartiring (bu namuna narxlar!)
PACKAGES = {
    50: {"uzs": 12000, "ton": 0.40},
    100: {"uzs": 23000, "ton": 0.80},
    250: {"uzs": 57000, "ton": 2.00},
    500: {"uzs": 112000, "ton": 4.00},
    1000: {"uzs": 220000, "ton": 8.00},
}
# ===========================================================================


class Flow(StatesGroup):
    username = State()
    receipt = State()


# ---------------------------- Baza ----------------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                target TEXT,
                stars INTEGER,
                method TEXT,
                uzs INTEGER,
                ton REAL,
                status TEXT,
                created INTEGER
            )"""
        )
        await db.commit()


async def create_order(user_id, target, stars, method):
    p = PACKAGES[stars]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO orders (user_id,target,stars,method,uzs,ton,status,created)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (user_id, target, stars, method, p["uzs"], p["ton"], "pending", int(time.time())),
        )
        await db.commit()
        return cur.lastrowid


async def get_order(oid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE id=?", (oid,))
        return await cur.fetchone()


async def set_status(oid, new, allowed):
    marks = ",".join("?" * len(allowed))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE orders SET status=? WHERE id=? AND status IN ({marks})",
            (new, oid, *allowed),
        )
        await db.commit()
        return cur.rowcount > 0


# ---------------------------- Bot ----------------------------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


def packages_kb():
    rows = [
        [InlineKeyboardButton(
            text=f"⭐ {s} Stars — {p['uzs']:,} so'm / {p['ton']} TON".replace(",", " "),
            callback_data=f"pkg:{s}",
        )]
        for s, p in PACKAGES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Salom! ⭐ Telegram Stars sotib olish uchun paketni tanlang:",
                   reply_markup=packages_kb())


@dp.callback_query(F.data.startswith("pkg:"))
async def choose_pkg(c: CallbackQuery, state: FSMContext):
    stars = int(c.data.split(":")[1])
    await state.update_data(stars=stars)
    await state.set_state(Flow.username)
    await c.message.answer("Stars qaysi akkauntga yuborilsin?\n"
                           "Username'ni yozing (masalan: @username):")
    await c.answer()


@dp.message(Flow.username, F.text)
async def got_username(m: Message, state: FSMContext):
    target = m.text.strip().lstrip("@")
    if not target or " " in target or len(target) < 4:
        await m.answer("Username noto'g'ri. Qaytadan yozing (masalan: @username):")
        return
    await state.update_data(target=target)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Karta (so'm)", callback_data="pay:card")],
        [InlineKeyboardButton(text="💎 TON", callback_data="pay:ton")],
    ])
    await m.answer("To'lov usulini tanlang:", reply_markup=kb)


@dp.callback_query(F.data.startswith("pay:"))
async def choose_pay(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "stars" not in data or "target" not in data:
        await c.message.answer("Avval /start bosing.")
        await c.answer()
        return
    method = c.data.split(":")[1]
    oid = await create_order(c.from_user.id, data["target"], data["stars"], method)
    p = PACKAGES[data["stars"]]

    if method == "card":
        await state.set_state(Flow.receipt)
        await state.update_data(order_id=oid)
        await c.message.answer(
            f"🧾 Buyurtma <b>#{oid}</b>\n"
            f"⭐ {data['stars']} Stars → @{data['target']}\n\n"
            f"Karta: <code>{CARD_NUMBER}</code>\n"
            f"Egasi: {CARD_OWNER}\n"
            f"Summa: <b>{p['uzs']:,} so'm</b>\n\n"
            "To'lovdan keyin <b>chek rasmini</b> shu yerga yuboring.".replace(",", " ")
        )
    else:
        await state.clear()
        await c.message.answer(
            f"🧾 Buyurtma <b>#{oid}</b>\n"
            f"⭐ {data['stars']} Stars → @{data['target']}\n\n"
            f"Hamyon:\n<code>{TON_WALLET}</code>\n"
            f"Summa: <b>{p['ton']} TON</b>\n"
            f"Izoh (comment) — <b>majburiy</b>:\n<code>ORD{oid}</code>\n\n"
            "Izohni aynan shunday yozing, to'lov avtomatik aniqlanadi (1-2 daqiqa)."
        )
    await c.answer()


@dp.message(Flow.receipt, F.photo)
async def got_receipt(m: Message, state: FSMContext):
    data = await state.get_data()
    oid = data["order_id"]
    o = await get_order(oid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"adm:ok:{oid}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"adm:no:{oid}"),
    ]])
    await bot.send_photo(
        ADMIN_ID, m.photo[-1].file_id,
        caption=f"💳 Chek — buyurtma #{oid}\n⭐ {o['stars']} → @{o['target']}\n"
                f"Summa: {o['uzs']} so'm\nFoydalanuvchi: {m.from_user.id}",
        reply_markup=kb,
    )
    await set_status(oid, "review", ["pending"])
    await state.clear()
    await m.answer("Chek adminga yuborildi. Tasdiqlangach xabar beramiz ✅")


@dp.message(Flow.receipt)
async def need_photo(m: Message):
    await m.answer("Iltimos, chek <b>rasmini</b> yuboring.")


async def on_paid(oid):
    """To'lov tasdiqlanganda chaqiriladi. Keyinroq shu yerga avtomatik yetkazishni ulaymiz."""
    o = await get_order(oid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📦 Yuborildi", callback_data=f"adm:done:{oid}")
    ]])
    await bot.send_message(
        ADMIN_ID,
        f"💰 To'landi — buyurtma #{oid}\nYubor: ⭐ {o['stars']} → @{o['target']}",
        reply_markup=kb,
    )
    await bot.send_message(o["user_id"],
                           f"✅ To'lov qabul qilindi (#{oid}). Stars tez orada yuboriladi.")


def is_admin(c: CallbackQuery):
    return c.from_user.id == ADMIN_ID


@dp.callback_query(F.data.startswith("adm:"))
async def admin_actions(c: CallbackQuery):
    if not is_admin(c):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    _, action, oid = c.data.split(":")
    oid = int(oid)
    o = await get_order(oid)
    if action == "ok":
        if await set_status(oid, "paid", ["pending", "review"]):
            await on_paid(oid)
            await c.message.edit_caption(caption=(c.message.caption or "") + "\n\n✅ Tasdiqlandi")
    elif action == "no":
        if await set_status(oid, "rejected", ["pending", "review"]):
            await bot.send_message(o["user_id"], f"❌ Buyurtma #{oid} rad etildi. Savol bo'lsa adminga yozing.")
            await c.message.edit_caption(caption=(c.message.caption or "") + "\n\n❌ Rad etildi")
    elif action == "done":
        if await set_status(oid, "delivered", ["paid"]):
            await bot.send_message(o["user_id"], f"🎉 Buyurtma #{oid}: ⭐ {o['stars']} Stars yuborildi!")
            await c.message.edit_text((c.message.text or "") + "\n\n📦 Yuborildi")
    await c.answer()


# ---------------------------- TON to'lovni avtomatik tekshirish ----------------------------
async def ton_watcher():
    url = "https://toncenter.com/api/v2/getTransactions"
    params = {"address": TON_WALLET, "limit": 30}
    headers = {"X-API-Key": TONCENTER_KEY} if TONCENTER_KEY else {}
    async with aiohttp.ClientSession(headers=headers) as s:
        while True:
            try:
                async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json()
                for tx in data.get("result", []):
                    msg = tx.get("in_msg") or {}
                    text = (msg.get("message") or "").strip()
                    if not text.startswith("ORD"):
                        continue
                    try:
                        oid = int(text[3:])
                    except ValueError:
                        continue
                    o = await get_order(oid)
                    if not o or o["method"] != "ton":
                        continue
                    value = int(msg.get("value", 0)) / 1e9
                    if value >= o["ton"] * 0.99:
                        if await set_status(oid, "paid", ["pending"]):
                            await on_paid(oid)
            except Exception as e:
                logging.warning("ton_watcher xatosi: %s", e)
            await asyncio.sleep(20)


async def main():
    await init_db()
    if TON_WALLET:
        asyncio.create_task(ton_watcher())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
