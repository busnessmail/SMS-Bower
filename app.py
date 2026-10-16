import asyncio
import logging
import os
import requests
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from motor.motor_asyncio import AsyncIOMotorClient

# ==================== CONFIGURATION ====================
# Railway Environment Variables থেকে ডাটা রিড করবে
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SMSBOWER_API_KEY = os.getenv("SMSBOWER_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
# =======================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# MongoDB Connection
client = AsyncIOMotorClient(MONGO_URI)
db = client["sms_bot_db"]
users_col = db["users"]
settings_col = db["settings"]


# Helper: Get/Set settings
async def get_setting(key, default):
    setting = await settings_col.find_one({"key": key})
    return setting["value"] if setting else default


async def set_setting(key, value):
    await settings_col.update_one(
        {"key": key}, {"$set": {"value": value}}, upsert=True
    )


async def em(default_emoji: str) -> str:
    emoji_id = await get_setting("custom_emoji_id", "")
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{default_emoji}</tg-emoji>'
    return default_emoji


class DepositStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_txid = State()
    waiting_for_screenshot = State()


class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_wa_price = State()
    waiting_for_emoji = State()


@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    is_on = await get_setting("bot_status", True)
    if not is_on and user_id != ADMIN_ID:
        await message.answer(
            "⚠️ বট বর্তমানে মেইনটেন্যান্স কাজের জন্য বন্ধ রয়েছে।"
        )
        return

    user = await users_col.find_one({"user_id": user_id})
    if not user:
        await users_col.insert_one(
            {
                "user_id": user_id,
                "balance": 0.0,
                "username": message.from_user.username,
            }
        )

    b_emoji = await em("🛒")
    w_emoji = await em("💰")
    d_emoji = await em("📥")
    a_emoji = await em("⚙️")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{b_emoji} Buy Number", callback_data="buy_number"
                ),
                InlineKeyboardButton(
                    text=f"{w_emoji} Balance", callback_data="check_balance"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{d_emoji} Deposit", callback_data="deposit_start"
                )
            ],
        ]
    )

    if user_id == ADMIN_ID:
        keyboard.inline_keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"{a_emoji} Admin Panel", callback_data="admin_panel"
                )
            ]
        )

    await message.answer(
        "স্বাগতম! আমাদের বট থেকে খুব সহজে ভার্চুয়াল নম্বর কিনতে পারেন:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "check_balance")
async def check_balance(callback: CallbackQuery):
    user = await users_col.find_one({"user_id": callback.from_user.id})
    balance = user.get("balance", 0.0) if user else 0.0
    await callback.answer(f"আপনার বর্তমান ব্যালেন্স: ${balance:.2f}", show_alert=True)


@router.callback_query(F.data == "deposit_start")
async def deposit_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💵 আপনি কত ডলার ডিপোজিট করতে চান?\n*(ন্যূনতম ডিপোজিট ০.৫০$)*"
    )
    await state.set_state(DepositStates.waiting_for_amount)


@router.message(DepositStates.waiting_for_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 0.5:
            await message.answer("❌ মিনিমাম ডিপোজিট ০.৫০$ হতে হবে।")
            return

        await state.update_data(amount=amount)
        binance_id = await get_setting("binance_id", "123456789")

        text = (
            f"📥 **ডিপোজিট নির্দেশিকা:**\n\n"
            f"Binance Pay ID: `{binance_id}`\n"
            f"পরিমাণ: **${amount}**\n\n"
            f"টাকা পাঠানোর পর TxID বা অর্ডার আইডি লিখুন:"
        )
        await message.answer(text, parse_mode="MARKDOWN")
        await state.set_state(DepositStates.waiting_for_txid)
    except ValueError:
        await message.answer("❌ দয়া করে সঠিক সংখ্যা লিখুন:")


@router.message(DepositStates.waiting_for_txid)
async def process_deposit_txid(message: Message, state: FSMContext):
    await state.update_data(txid=message.text)
    await message.answer("📸 এখন পেমেন্টের স্ক্রিনশট দিন:")
    await state.set_state(DepositStates.waiting_for_screenshot)


@router.message(DepositStates.waiting_for_screenshot, F.photo)
async def process_deposit_screenshot(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    data = await state.get_data()
    amount = data["amount"]
    txid = data["txid"]
    user_id = message.from_user.id

    await state.clear()
    await message.answer(
        "✅ আপনার ডিপোজিট রিকোয়েস্ট সাবমিট হয়েছে! অ্যাডমিন চেক করে রিভিউ করবেন।"
    )

    admin_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Approve",
                    callback_data=f"dep_app_{user_id}_{amount}",
                ),
                InlineKeyboardButton(
                    text="❌ Reject", callback_data=f"dep_rej_{user_id}"
                ),
            ]
        ]
    )

    admin_text = (
        f"🔔 **নতুন ডিপোজিট রিকোয়েস্ট!**\n\n"
        f"👤 User ID: `{user_id}`\n"
        f"💵 Amount: `${amount}`\n"
        f"🔖 TxID: `{txid}`"
    )
    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=photo_id,
        caption=admin_text,
        reply_markup=admin_keyboard,
        parse_mode="MARKDOWN",
    )


