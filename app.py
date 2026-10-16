import asyncio
import logging
import os
import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from motor.motor_asyncio import AsyncIOMotorClient

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SMSBOWER_API_KEY = os.getenv("SMSBOWER_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")

# OTP যে গ্রুপে ফরওয়ার্ড হবে সেই গ্রুপের ID (যেমন: -100123456789)
OTP_GROUP_ID = int(os.getenv("OTP_GROUP_ID", "0"))
# =======================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

client = AsyncIOMotorClient(MONGO_URI)
db = client["sms_bot_db"]
users_col = db["users"]
settings_col = db["settings"]
active_numbers_col = db["active_numbers"]


async def get_setting(key, default):
    setting = await settings_col.find_one({"key": key})
    return setting["value"] if setting else default


async def set_setting(key, value):
    await settings_col.update_one(
        {"key": key}, {"$set": {"value": value}}, upsert=True
    )


# Main Reply Keyboard Setup
def get_main_keyboard(user_id: int):
    buttons = [
        [
            KeyboardButton(text="🛒 Buy Number"),
            KeyboardButton(text="💰 Balance"),
        ],
        [KeyboardButton(text="📥 Deposit")],
    ]
    if user_id == ADMIN_ID:
        buttons.append([KeyboardButton(text="⚙️ Admin Panel")])

    return ReplyKeyboardMarkup(
        keyboard=buttons, resize_keyboard=True, persistent=True
    )


class DepositStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_txid = State()
    waiting_for_screenshot = State()


class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_wa_price = State()


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

    await message.answer(
        "স্বাগতম! আমাদের বট থেকে খুব সহজে ভার্চুয়াল নম্বর কিনতে পারেন:",
        reply_markup=get_main_keyboard(user_id),
    )


# ==================== BALANCE SYSTEM ====================
@router.message(F.text == "💰 Balance")
async def show_balance(message: Message):
    user_id = message.from_user.id
    user = await users_col.find_one({"user_id": user_id})
    user_bal = user.get("balance", 0.0) if user else 0.0

    if user_id == ADMIN_ID:
        # Fetch Web Balance from SMSBower API
        api_url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=getBalance"
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                web_resp = await resp.text()

        web_bal = (
            web_resp.split(":")[1]
            if web_resp.startswith("ACCESS_BALANCE")
            else "Error"
        )

        msg_text = (
            f"📊 **Admin Balance Dashboard:**\n\n"
            f"🌐 **Website Balance:** `${web_bal}`\n"
            f"🤖 **Bot Internal Balance:** `${user_bal:.2f}`"
        )
    else:
        msg_text = f"💰 **আপনার বর্তমান ব্যালেন্স:** `${user_bal:.2f}`"

    await message.answer(msg_text, parse_mode="MARKDOWN")


# ==================== BUY NUMBER & OTP HOLD ====================
@router.message(F.text == "🛒 Buy Number")
async def buy_number_menu(message: Message):
    wa_price = float(await get_setting("wa_price", 0.095))
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📱 WhatsApp Colombia (${wa_price})",
                    callback_data="buy_colombia_wa",
                )
            ]
        ]
    )
    await message.answer(
        "সার্ভিস নির্বাচন করুন:", reply_markup=keyboard
    )


@router.callback_query(F.data == "buy_colombia_wa")
async def process_buy_colombia(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await users_col.find_one({"user_id": user_id})
    wa_price = float(await get_setting("wa_price", 0.095))

    # Check Max Price Lock (Strictly <= 0.095$)
    if wa_price > 0.095:
        await callback.answer(
            "❌ প্রাইস লক লিমিট অতিক্রম করেছে! সার্ভিস উপলব্ধ নেই।",
            show_alert=True,
        )
        return

    if not user or user.get("balance", 0.0) < wa_price:
        await callback.answer(
            "❌ আপনার পর্যাপ্ত ব্যালেন্স নেই। ডিপোজিট করুন।", show_alert=True
        )
        return

    # API Request for Colombia WhatsApp (Country=33, Service=wa)
    url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=getNumber&service=wa&country=33"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            response = await resp.text()

    if response.startswith("ACCESS_NUMBER"):
        _, act_id, phone = response.split(":")

        # Store active session into DB (Money is HOLD, not deducted yet)
        await active_numbers_col.insert_one(
            {
                "user_id": user_id,
                "activation_id": act_id,
                "phone": phone,
                "price": wa_price,
                "status": "WAITING_OTP",
            }
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Cancel Order",
                        callback_data=f"cancel_act_{act_id}",
                    )
                ]
            ]
        )

        await callback.message.edit_text(
            f"✅ **নম্বর সফলভাবে পাওয়া গেছে!**\n\n"
            f"🇨🇴 **Country:** Colombia\n"
            f"📞 **Number:** `+{phone}`\n"
            f"🆔 **ID:** `{act_id}`\n\n"
            f"⏳ **OTP-এর জন্য অপেক্ষা করা হচ্ছে...**\n"
            f"*(OTP পাওয়ার পরই কেবল আপনার অ্যাকাউন্ট থেকে ${wa_price} কাটা হবে)*",
            reply_markup=keyboard,
            parse_mode="MARKDOWN",
        )

        # Background Task to check OTP
        asyncio.create_task(
            poll_for_otp(user_id, act_id, phone, wa_price, callback.message)
        )

    else:
        await callback.answer(
            "⚠️ বর্তমানে এই মূল্যে স্টক আউট রয়েছে! পরে চেষ্টা করুন।",
            show_alert=True,
        )


