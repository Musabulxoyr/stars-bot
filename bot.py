import asyncio
import logging
import os
import re
import time

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CARD_NUMBER = os.getenv("CARD_NUMBER", "0000 0000 0000 0000")
CARD_OWNER = os.getenv("CARD_OWNER", "Ism Familiya")
PRICE_PER_STAR = float(os.getenv("PRICE_PER_STAR", "200"))  # so'm, o'zingizning marjangiz bilan
DB_PATH = os.getenv("DB_PATH", "orders.db")

PACKS = [50, 75, 100, 150, 250, 350, 500, 750, 1000, 1500, 2500, 5000, 10000]
MIN_STARS, MAX_STARS = 50, 10000  # Fragment minimumi 50 Stars

# Gifts: key -> (emoji, nomi, Telegram'dagi Stars narxi, sotuv narxi so'mda)
GIFTS = {
    "heart":    ("💝", "Yurak",   15,  3000),
    "bear":     ("🧸", "Ayiqcha", 15,  3000),
    "box":      ("🎁", "Sovg'a",  25,  5000),
    "rose":     ("🌹", "Atirgul", 25,  5000),
    "cake":     ("🎂", "Tort",    50, 10000),
    "bouquet":  ("💐", "Guldasta", 50, 10000),
    "rocket":   ("🚀", "Raketa",  50, 10000),
    "champ":    ("🍾", "Shampan", 50, 10000),
    "trophy":   ("🏆", "Kubok",  100, 20000),
    "ring":     ("💍", "Uzuk",   100, 20000),
    "diamond":  ("💎", "Olmos",  100, 20000),
}

router = Router()


class Order(StatesGroup):
    custom_amount = State()
    recipient = State()
    receipt = State()


def price_for(stars: int) -> int:
    return int(round(stars * PRICE_PER_STAR, -2))  # 100 so'mgacha yaxlitlanadi


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ---------- DB ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                recipient TEXT NOT NULL,
                stars INTEGER NOT NULL,
                price INTEGER NOT NULL,
                status TEXT NOT NULL,
                receipt_uid TEXT UNIQUE,
                created_at INTEGER NOT NULL
            )"""
        )
        cur = await db.execute("PRAGMA table_info(orders)")
        cols = [r[1] for r in await cur.fetchall()]
        if "item" not in cols:
            await db.execute("ALTER TABLE orders ADD COLUMN item TEXT NOT NULL DEFAULT 'stars'")
        await db.commit()


def describe(o) -> str:
    """Buyurtma nima ekanini matn qilib beradi (Stars yoki Gift)."""
    if o["item"] == "stars":
        return f"{o['stars']}⭐"
    emoji, name, cost, _ = GIFTS[o["item"]]
    return f"{emoji} {name} gifti"


async def create_order(user_id, recipient, stars, price, item="stars") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO orders (user_id, recipient, stars, price, status, created_at, item) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (user_id, recipient, stars, price, int(time.time()), item),
        )
        await db.commit()
        return cur.lastrowid


async def get_order(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        return await cur.fetchone()


async def set_status(order_id, new, expected) -> bool:
    """Faqat kutilgan holatdan o'tkazadi: ikki marta bosishdan himoya."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE orders SET status=? WHERE id=? AND status=?",
            (new, order_id, expected),
        )
        await db.commit()
        return cur.rowcount == 1