@router.callback_query(F.data.startswith("dep_app_"))
async def approve_deposit(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    _, _, user_id_str, amount_str = callback.data.split("_")
    user_id = int(user_id_str)
    amount = float(amount_str)

    await users_col.update_one(
        {"user_id": user_id}, {"$inc": {"balance": amount}}
    )
    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n✅ **Approved**"
    )
    await bot.send_message(
        user_id,
        f"🎉 আপনার ডিপোজিট অ্যাপ্রুভ হয়েছে! `${amount}` যোগ করা হয়েছে।",
    )


@router.callback_query(F.data.startswith("dep_rej_"))
async def reject_deposit(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    _, _, user_id_str = callback.data.split("_")
    user_id = int(user_id_str)

    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n❌ **Rejected**"
    )
    await bot.send_message(user_id, "❌ আপনার ডিপোজিট রিকোয়েস্টটি রিজেক্ট করা হয়েছে।")


@router.callback_query(F.data == "buy_number")
async def buy_number_menu(callback: CallbackQuery):
    wa_price = float(await get_setting("wa_price", 0.50))
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📱 WhatsApp (${wa_price})",
                    callback_data="buy_wa_number",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back", callback_data="back_to_start"
                )
            ],
        ]
    )
    await callback.message.edit_text(
        "সার্ভিস সিলেক্ট করুন:", reply_markup=keyboard
    )


@router.callback_query(F.data == "buy_wa_number")
async def buy_whatsapp(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await users_col.find_one({"user_id": user_id})
    wa_price = float(await get_setting("wa_price", 0.50))

    if not user or user.get("balance", 0.0) < wa_price:
        await callback.answer(
            "❌ আপনার পর্যাপ্ত ব্যালেন্স নেই।", show_alert=True
        )
        return

    url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=getNumber&service=wa&country=0"
    try:
        response = requests.get(url).text
        if response.startswith("ACCESS_NUMBER"):
            _, activation_id, phone_number = response.split(":")

            await users_col.update_one(
                {"user_id": user_id}, {"$inc": {"balance": -wa_price}}
            )

            await callback.message.edit_text(
                f"✅ নম্বর পাওয়া গেছে!\n\n"
                f"📞 **Number:** `+{phone_number}`\n"
                f"🆔 **ID:** `{activation_id}`",
                parse_mode="MARKDOWN",
            )
        else:
            await callback.answer(f"❌ স্টক নেই বা সমস্যা: {response}", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ API Error: {str(e)}", show_alert=True)


@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    bot_status = await get_setting("bot_status", True)
    status_text = "🟢 ON" if bot_status else "🔴 OFF"
    wa_price = await get_setting("wa_price", 0.50)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Bot Status: {status_text}",
                    callback_data="admin_toggle_bot",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Set WA Price (${wa_price})",
                    callback_data="admin_set_wa_price",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 Broadcast", callback_data="admin_broadcast"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎨 Set Emoji ID", callback_data="admin_set_emoji"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Main Menu", callback_data="back_to_start"
                )
            ],
        ]
    )
    await callback.message.edit_text(
        "⚙️ **অ্যাডমিন প্যানেল:**", reply_markup=keyboard, parse_mode="MARKDOWN"
    )


@router.callback_query(F.data == "admin_toggle_bot")
async def admin_toggle_bot(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    current_status = await get_setting("bot_status", True)
    await set_setting("bot_status", not current_status)
    await admin_panel(callback)


@router.callback_query(F.data == "admin_set_wa_price")
async def admin_set_wa_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("নতুন WhatsApp প্রাইস লিখুন (যেমন: 0.75):")
    await state.set_state(AdminStates.waiting_for_wa_price)


@router.message(AdminStates.waiting_for_wa_price)
async def save_wa_price(message: Message, state: FSMContext):
    try:
        price = float(message.text)
        await set_setting("wa_price", price)
        await state.clear()
        await message.answer(f"✅ WhatsApp প্রাইস সেট হয়েছে: ${price}")
    except ValueError:
        await message.answer("❌ সঠিক সংখ্যা দিন:")


@router.callback_query(F.data == "admin_set_emoji")
async def admin_set_emoji(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("কাস্টম ইমোজি আইডি দিন:")
    await state.set_state(AdminStates.waiting_for_emoji)


@router.message(AdminStates.waiting_for_emoji)
async def save_emoji_id(message: Message, state: FSMContext):
    await set_setting("custom_emoji_id", message.text.strip())
    await state.clear()
    await message.answer("✅ কাস্টম ইমোজি আইডি সেভ হয়েছে!")


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("ব্রডকাস্ট মেসেজ লিখুন:")
    await state.set_state(AdminStates.waiting_for_broadcast)


@router.message(AdminStates.waiting_for_broadcast)
async def execute_broadcast(message: Message, state: FSMContext):
    text = message.text
    await state.clear()
    users = users_col.find({})
    success = 0
    async for user in users:
        try:
            await bot.send_message(user["user_id"], text)
            success += 1
        except Exception:
            pass
    await message.answer(f"✅ {success} জন ইউজারকে মেসেজ পাঠানো হয়েছে।")


@router.callback_query(F.data == "back_to_start")
async def back_to_start(callback: CallbackQuery):
    await callback.message.delete()
    await cmd_start(callback.message)


async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Railway Bot Online...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