# Background Task: Check OTP Status
async def poll_for_otp(
    user_id: int, act_id: str, phone: str, price: float, message: Message
):
    url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=getStatus&id={act_id}"

    for _ in range(60):  # Check for 5 minutes (every 5 seconds)
        await asyncio.sleep(5)

        # Check if cancelled manually
        act_record = await active_numbers_col.find_one({"activation_id": act_id})
        if not act_record or act_record.get("status") == "CANCELLED":
            return

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                res_text = await resp.text()

        if res_text.startswith("STATUS_OK"):
            otp_code = res_text.split(":")[1]

            # 1. Deduct Balance from User DB
            await users_col.update_one(
                {"user_id": user_id}, {"$inc": {"balance": -price}}
            )

            # 2. Update status in DB
            await active_numbers_col.update_one(
                {"activation_id": act_id}, {"$set": {"status": "COMPLETED"}}
            )

            # 3. Notify User
            await message.edit_text(
                f"🎉 **OTP রিসিভ হয়েছে!**\n\n"
                f"📞 **Number:** `+{phone}`\n"
                f"🔑 **OTP Code:** `{otp_code}`\n\n"
                f"✅ আপনার অ্যাকাউন্ট থেকে `${price}` কেটে নেওয়া হয়েছে।",
                parse_mode="MARKDOWN",
            )

            # 4. Forward OTP to Telegram Group
            if OTP_GROUP_ID != 0:
                try:
                    await bot.send_message(
                        OTP_GROUP_ID,
                        f"📢 **New OTP Received!**\n\n"
                        f"🇨🇴 **Country:** Colombia (WhatsApp)\n"
                        f"📞 **Number:** `+{phone}`\n"
                        f"🔑 **OTP:** `{otp_code}`",
                        parse_mode="MARKDOWN",
                    )
                except Exception as e:
                    logging.error(f"Group Forward Error: {e}")
            return

        elif res_text in ["STATUS_CANCEL", "NO_ACTIVATION"]:
            return


# Cancel Order Handler
@router.callback_query(F.data.startswith("cancel_act_"))
async def cancel_activation(callback: CallbackQuery):
    act_id = callback.data.split("_")[2]

    # API Request to Cancel Activation on SMSBower
    url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=setStatus&status=8&id={act_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            await resp.text()

    # Update Status in DB
    await active_numbers_col.update_one(
        {"activation_id": act_id}, {"$set": {"status": "CANCELLED"}}
    )

    await callback.message.edit_text(
        "❌ **অর্ডারটি সফলভাবে বাতিল করা হয়েছে।** ওয়েবসাইট থেকেও নম্বর রিলিজ করা হয়েছে।",
        parse_mode="MARKDOWN",
    )


# ==================== DEPOSIT SYSTEM ====================
@router.message(F.text == "📥 Deposit")
async def deposit_start(message: Message, state: FSMContext):
    await message.answer(
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
            f"📥 **Binance Deposit Info:**\n\n"
            f"🆔 **Binance Pay ID:** `{binance_id}`\n"
            f"💵 **Amount:** `${amount}`\n\n"
            f"টাকা পাঠানোর পর TxID বা অর্ডার আইডি লিখুন:"
        )
        await message.answer(text, parse_mode="MARKDOWN")
        await state.set_state(DepositStates.waiting_for_txid)
    except ValueError:
        await message.answer("❌ সঠিক সংখ্যা লিখুন:")


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
        "✅ ডিপোজিট রিকোয়েস্ট সাবমিট হয়েছে! অ্যাডমিন অ্যাপ্রুভ করবেন।"
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

    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=photo_id,
        caption=f"🔔 **নতুন ডিপোজিট!**\n\n👤 User: `{user_id}`\n💵 Amount: `${amount}`\n🔖 TxID: `{txid}`",
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
        user_id, f"🎉 ডিপোজিট অ্যাপ্রুভ হয়েছে! `${amount}` যোগ করা হয়েছে।"
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
    await bot.send_message(user_id, "❌ ডিপোজিট রিকোয়েস্টটি রিজেক্ট করা হয়েছে।")


# ==================== ADMIN PANEL ====================
@router.message(F.text == "⚙️ Admin Panel")
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    bot_status = await get_setting("bot_status", True)
    status_text = "🟢 ON" if bot_status else "🔴 OFF"
    wa_price = await get_setting("wa_price", 0.095)

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
        ]
    )
    await message.answer(
        "⚙️ **অ্যাডমিন প্যানেল:**", reply_markup=keyboard, parse_mode="MARKDOWN"
    )


@router.callback_query(F.data == "admin_toggle_bot")
async def admin_toggle_bot(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    current_status = await get_setting("bot_status", True)
    await set_setting("bot_status", not current_status)
    await callback.answer("বট স্ট্যাটাস আপডেট হয়েছে!")


@router.callback_query(F.data == "admin_set_wa_price")
async def admin_set_wa_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer(
        "নতুন WhatsApp প্রাইস লিখুন (সর্বোচ্চ limit: 0.095):"
    )
    await state.set_state(AdminStates.waiting_for_wa_price)


@router.message(AdminStates.waiting_for_wa_price)
async def save_wa_price(message: Message, state: FSMContext):
    try:
        price = float(message.text)
        if price > 0.095:
            await message.answer("❌ প্রাইস $0.095 এর বেশি হওয়া যাবে না।")
            return
        await set_setting("wa_price", price)
        await state.clear()
        await message.answer(f"✅ WhatsApp প্রাইস আপডেট করা হয়েছে: ${price}")
    except ValueError:
        await message.answer("❌ সঠিক সংখ্যা দিন:")


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("ব্রডকাস্ট মেসেজটি লিখুন:")
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


async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Railway Bot Running with Reply Keyboard...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