async def attach_receipt(order_id, uid) -> bool:
    """Bir xil chekni ikki buyurtmada ishlatishni bloklaydi."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cur = await db.execute(
                "UPDATE orders SET receipt_uid=?, status='receipt_sent' "
                "WHERE id=? AND status='pending'",
                (uid, order_id),
            )
            await db.commit()
            return cur.rowcount == 1
        except aiosqlite.IntegrityError:
            return False


# ---------- Foydalanuvchi oqimi ----------
def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Stars sotib olish", callback_data="menu:stars")
    kb.button(text="🎁 Gifts", callback_data="menu:gifts")
    kb.adjust(1)
    return kb.as_markup()


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Xush kelibsiz! Kerakli bo'limni tanlang 👇", reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu:main")
async def menu_main(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Xush kelibsiz! Kerakli bo'limni tanlang 👇", reply_markup=main_menu_kb())
    await c.answer()


@router.callback_query(F.data == "menu:stars")
async def menu_stars(c: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    for n in PACKS:
        kb.button(text=f"⭐ {n} — {fmt(price_for(n))} so'm", callback_data=f"amt:{n}")
    kb.button(text="✏️ Boshqa miqdor", callback_data="amt:other")
    kb.button(text="⬅️ Orqaga", callback_data="menu:main")
    kb.adjust(2)
    await c.message.answer(
        "⭐ Telegram Stars — arzon va qulay!\n\n"
        "✅ Hamyonbop narx\n"
        "✅ Tez yetkazish\n"
        "✅ Karta orqali oson to'lov\n\n"
        f"📌 Minimal: {MIN_STARS} ⭐\n"
        f"📌 Maksimal: {fmt(MAX_STARS)} ⭐\n"
        f"💰 1 ⭐ = {int(PRICE_PER_STAR)} so'm\n\n"
        "Kerakli miqdorni tanlang 👇",
        reply_markup=kb.as_markup(),
    )
    await c.answer()


# ---------- Gifts ----------
@router.callback_query(F.data == "menu:gifts")
async def menu_gifts(c: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    for key, (emoji, _name, _cost, price) in GIFTS.items():
        kb.button(text=f"{emoji} — {fmt(price)} so'm", callback_data=f"gift:{key}")
    kb.button(text="⬅️ Orqaga", callback_data="menu:main")
    kb.adjust(*([2] * (len(GIFTS) // 2)), *([1] * (len(GIFTS) % 2)), 1)
    await c.message.answer(
        "🎁 Telegram Gift xarid qilish\n\n"
        "ℹ️ Giftni yuborish jarayoni admin profili orqali anonim amalga oshiriladi.\n\n"
        "Iltimos, yuborish uchun kerakli Giftni tanlang 👇",
        reply_markup=kb.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data.startswith("gift:"))
async def choose_gift(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[1]
    if key not in GIFTS:
        return await c.answer()
    emoji, name, _cost, price = GIFTS[key]
    await state.update_data(gift=key, stars=None)
    await state.set_state(Order.recipient)
    await c.message.answer(
        f"{emoji} {name} — {fmt(price)} so'm\n\n"
        "Gift qaysi akkauntga yuborilsin? Username yozing (masalan @user):"
    )
    await c.answer()


@router.callback_query(F.data.startswith("amt:"))
async def choose_amount(c: CallbackQuery, state: FSMContext):
    val = c.data.split(":")[1]
    if val == "other":
        await state.set_state(Order.custom_amount)
        await c.message.answer(f"Miqdorni yozing ({MIN_STARS}–{MAX_STARS}):")
    else:
        await state.update_data(stars=int(val), gift=None)
        await state.set_state(Order.recipient)
        await c.message.answer("Stars qaysi akkauntga yuborilsin? Username yozing (masalan @user):")
    await c.answer()


@router.message(Order.custom_amount)
async def custom_amount(m: Message, state: FSMContext):
    if not (m.text and m.text.isdigit() and MIN_STARS <= int(m.text) <= MAX_STARS):
        return await m.answer(f"{MIN_STARS} dan {MAX_STARS} gacha butun son yozing.")
    await state.update_data(stars=int(m.text), gift=None)
    await state.set_state(Order.recipient)
    await m.answer("Stars qaysi akkauntga yuborilsin? Username yozing (masalan @user):")


@router.message(Order.recipient)
async def recipient(m: Message, state: FSMContext):
    username = (m.text or "").strip()
    if not re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", username):
        return await m.answer("Username noto'g'ri. Masalan: @user")
    username = "@" + username.lstrip("@")
    data = await state.get_data()
    gift = data.get("gift")
    if gift:
        _emoji, _name, cost, price = GIFTS[gift]
        order_id = await create_order(m.from_user.id, username, cost, price, gift)
    else:
        stars = data["stars"]
        price = price_for(stars)
        order_id = await create_order(m.from_user.id, username, stars, price)
    o = await get_order(order_id)
    await state.update_data(order_id=order_id)
    await state.set_state(Order.receipt)
    await m.answer(
        f"Buyurtma #{order_id}\n"
        f"{describe(o)} → {username}\n"
        f"💰 To'lov: {fmt(price)} so'm\n\n"
        f"Karta: {CARD_NUMBER}\n{CARD_OWNER}\n\n"
        "To'lovdan keyin chek rasmini (screenshot) shu yerga yuboring."
    )


@router.message(Order.receipt, F.photo)
async def receipt(m: Message, state: FSMContext, bot: Bot):
    order_id = (await state.get_data())["order_id"]
    photo = m.photo[-1]
    if not await attach_receipt(order_id, photo.file_unique_id):
        return await m.answer("Bu chek avval ishlatilgan yoki buyurtma allaqachon yuborilgan.")
    o = await get_order(order_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ To'lov keldi", callback_data=f"ok:{order_id}")
    kb.button(text="❌ Rad etish", callback_data=f"no:{order_id}")
    await bot.send_photo(
        ADMIN_ID,
        photo.file_id,
        caption=f"#{order_id} | {describe(o)} → {o['recipient']}\n"
        f"Summa: {fmt(o['price'])} so'm\nFoydalanuvchi: {m.from_user.id}",
        reply_markup=kb.as_markup(),
    )
    await state.clear()
    await m.answer(
        "✅ Chek qabul qilindi!\n\n"
        f"Buyurtma #{order_id}: {describe(o)} → {o['recipient']}\n"
        f"💰 {fmt(o['price'])} so'm\n\n"
        "Admin chekni qo'lda tekshiradi, shuning uchun biroz kutib turing ⏳\n"
        "Tasdiqlangach Stars yuboriladi va sizga xabar keladi."
    )


@router.message(Order.receipt)
async def receipt_wrong(m: Message):
    await m.answer("Iltimos, chek rasmini yuboring.")


# ---------- Admin oqimi ----------
def admin_only(c: CallbackQuery) -> bool:
    return c.from_user.id == ADMIN_ID


@router.callback_query(F.data.startswith("ok:"))
async def approve(c: CallbackQuery, bot: Bot):
    if not admin_only(c):
        return await c.answer()
    order_id = int(c.data.split(":")[1])
    if not await set_status(order_id, "paid", "receipt_sent"):
        return await c.answer("Allaqachon ishlangan", show_alert=True)
    o = await get_order(order_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Yetkazildi", callback_data=f"dl:{order_id}")
    kb.button(text="⚠️ Xato", callback_data=f"fl:{order_id}")
    await c.message.edit_caption(
        caption=f"#{order_id} TO'LANDI\n{describe(o)} ni {o['recipient']} ga yuboring, "
        "keyin «Yetkazildi» ni bosing.",
        reply_markup=kb.as_markup(),
    )
    await bot.send_message(
        o["user_id"],
        f"💳 Buyurtma #{order_id}: to'lovingiz tasdiqlandi!\n"
        f"{describe(o)} tez orada {o['recipient']} ga yuboriladi.",
    )
    await c.answer()


@router.callback_query(F.data.startswith("no:"))
async def reject(c: CallbackQuery, bot: Bot):
    if not admin_only(c):
        return await c.answer()
    order_id = int(c.data.split(":")[1])
    if not await set_status(order_id, "rejected", "receipt_sent"):
        return await c.answer("Allaqachon ishlangan", show_alert=True)
    o = await get_order(order_id)
    await c.message.edit_caption(caption=f"#{order_id} RAD ETILDI")
    await bot.send_message(o["user_id"], f"Buyurtma #{order_id} uchun to'lov tasdiqlanmadi. Admin bilan bog'laning.")
    await c.answer()


@router.callback_query(F.data.startswith("dl:"))
async def delivered(c: CallbackQuery, bot: Bot):
    if not admin_only(c):
        return await c.answer()
    order_id = int(c.data.split(":")[1])
    if not await set_status(order_id, "delivered", "paid"):
        return await c.answer("Allaqachon ishlangan", show_alert=True)
    o = await get_order(order_id)
    await c.message.edit_caption(caption=f"#{order_id} YETKAZILDI ✅")
    await bot.send_message(o["user_id"], f"✅ Buyurtma #{order_id}: {describe(o)} {o['recipient']} ga yuborildi!")
    await c.answer()


@router.callback_query(F.data.startswith("fl:"))
async def failed(c: CallbackQuery, bot: Bot):
    if not admin_only(c):
        return await c.answer()
    order_id = int(c.data.split(":")[1])
    if not await set_status(order_id, "failed", "paid"):
        return await c.answer("Allaqachon ishlangan", show_alert=True)
    o = await get_order(order_id)
    await c.message.edit_caption(caption=f"#{order_id} XATO ⚠️ (to'lov olingan, qo'lda hal qiling)")
    await bot.send_message(o["user_id"], f"Buyurtma #{order_id} da muammo chiqdi. Admin tez orada bog'lanadi.")
    await c.answer()


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi. /start")


async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
