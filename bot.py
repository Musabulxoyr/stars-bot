import asyncio
import logging
import os
import re
import time

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CARD_NUMBER = os.getenv("CARD_NUMBER", "0000 0000 0000 0000")
CARD_OWNER = os.getenv("CARD_OWNER", "Ism Familiya")
PRICE_PER_STAR = float(os.getenv("PRICE_PER_STAR", "200"))  # so'm, o'zingizning marjangiz bilan
DB_PATH = os.getenv("DB_PATH", "orders.db")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@musabulxoyr")  # xabarlarda ko'rsatiladi
LOG_CHANNEL = os.getenv("LOG_CHANNEL", "-1004372315464")  # masalan @starspaylogs yoki -100123456789
MIN_TOPUP = int(os.getenv("MIN_TOPUP", "5000"))  # balansni eng kam to'ldirish summasi (so'm)

PACKS = [50, 75, 100, 150, 250, 350, 500, 750, 1000, 1500, 2500, 5000, 10000]
MIN_STARS, MAX_STARS = 50, 10000  # Fragment minimumi 50 Stars

# Referal bonuslari
REF_INVITE_STARS = float(os.getenv("REF_INVITE_STARS", "0.10"))  # har bir taklif uchun (⭐)
REF_PERCENT = float(os.getenv("REF_PERCENT", "1"))  # do'st har bir xarididan (%)
INVITE_BONUS = int(round(REF_INVITE_STARS * PRICE_PER_STAR))  # so'mda (0.10 ⭐ = 20 so'm)

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


class TopUp(StatesGroup):
    amount = State()
    receipt = State()


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
        await db.execute(
            """CREATE TABLE IF NOT EXISTS banned (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                created_at INTEGER NOT NULL
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER PRIMARY KEY,
                amount INTEGER NOT NULL DEFAULT 0
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS topups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL,
                receipt_uid TEXT UNIQUE,
                created_at INTEGER NOT NULL
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                referrer_id INTEGER,
                ref_paid INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )"""
        )
        cur = await db.execute("PRAGMA table_info(users)")
        ucols = [r[1] for r in await cur.fetchall()]
        if "ref_earned" not in ucols:
            await db.execute("ALTER TABLE users ADD COLUMN ref_earned INTEGER NOT NULL DEFAULT 0")
        cur = await db.execute("PRAGMA table_info(orders)")
        cols = [r[1] for r in await cur.fetchall()]
        if "item" not in cols:
            await db.execute("ALTER TABLE orders ADD COLUMN item TEXT NOT NULL DEFAULT 'stars'")
        await db.commit()


# ---------- Balans ----------
async def get_balance(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT amount FROM balances WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0


async def add_balance(user_id: int, delta: int) -> int:
    """Balansga qo'shadi (manfiy bo'lsa ayiradi, lekin 0 dan pastga tushirmaydi)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO balances (user_id, amount) VALUES (?, 0)", (user_id,))
        await db.execute(
            "UPDATE balances SET amount = MAX(amount + ?, 0) WHERE user_id=?", (delta, user_id)
        )
        await db.commit()
    return await get_balance(user_id)


async def pay_order_with_balance(order_id: int, user_id: int) -> str:
    """Bitta tranzaksiyada: buyurtma pending bo'lsa va pul yetsa, yechadi va 'paid' qiladi."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT price FROM orders WHERE id=? AND user_id=? AND status='pending'",
            (order_id, user_id),
        )
        row = await cur.fetchone()
        if not row:
            await db.rollback()
            return "gone"
        cur = await db.execute(
            "UPDATE balances SET amount = amount - ? WHERE user_id=? AND amount >= ?",
            (row[0], user_id, row[0]),
        )
        if cur.rowcount != 1:
            await db.rollback()
            return "low"
        await db.execute("UPDATE orders SET status='paid' WHERE id=?", (order_id,))
        await db.commit()
        return "ok"


async def create_topup(user_id: int, amount: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO topups (user_id, amount, status, created_at) VALUES (?, ?, 'pending', ?)",
            (user_id, amount, int(time.time())),
        )
        await db.commit()
        return cur.lastrowid


async def get_topup(tid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM topups WHERE id=?", (tid,))
        return await cur.fetchone()


async def set_topup_status(tid, new, expected) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE topups SET status=? WHERE id=? AND status=?", (new, tid, expected)
        )
        await db.commit()
        return cur.rowcount == 1


async def attach_topup_receipt(tid, uid) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cur = await db.execute(
                "UPDATE topups SET receipt_uid=?, status='receipt_sent' WHERE id=? AND status='pending'",
                (uid, tid),
            )
            await db.commit()
            return cur.rowcount == 1
        except aiosqlite.IntegrityError:
            return False


