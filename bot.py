import asyncio
import logging
import os
import time

import aiohttp
import aiosqlite
from dotenv import load_dotenv

try:
    from mystars_faas import AsyncMyStarsClient
except ImportError:  # SDK o'rnatilmagan bo'lsa qo'lda yetkazish ishlaydi
    AsyncMyStarsClient = None
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
MYSTARS_API_KEY = os.getenv("MYSTARS_API_KEY", "")  # bo'sh bo'lsa Stars qo'lda yuboriladi
AUTO_TON_CONFIRM = os.getenv("AUTO_TON_CONFIRM", "0") == "1"  # 1 = TON tushsa o'zi tasdiqlaydi

# Narxlar: 1 Star = 200 so'm. TON narxi (1 Star = 0.008 TON) taxminiy, o'zingiz tuzating.
PACKAGES = {
    s: {"uzs": s * 200, "ton": round(s * 0.008, 2)}
    for s in (50, 75, 100, 150, 250, 350, 500, 750, 1000, 1500, 2500, 5000, 10000)
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
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN ms_order TEXT")
        except Exception:
            pass  # ustun allaqachon bor
        await db.commit()


async def list_orders(status):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE status=?", (status,))
        return await cur.fetchall()


async def set_ms_order(oid, ms_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET ms_order=? WHERE id=?", (ms_id, oid))
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
    btns = [
        InlineKeyboardButton(
            text=f"{st} ⭐ ({p['uzs']:,} so'm)".replace(",", " "),
            callback_data=f"pkg:{st}",
        )
        for st, p in PACKAGES.items()
    ]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
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


def admin_kb(oid, retry=False):
    row = [InlineKeyboardButton(text="📦 Qo'lda yubordim", callback_data=f"adm:done:{oid}")]
    if retry:
        row.insert(0, InlineKeyboardButton(text="🔁 Qayta urinish", callback_data=f"adm:retry:{oid}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def on_paid(oid):
    """To'lov tasdiqlanganda chaqiriladi (admin tasdiqlagach)."""
    o = await get_order(oid)
    await bot.send_message(o["user_id"],
                           f"✅ To'lov qabul qilindi (#{oid}). Stars tez orada yuboriladi.")
    if MYSTARS_API_KEY and AsyncMyStarsClient:
        await deliver_mystars(oid)
    else:
        await bot.send_message(
            ADMIN_ID,
            f"💰 To'landi — buyurtma #{oid}\nYubor: ⭐ {o['stars']} → @{o['target']}",
            reply_markup=admin_kb(oid),
        )


async def deliver_mystars(oid):
    """MyStars orqali Stars yetkazish: tekshirish -> buyurtma -> admin TON to'laydi."""
    if not await set_status(oid, "ordering", ["paid"]):
        return  # allaqachon jarayonda
    o = await get_order(oid)
    try:
        async with AsyncMyStarsClient.production(MYSTARS_API_KEY) as ms:
            check = await ms.check_recipient(o["target"], type="stars")
            if not check.eligible:
                await set_status(oid, "failed", ["ordering"])
                why = check.telegram_message or check.reason or "noma'lum sabab"
                await bot.send_message(
                    ADMIN_ID, f"⚠️ #{oid}: @{o['target']} ga Stars yuborib bo'lmaydi: {why}")
                await bot.send_message(
                    o["user_id"],
                    f"⚠️ Buyurtma #{oid}: @{o['target']} akkauntiga Stars yuborib bo'lmadi. "
                    "Username to'g'riligini tekshiring, admin siz bilan bog'lanadi.")
                return
            order = await ms.create_order(
                type="stars", recipient=o["target"], quantity=o["stars"],
                payment_currency="ton",
            )
        pay = order.payment
        await set_ms_order(oid, str(order.order_id))
        await set_status(oid, "ms_wait", ["ordering"])
        await bot.send_message(
            ADMIN_ID,
            f"💸 <b>MyStars to'lovi — buyurtma #{oid}</b>\n"
            f"⭐ {o['stars']} → @{o['target']}\n"
            f"(Mijoz to'ladi: {o['uzs']:,} so'm)\n\n".replace(",", " ") +
            "Quyidagini TON hamyoningizdan yuboring:\n"
            f"Manzil:\n<code>{pay.pay_to_address}</code>\n"
            f"Summa: <code>{pay.amount}</code> {str(pay.currency).upper()}\n"
            f"Izoh (memo) — <b>majburiy</b>:\n<code>{pay.memo}</code>\n\n"
            "To'lovdan keyin bot o'zi kuzatadi va mijozga xabar beradi.",
        )
    except Exception as e:
        logging.exception("MyStars xatosi")
        await set_status(oid, "paid", ["ordering"])
        await bot.send_message(
            ADMIN_ID,
            f"❌ #{oid}: MyStars buyurtmasi yaratilmadi:\n<code>{e}</code>",
            reply_markup=admin_kb(oid, retry=True),
        )


def is_admin(c: CallbackQuery):
    return c.from_user.id == ADMIN_ID


async def mark_msg(c: CallbackQuery, suffix: str):
    if c.message.photo:
        await c.message.edit_caption(caption=(c.message.caption or "") + suffix)
    else:
        await c.message.edit_text((c.message.html_text or "") + suffix)


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
            await mark_msg(c, "\n\n✅ Tasdiqlandi")
            await on_paid(oid)
    elif action == "no":
        if await set_status(oid, "rejected", ["pending", "review"]):
            await bot.send_message(o["user_id"], f"❌ Buyurtma #{oid} rad etildi. Savol bo'lsa adminga yozing.")
            await mark_msg(c, "\n\n❌ Rad etildi")
    elif action == "retry":
        if MYSTARS_API_KEY and AsyncMyStarsClient and o["status"] == "paid":
            await c.answer("Qayta urinilmoqda...")
            await deliver_mystars(oid)
            return
    elif action == "done":
        if await set_status(oid, "delivered", ["paid", "ms_wait"]):
            await bot.send_message(o["user_id"], f"🎉 Buyurtma #{oid}: ⭐ {o['stars']} Stars yuborildi!")
            await mark_msg(c, "\n\n📦 Yuborildi")
    await c.answer()


async def mystars_watcher():
    """MyStars buyurtmalari holatini har 30 soniyada tekshiradi."""
    while True:
        try:
            rows = await list_orders("ms_wait")
            if rows:
                async with AsyncMyStarsClient.production(MYSTARS_API_KEY) as ms:
                    for o in rows:
                        r = await ms.get_order(o["ms_order"])
                        st = str(r.status)
                        if st == "delivered":
                            if await set_status(o["id"], "delivered", ["ms_wait"]):
                                await bot.send_message(
                                    o["user_id"],
                                    f"🎉 Buyurtma #{o['id']}: ⭐ {o['stars']} Stars yuborildi!")
                                await bot.send_message(ADMIN_ID, f"✅ #{o['id']} yetkazildi.")
                        elif st in ("failed", "reversed", "expired"):
                            if await set_status(o["id"], "failed", ["ms_wait"]):
                                await bot.send_message(
                                    ADMIN_ID,
                                    f"⚠️ #{o['id']}: MyStars holati <b>{st}</b>. "
                                    "Buyurtmani tekshiring (kerak bo'lsa mijozga pulini qaytaring).")
                                await bot.send_message(
                                    o["user_id"],
                                    f"⚠️ Buyurtma #{o['id']} bilan muammo chiqdi, admin siz bilan bog'lanadi.")
        except Exception as e:
            logging.warning("mystars_watcher xatosi: %s", e)
        await asyncio.sleep(30)


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
                        if AUTO_TON_CONFIRM:
                            if await set_status(oid, "paid", ["pending"]):
                                await on_paid(oid)
                        elif await set_status(oid, "review", ["pending"]):
                            kb = InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"adm:ok:{oid}"),
                                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"adm:no:{oid}"),
                            ]])
                            await bot.send_message(
                                ADMIN_ID,
                                f"💎 TON tushdi — buyurtma #{oid}\n⭐ {o['stars']} → @{o['target']}\n"
                                f"Summa: {value} TON",
                                reply_markup=kb,
                            )
            except Exception as e:
                logging.warning("ton_watcher xatosi: %s", e)
            await asyncio.sleep(20)


async def main():
    await init_db()
    if TON_WALLET:
        asyncio.create_task(ton_watcher())
    if MYSTARS_API_KEY and AsyncMyStarsClient:
        asyncio.create_task(mystars_watcher())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
