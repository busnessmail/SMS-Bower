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
            "⚠️ Bot bortomane maintenance kajer jonno bondho ache."
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
        "Swagotom! Amader bot theke khub shohoje virtual number kinte paren:",
        reply_markup=get_main_keyboard(user_id),
    )


# ==================== BALANCE SYSTEM ====================
@router.message(F.text == "💰 Balance")
async def show_balance(message: Message):
    user_id = message.from_user.id
    user = await users_col.find_one({"user_id": user_id})
    user_bal = user.get("balance", 0.0) if user else 0.0

    if user_id == ADMIN_ID:
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
        msg_text = f"💰 **Apnar bortoman balance:** `${user_bal:.2f}`"

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
        "Service nirbachon korun:", reply_markup=keyboard
    )


@router.callback_query(F.data == "buy_colombia_wa")
async def process_buy_colombia(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await users_col.find_one({"user_id": user_id})
    wa_price = float(await get_setting("wa_price", 0.095))

    if not user or user.get("balance", 0.0) < wa_price:
        await callback.answer(
            "❌ Apnar porjapto balance nei. Deposit korun.", show_alert=True
        )
        return

    # Website API strict maxPrice=0.095 lock
    url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=getNumber&service=wa&country=33&maxPrice=0.095"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            response = await resp.text()

    if response.startswith("ACCESS_NUMBER"):
        _, act_id, phone = response.split(":")

        await active_numbers_col.insert_one(
            {
                "user_id": user_id,
                "activation_id": act_id,
                "phone": phone,
                "price": wa_price,  # User deduct amount = Admin set price
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
            f"✅ **Number shohoje paowa gese!**\n\n"
            f"🇨🇴 **Country:** Colombia\n"
            f"📞 **Number:** `+{phone}`\n"
            f"🆔 **ID:** `{act_id}`\n\n"
            f"⏳ **OTP-er jonno opekkha kora hocche...**\n"
            f"*(OTP paowar por-i kebol apnar account theke ${wa_price} kata hobe)*",
            reply_markup=keyboard,
            parse_mode="MARKDOWN",
        )

        asyncio.create_task(
            poll_for_otp(user_id, act_id, phone, wa_price, callback.message)
        )

    else:
        await callback.answer(
            "⚠️ Bortomane stock out royeche! Pore chesta korun.",
            show_alert=True,
        )


async def poll_for_otp(
    user_id: int, act_id: str, phone: str, price: float, message: Message
):
    url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=getStatus&id={act_id}"

    for _ in range(60):
        await asyncio.sleep(5)

        act_record = await active_numbers_col.find_one({"activation_id": act_id})
        if not act_record or act_record.get("status") == "CANCELLED":
            return

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                res_text = await resp.text()

        if res_text.startswith("STATUS_OK"):
            otp_code = res_text.split(":")[1]

            await users_col.update_one(
                {"user_id": user_id}, {"$inc": {"balance": -price}}
            )

            await active_numbers_col.update_one(
                {"activation_id": act_id}, {"$set": {"status": "COMPLETED"}}
            )

            await message.edit_text(
                f"🎉 **OTP receive hoyeche!**\n\n"
                f"📞 **Number:** `+{phone}`\n"
                f"🔑 **OTP Code:** `{otp_code}`\n\n"
                f"✅ Apnar account theke `${price}` kete neowa hoyeche.",
                parse_mode="MARKDOWN",
            )

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


@router.callback_query(F.data.startswith("cancel_act_"))
async def cancel_activation(callback: CallbackQuery):
    act_id = callback.data.split("_")[2]

    url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=setStatus&status=8&id={act_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            await resp.text()

    await active_numbers_col.update_one(
        {"activation_id": act_id}, {"$set": {"status": "CANCELLED"}}
    )

    await callback.message.edit_text(
        "❌ **Order-ti batil kora hoyeche.** Website theke-o number release kora hoyeche.",
        parse_mode="MARKDOWN",
    )


# ==================== DEPOSIT SYSTEM ====================
@router.message(F.text == "📥 Deposit")
async def deposit_start(message: Message, state: FSMContext):
    await message.answer(
        "💵 Apni koto dollar deposit korte chan?\n*(Nyunotomo deposit 0.50$)*"
    )
    await state.set_state(DepositStates.waiting_for_amount)


@router.message(DepositStates.waiting_for_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 0.5:
            await message.answer("❌ Minimum deposit 0.50$ hote hobe.")
            return

        await state.update_data(amount=amount)
        binance_id = await get_setting("binance_id", "123456789")

        text = (
            f"📥 **Binance Deposit Info:**\n\n"
            f"🆔 **Binance Pay ID:** `{binance_id}`\n"
            f"💵 **Amount:** `${amount}`\n\n"
            f"Taka pathanor por TxID ba Order ID likhun:"
        )
        await message.answer(text, parse_mode="MARKDOWN")
        await state.set_state(DepositStates.waiting_for_txid)
    except ValueError:
        await message.answer("❌ Sothik shongkha likhun:")


@router.message(DepositStates.waiting_for_txid)
async def process_deposit_txid(message: Message, state: FSMContext):
    await state.update_data(txid=message.text)
    await message.answer("📸 Ekhon payment-er screenshot din:")
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
        "✅ Deposit request submit hoyeche! Admin approve korben."
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
        caption=f"🔔 **Notun Deposit!**\n\n👤 User: `{user_id}`\n💵 Amount: `${amount}`\n🔖 TxID: `{txid}`",
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
        user_id, f"🎉 Deposit approve hoyeche! `${amount}` jog kora hoyeche."
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
    await bot.send_message(user_id, "❌ Deposit request-ti reject kora hoyeche.")


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
        "⚙️ **Admin Panel:**", reply_markup=keyboard, parse_mode="MARKDOWN"
    )


@router.callback_query(F.data == "admin_toggle_bot")
async def admin_toggle_bot(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    current_status = await get_setting("bot_status", True)
    await set_setting("bot_status", not current_status)
    await callback.answer("Bot status update hoyeche!")


@router.callback_query(F.data == "admin_set_wa_price")
async def admin_set_wa_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer(
        "Notun WhatsApp price likhun (Jemon: 0.11 ba 0.15):"
    )
    await state.set_state(AdminStates.waiting_for_wa_price)


@router.message(AdminStates.waiting_for_wa_price)
async def save_wa_price(message: Message, state: FSMContext):
    try:
        price = float(message.text)
        await set_setting("wa_price", price)
        await state.clear()
        await message.answer(f"✅ WhatsApp price update kora hoyeche: ${price}")
    except ValueError:
        await message.answer("❌ Sothik shongkha din:")


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Broadcast message-ti likhun:")
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
    await message.answer(f"✅ {success} jon user-ke message pathano hoyeche.")


async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Railway Bot Running with Custom Price Logic...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