# ---------- Referal ----------
async def register_user(user_id: int, referrer_id: int | None = None) -> bool:
    """Yangi foydalanuvchini yozadi. Faqat yangi bo'lsa referrer saqlanadi."""
    async with aiosqlite.connect(DB_PATH) as db:
        if referrer_id is not None:
            cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (referrer_id,))
            if referrer_id == user_id or not await cur.fetchone():
                referrer_id = None
        cur = await db.execute(
            "INSERT OR IGNORE INTO users (user_id, referrer_id, ref_earned, created_at) VALUES (?, ?, ?, ?)",
            (user_id, referrer_id, INVITE_BONUS if referrer_id else 0, int(time.time())),
        )
        await db.commit()
        joined = cur.rowcount == 1 and referrer_id is not None
    if joined and INVITE_BONUS > 0:
        await add_balance(referrer_id, INVITE_BONUS)  # har bir taklif uchun bonus
    return joined


async def reward_referrer(user_id: int, price: int):
    """Do'sti har bir xarid qilganda taklif qilganga foiz bonus beradi."""
    bonus = int(price * REF_PERCENT / 100)
    if bonus <= 0:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT referrer_id FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row or row[0] is None:
            return None
        await db.execute(
            "UPDATE users SET ref_paid=1, ref_earned=ref_earned+? WHERE user_id=?", (bonus, user_id)
        )
        await db.commit()
        referrer_id = row[0]
    await add_balance(referrer_id, bonus)
    return referrer_id, bonus


async def ref_stats(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(ref_paid), 0), COALESCE(SUM(ref_earned), 0) "
            "FROM users WHERE referrer_id=?",
            (user_id,),
        )
        return await cur.fetchone()


# ---------- Ban ----------
BANNED: set[int] = set()  # tez tekshirish uchun xotirada


async def load_banned():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM banned")
        BANNED.update(r[0] for r in await cur.fetchall())


async def ban_user(user_id: int, reason: str = "") -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO banned (user_id, reason, created_at) VALUES (?, ?, ?)",
            (user_id, reason, int(time.time())),
        )
        await db.commit()
    BANNED.add(user_id)
    return cur.rowcount == 1


async def unban_user(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM banned WHERE user_id=?", (user_id,))
        await db.commit()
    BANNED.discard(user_id)
    return cur.rowcount == 1


class BanMiddleware(BaseMiddleware):
    """Bloklangan foydalanuvchining hech bir xabari/tugmasi ishlamaydi."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id in BANNED and user.id != ADMIN_ID:
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Siz bloklangansiz.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("🚫 Siz bloklangansiz.")
            return
        return await handler(event, data)


def mask_username(u: str) -> str:
    """@username ni qisman yashiradi: @musabulxoyr -> @mus*****"""
    name = u.lstrip("@")
    return "@" + name[:3] + "*" * max(len(name) - 3, 2)


async def log_order(bot: Bot, o):
    """Yetkazilgan buyurtmani kanalga tashlaydi (kanal sozlanmagan bo'lsa jim o'tadi)."""
    if not LOG_CHANNEL:
        return
    text = (
        "✅ Buyurtma bajarildi\n\n"
        f"🎁 {describe(o)}\n"
        f"👤 {mask_username(o['recipient'])}\n"
        f"💰 {fmt(o['price'])} so'm\n"
        f"🕒 {time.strftime('%d.%m.%Y %H:%M')}"
    )
    chat = int(LOG_CHANNEL) if LOG_CHANNEL.lstrip("-").isdigit() else LOG_CHANNEL
    try:
        await bot.send_message(chat, text)
    except Exception as e:
        logging.warning("Log kanaliga yuborib bo'lmadi: %s", e)


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
    kb.button(text="💰 Balans", callback_data="menu:balance")
    kb.button(text="👥 Referal", callback_data="menu:ref")
    kb.adjust(2, 2)
    return kb.as_markup()


@router.callback_query(F.data == "menu:ref")
async def menu_ref(c: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await register_user(c.from_user.id)
    total, paid, earned = await ref_stats(c.from_user.id)
    username = (await bot.me()).username
    link = f"https://t.me/{username}?start=ref_{c.from_user.id}"
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Orqaga", callback_data="menu:main")
    example = int(price_for(100) * REF_PERCENT / 100)
    pct = f"{REF_PERCENT:g}"
    await c.message.answer(
        "🎁 Qanday bonus olasiz?\n"
        f"👥 Har bir taklif uchun → {REF_INVITE_STARS:.2f} ⭐\n"
        f"💸 Do'stingiz har bir xarididan → {pct}% so'm bonus\n\n"
        "💡 Misol:\n"
        f"⭐️ Do'stingiz 100 stars xarid qildi → {fmt(example)} so'm sizga\n\n"
        f"🔗 Sizning havolangiz:\n{link}\n\n"
        f"👤 Taklif qilinganlar: {total}\n"
        f"🛒 Xarid qilganlar: {paid}\n"
        f"💰 Jami ishlangan: {fmt(earned)} so'm",
        reply_markup=kb.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data == "menu:balance")
async def menu_balance(c: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_balance(c.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Balansni to'ldirish", callback_data="topup:start")
    kb.button(text="⬅️ Orqaga", callback_data="menu:main")
    kb.adjust(1)
    await c.message.answer(
        f"💰 Sizning balansingiz: {fmt(bal)} so'm\n\n"
        "Balansdan Stars va Gift buyurtmalarini to'lashda foydalanishingiz mumkin.",
        reply_markup=kb.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data == "topup:start")
async def topup_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(TopUp.amount)
    await c.message.answer(f"To'ldirish summasini so'mda yozing (kamida {fmt(MIN_TOPUP)}):")
    await c.answer()


@router.message(TopUp.amount)
async def topup_amount(m: Message, state: FSMContext):
    txt = (m.text or "").replace(" ", "")
    if not txt.isdigit() or int(txt) < MIN_TOPUP:
        return await m.answer(f"{fmt(MIN_TOPUP)} so'mdan kam bo'lmagan butun son yozing.")
    amount = int(txt)
    tid = await create_topup(m.from_user.id, amount)
    await state.update_data(topup_id=tid)
    await state.set_state(TopUp.receipt)
    await m.answer(
        f"💳 Balansni to'ldirish\n"
        f"💰 Summa: {fmt(amount)} so'm\n\n"
        f"Karta: {CARD_NUMBER}\n{CARD_OWNER}\n\n"
        "To'lovdan keyin chek rasmini (screenshot) shu yerga yuboring."
    )


@router.message(TopUp.receipt, F.photo)
async def topup_receipt(m: Message, state: FSMContext, bot: Bot):
    tid = (await state.get_data())["topup_id"]
    photo = m.photo[-1]
    if not await attach_topup_receipt(tid, photo.file_unique_id):
        return await m.answer("Bu chek avval ishlatilgan yoki so'rov allaqachon yuborilgan.")
    t = await get_topup(tid)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ To'lov keldi", callback_data=f"tok:{tid}")
    kb.button(text="❌ Rad etish", callback_data=f"tno:{tid}")
    kb.button(text="🚫 Ban", callback_data=f"ban:{m.from_user.id}")
    kb.adjust(2, 1)
    await bot.send_photo(
        ADMIN_ID,
        photo.file_id,
        caption=f"💰 BALANS | {fmt(t['amount'])} so'm\nFoydalanuvchi: {m.from_user.id}",
        reply_markup=kb.as_markup(),
    )
    await state.clear()
    await m.answer("✅ Chek qabul qilindi! Admin tekshirgach balansingizga qo'shiladi ⏳")


@router.message(TopUp.receipt)
async def topup_receipt_wrong(m: Message):
    await m.answer("Iltimos, chek rasmini yuboring.")


@router.message(CommandStart())
async def start(m: Message, state: FSMContext, command: CommandObject, bot: Bot):
    await state.clear()
    referrer = None
    arg = command.args or ""
    if arg.startswith("ref_") and arg[4:].isdigit():
        referrer = int(arg[4:])
    if await register_user(m.from_user.id, referrer):
        try:
            await bot.send_message(
                referrer,
                f"👥 Havolangiz orqali yangi foydalanuvchi qo'shildi!\n"
                f"💰 Balansingizga {fmt(INVITE_BONUS)} so'm ({REF_INVITE_STARS:g} ⭐) qo'shildi.",
            )
        except Exception:
            pass
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
        f"🛒 Buyurtma\n"
        f"{describe(o)} → {username}\n"
        f"💰 To'lov: {fmt(price)} so'm\n\n"
        f"Karta: {CARD_NUMBER}\n{CARD_OWNER}\n\n"
        "To'lovdan keyin chek rasmini (screenshot) shu yerga yuboring.\n"
        "Yoki balansingizdan to'lang 👇",
        reply_markup=InlineKeyboardBuilder()
        .button(text="💰 Balansdan to'lash", callback_data=f"paybal:{order_id}")
        .as_markup(),
    )


@router.callback_query(F.data.startswith("paybal:"))
async def pay_with_balance(c: CallbackQuery, state: FSMContext, bot: Bot):
    order_id = int(c.data.split(":")[1])
    res = await pay_order_with_balance(order_id, c.from_user.id)
    if res == "gone":
        return await c.answer("Buyurtma topilmadi yoki allaqachon to'langan.", show_alert=True)
    if res == "low":
        return await c.answer("Balansingizda mablag' yetarli emas. Avval balansni to'ldiring.", show_alert=True)
    await state.clear()
    o = await get_order(order_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Yetkazildi", callback_data=f"dl:{order_id}")
    kb.button(text="⚠️ Xato", callback_data=f"fl:{order_id}")
    await bot.send_message(
        ADMIN_ID,
        f"BALANSDAN TO'LANDI ({fmt(o['price'])} so'm)\n"
        f"{describe(o)} ni {o['recipient']} ga yuboring, keyin «Yetkazildi» ni bosing.\n"
        f"Foydalanuvchi: {o['user_id']}",
        reply_markup=kb.as_markup(),
    )
    await c.message.answer(
        f"✅ Buyurtma balansdan to'landi!\n"
        f"{describe(o)} tez orada {o['recipient']} ga yuboriladi."
    )
    await c.answer()


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
    kb.button(text="🚫 Ban", callback_data=f"ban:{m.from_user.id}")
    kb.adjust(2, 1)
    await bot.send_photo(
        ADMIN_ID,
        photo.file_id,
        caption=f"{describe(o)} → {o['recipient']}\n"
        f"Summa: {fmt(o['price'])} so'm\nFoydalanuvchi: {m.from_user.id}",
        reply_markup=kb.as_markup(),
    )
    await state.clear()
    await m.answer(
        "✅ Chek qabul qilindi!\n\n"
        f"Buyurtma: {describe(o)} → {o['recipient']}\n"
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


async def edit_admin(c: CallbackQuery, text: str, markup=None):
    """Rasmli xabarda caption, oddiy xabarda text tahrirlanadi."""
    if c.message.photo:
        await c.message.edit_caption(caption=text, reply_markup=markup)
    else:
        await c.message.edit_text(text, reply_markup=markup)


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
        caption=f"TO'LANDI ✅\n{describe(o)} ni {o['recipient']} ga yuboring, "
        "keyin «Yetkazildi» ni bosing.",
        reply_markup=kb.as_markup(),
    )
    await bot.send_message(
        o["user_id"],
        f"💳 To'lovingiz tasdiqlandi!\n"
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
    await c.message.edit_caption(caption="RAD ETILDI ❌")
    await bot.send_message(o["user_id"], f"Buyurtma uchun to'lov tasdiqlanmadi. Admin bilan bog'laning: {ADMIN_USERNAME}")
    await c.answer()


@router.callback_query(F.data.startswith("dl:"))
async def delivered(c: CallbackQuery, bot: Bot):
    if not admin_only(c):
        return await c.answer()
    order_id = int(c.data.split(":")[1])
    if not await set_status(order_id, "delivered", "paid"):
        return await c.answer("Allaqachon ishlangan", show_alert=True)
    o = await get_order(order_id)
    await edit_admin(c, "YETKAZILDI ✅")
    await bot.send_message(o["user_id"], f"✅ {describe(o)} {o['recipient']} ga yuborildi!")
    await log_order(bot, o)
    res = await reward_referrer(o["user_id"], o["price"])
    if res:
        referrer, bonus = res
        try:
            await bot.send_message(
                referrer,
                f"🎉 Do'stingiz xarid qildi! Balansingizga {fmt(bonus)} so'm bonus qo'shildi.",
            )
        except Exception:
            pass
    await c.answer()


@router.callback_query(F.data.startswith("fl:"))
async def failed(c: CallbackQuery, bot: Bot):
    if not admin_only(c):
        return await c.answer()
    order_id = int(c.data.split(":")[1])
    if not await set_status(order_id, "failed", "paid"):
        return await c.answer("Allaqachon ishlangan", show_alert=True)
    o = await get_order(order_id)
    await edit_admin(c, "XATO ⚠️ (to'lov olingan, qo'lda hal qiling)")
    await bot.send_message(o["user_id"], f"Buyurtmangizda muammo chiqdi. Admin tez orada bog'lanadi.")
    await c.answer()


@router.callback_query(F.data.startswith("tok:"))
async def topup_ok(c: CallbackQuery, bot: Bot):
    if not admin_only(c):
        return await c.answer()
    tid = int(c.data.split(":")[1])
    if not await set_topup_status(tid, "paid", "receipt_sent"):
        return await c.answer("Allaqachon ishlangan", show_alert=True)
    t = await get_topup(tid)
    bal = await add_balance(t["user_id"], t["amount"])
    await c.message.edit_caption(caption=f"💰 BALANS QO'SHILDI ✅ ({fmt(t['amount'])} so'm)")
    await bot.send_message(
        t["user_id"],
        f"✅ Balansingiz {fmt(t['amount'])} so'mga to'ldirildi!\n💰 Joriy balans: {fmt(bal)} so'm",
    )
    await c.answer()


@router.callback_query(F.data.startswith("tno:"))
async def topup_no(c: CallbackQuery, bot: Bot):
    if not admin_only(c):
        return await c.answer()
    tid = int(c.data.split(":")[1])
    if not await set_topup_status(tid, "rejected", "receipt_sent"):
        return await c.answer("Allaqachon ishlangan", show_alert=True)
    t = await get_topup(tid)
    await c.message.edit_caption(caption="💰 BALANS RAD ETILDI ❌")
    await bot.send_message(t["user_id"], f"Balans to'ldirish tasdiqlanmadi. Admin bilan bog'laning: {ADMIN_USERNAME}")
    await c.answer()


@router.message(Command("addbal"), F.from_user.id == ADMIN_ID)
async def addbal_cmd(m: Message, bot: Bot):
    parts = (m.text or "").split()
    try:
        uid, delta = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return await m.answer("Ishlatish: /addbal <user_id> <summa>  (ayirish uchun minus: -5000)")
    bal = await add_balance(uid, delta)
    await m.answer(f"✅ {uid} balansi: {fmt(bal)} so'm")
    try:
        await bot.send_message(uid, f"💰 Balansingiz o'zgardi. Joriy balans: {fmt(bal)} so'm")
    except Exception:
        pass


@router.message(Command("bal"), F.from_user.id == ADMIN_ID)
async def bal_cmd(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.answer("Ishlatish: /bal <user_id>")
    await m.answer(f"{parts[1]} balansi: {fmt(await get_balance(int(parts[1])))} so'm")


@router.callback_query(F.data.startswith("ban:"))
async def ban_btn(c: CallbackQuery, bot: Bot):
    if not admin_only(c):
        return await c.answer()
    uid = int(c.data.split(":")[1])
    if uid == ADMIN_ID:
        return await c.answer("O'zingizni ban qila olmaysiz", show_alert=True)
    await ban_user(uid, "admin tugmasi")
    await c.answer(f"🚫 {uid} bloklandi", show_alert=True)


@router.message(Command("ban"), F.from_user.id == ADMIN_ID)
async def ban_cmd(m: Message):
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.answer("Ishlatish: /ban <user_id> [sabab]")
    uid = int(parts[1])
    if uid == ADMIN_ID:
        return await m.answer("O'zingizni ban qila olmaysiz.")
    new = await ban_user(uid, parts[2] if len(parts) > 2 else "")
    await m.answer(f"🚫 {uid} bloklandi." if new else f"{uid} allaqachon bloklangan.")


@router.message(Command("unban"), F.from_user.id == ADMIN_ID)
async def unban_cmd(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.answer("Ishlatish: /unban <user_id>")
    ok = await unban_user(int(parts[1]))
    await m.answer(f"✅ {parts[1]} blokdan chiqarildi." if ok else "Bu foydalanuvchi bloklanmagan.")


@router.message(Command("banlist"), F.from_user.id == ADMIN_ID)
async def banlist_cmd(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, reason FROM banned ORDER BY created_at DESC LIMIT 50")
        rows = await cur.fetchall()
    if not rows:
        return await m.answer("Bloklanganlar yo'q.")
    await m.answer("🚫 Bloklanganlar:\n" + "\n".join(f"{u} — {r or '-'}" for u, r in rows))


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi. /start")


async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await load_banned()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    await bot.set_my_commands([
        BotCommand(command="start", description="Botni boshlash"),
        BotCommand(command="cancel", description="Bekor qilish"),
    ])
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Botni boshlash"),
            BotCommand(command="cancel", description="Bekor qilish"),
            BotCommand(command="ban", description="Ban: /ban id [sabab]"),
            BotCommand(command="unban", description="Blokdan chiqarish: /unban id"),
            BotCommand(command="banlist", description="Bloklanganlar ro'yxati"),
            BotCommand(command="bal", description="Balans: /bal id"),
            BotCommand(command="addbal", description="Balans: /addbal id summa"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID),
    )
    dp.message.outer_middleware(BanMiddleware())
    dp.callback_query.outer_middleware(BanMiddleware())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
