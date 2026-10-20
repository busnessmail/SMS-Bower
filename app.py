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
SUB_ADMIN_ID = int(os.getenv("SUB_ADMIN_ID", "0"))
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

# ==================== TELEGRAM PREMIUM EMOJI IDs ====================
def E(emoji_char, emoji_id):
    return f'<tg-emoji id="{emoji_id}">{emoji_char}</tg-emoji>'

EM_CART = E("🛒", "5231159755803761138")
EM_MONEY = E("💰", "5197434882321567830")
EM_DEPOSIT = E("📥", "5206607081334906820")
EM_ADMIN = E("⚙️", "5427168083074628963")
EM_CHECK = E("✅", "5217822164362739968")
EM_CROSS = E("❌", "5240241223632954241")
EM_WARN = E("⚠️", "5253742260054409879")
EM_PHONE = E("📱", "5337080053119336309")
EM_CLOCK = E("⏳", "5337172996211648018")
EM_DOLLAR = E("$", "5334637951894722661")
EM_COLOMBIA = E("🇨🇴", "5780829794899858599")
EM_USA = E("🇺🇸", "5334530732331143967")
EM_GIFT = E("🎉", "5231484622835063911")
EM_KEY = E("🔑", "5193177581888755275")
EM_ID = E("🆔", "529411658396895748")


async def get_setting(key, default):
    setting = await settings_col.find_one({"key": key})
    return setting["value"] if setting else default


async def set_setting(key, value):
    await settings_col.update_one(
        {"key": key}, {"$set": {"value": value}}, upsert=True
    )


def get_main_keyboard(user_id: int):
    # Reply Keyboard-এ শুধু সাধারণ ইমোজি ব্যবহার করা হয়েছে (বট সচল রাখার জন্য জরুরি)
    buttons = [
        [
            KeyboardButton(text="🛒 নম্বর কিনুন"),
            KeyboardButton(text="💰 ব্যালেন্স"),
        ],
        [KeyboardButton(text="📥 ডিপোজিট")],
    ]
    if user_id in [ADMIN_ID, SUB_ADMIN_ID]:
        buttons.append([KeyboardButton(text="⚙️ অ্যাডমিন প্যানেল")])

    return ReplyKeyboardMarkup(
        keyboard=buttons, resize_keyboard=True, persistent=True
    )


async def check_all_force_joins(user_id: int) -> tuple[bool, list]:
    if user_id in [ADMIN_ID, SUB_ADMIN_ID]:
        return True, []

    channels = await get_setting("force_join_channels", [])
    if not channels:
        return True, []

    unjoined_list = []

    for idx, ch_id in enumerate(channels, start=1):
        if not ch_id:
            continue
        try:
            member = await bot.get_chat_member(chat_id=int(ch_id), user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                chat_info = await bot.get_chat(int(ch_id))
                invite_link = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)
                unjoined_list.append((f"📢 চ্যানেল {idx}", invite_link))
        except Exception as e:
            logging.error(f"Force Join Check Error for {ch_id}: {e}")

    if unjoined_list:
        return False, unjoined_list
    return True, []


async def send_force_join_message(message_or_query, unjoined_list):
    keyboard_buttons = []
    for title, link in unjoined_list:
        if link:
            keyboard_buttons.append([InlineKeyboardButton(text=f"{title}-এ জয়েন করুন", url=link)])
    
    keyboard_buttons.append([InlineKeyboardButton(text="✅ ভেরিফাই করুন", callback_data="verify_join")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    msg_text = (
        f"{EM_WARN} <b>আমাদের বট ব্যবহার করতে হলে নিচের চ্যানেল/গ্রুপগুলোতে জয়েন করতে হবে!</b>\n\n"
        f"সবগুলোতে জয়েন করার পর <b>'ভেরিফাই করুন'</b> বাটনে ক্লিক করুন।"
    )

    if isinstance(message_or_query, Message):
        await message_or_query.answer(msg_text, reply_markup=keyboard, parse_mode="HTML")
    elif isinstance(message_or_query, CallbackQuery):
        await message_or_query.answer("❌ আগে চ্যানেলে জয়েন করুন!", show_alert=True)
        try:
            await message_or_query.message.answer(msg_text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            pass


@router.message.middleware()
async def force_join_message_middleware(handler, event: Message, data):
    user_id = event.from_user.id
    if event.text and event.text.startswith("/start"):
        return await handler(event, data)

    is_joined, unjoined = await check_all_force_joins(user_id)
    if not is_joined:
        await send_force_join_message(event, unjoined)
        return
    return await handler(event, data)


@router.callback_query.middleware()
async def force_join_callback_middleware(handler, event: CallbackQuery, data):
    if event.data == "verify_join" or event.data.startswith("admin_") or event.data.startswith("dep_"):
        return await handler(event, data)

    user_id = event.from_user.id
    is_joined, unjoined = await check_all_force_joins(user_id)
    if not is_joined:
        await send_force_join_message(event, unjoined)
        return
    return await handler(event, data)


class DepositStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_txid = State()
    waiting_for_screenshot = State()


class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_colombia_price = State()
    waiting_for_usa_price = State()
    waiting_for_ch1 = State()
    waiting_for_ch2 = State()
    waiting_for_ch3 = State()


@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    is_on = await get_setting("bot_status", True)
    if not is_on and user_id not in [ADMIN_ID, SUB_ADMIN_ID]:
        await message.answer("⚠️ বট বর্তমানে মেইনটেন্যান্স কাজের জন্য বন্ধ রয়েছে।")
        return

    is_joined, unjoined = await check_all_force_joins(user_id)
    if not is_joined:
        await send_force_join_message(message, unjoined)
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


@router.callback_query(F.data == "verify_join")
async def verify_user_join(callback: CallbackQuery):
    user_id = callback.from_user.id
    is_joined, unjoined = await check_all_force_joins(user_id)

    if is_joined:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer("✅ ভেরিফিকেশন সফল হয়েছে!", show_alert=True)

        user = await users_col.find_one({"user_id": user_id})
        if not user:
            await users_col.insert_one(
                {
                    "user_id": user_id,
                    "balance": 0.0,
                    "username": callback.from_user.username,
                }
            )

        await callback.message.answer(
            "🎉 ভেরিফিকেশন সফল! এখন বট ব্যবহার করতে পারেন:",
            reply_markup=get_main_keyboard(user_id),
        )
    else:
        await callback.answer("❌ আপনি এখনো সকল চ্যানেল/গ্রুপে জয়েন করেননি!", show_alert=True)


# ==================== BALANCE SYSTEM ====================
@router.message(F.text == "💰 ব্যালেন্স")
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
            else "ত্রুটি"
        )

        msg_text = (
            f"📊 <b>অ্যাডমিন ব্যালেন্স ড্যাশবোর্ড:</b>\n\n"
            f"🌐 <b>ওয়েবসাইট ব্যালেন্স:</b> <code>{web_bal}</code> {EM_DOLLAR}\n"
            f"🤖 <b>বট ইন্টারনাল ব্যালেন্স:</b> <code>{user_bal:.2f}</code> {EM_DOLLAR}"
        )
    else:
        msg_text = f"{EM_MONEY} <b>আপনার বর্তমান ব্যালেন্স:</b> <code>{user_bal:.2f}</code> {EM_DOLLAR}"

    await message.answer(msg_text, parse_mode="HTML")


# ==================== BUY NUMBER & OTP HOLD ====================
@router.message(F.text == "🛒 নম্বর কিনুন")
async def buy_number_menu(message: Message):
    col_price = float(await get_setting("wa_colombia_price", 0.132))
    usa_price = float(await get_setting("wa_usa_price", 0.134))

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📱 হোয়াটসঅ্যাপ কলম্বিয়া (${col_price})",
                    callback_data="buy_colombia_wa",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📱 হোয়াটসঅ্যাপ USA Virtual (${usa_price})",
                    callback_data="buy_usa_wa",
                )
            ],
        ]
    )
    await message.answer("সার্ভিস নির্বাচন করুন:", reply_markup=keyboard)


async def has_active_order(user_id: int) -> bool:
    active = await active_numbers_col.find_one({"user_id": user_id, "status": "WAITING_OTP"})
    return active is not None


# --- কলম্বিয়া হোয়াটসঅ্যাপ ---
@router.callback_query(F.data == "buy_colombia_wa")
async def process_buy_colombia(callback: CallbackQuery):
    user_id = callback.from_user.id

    if await has_active_order(user_id):
        await callback.answer(
            "⚠️ আপনার ইতিমধ্যে একটি নম্বর কেনা আছে! OTP পাওয়ার পর বা অর্ডারটি বাতিল না করা পর্যন্ত নতুন নম্বর কিনতে পারবেন না।",
            show_alert=True
        )
        return

    user = await users_col.find_one({"user_id": user_id})
    price_to_charge = float(await get_setting("wa_colombia_price", 0.132))

    if not user or user.get("balance", 0.0) < price_to_charge:
        await callback.answer(
            "❌ আপনার পর্যাপ্ত ব্যালেন্স নেই। ডিপোজিট করুন।", show_alert=True
        )
        return

    url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=getNumber&service=wa&country=33&maxPrice=0.132"

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
                "price": price_to_charge,
                "status": "WAITING_OTP",
            }
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ অর্ডার বাতিল করুন",
                        callback_data=f"cancel_act_{act_id}",
                    )
                ]
            ]
        )

        await callback.message.edit_text(
            f"{EM_CHECK} <b>নম্বর সফলভাবে পাওয়া গেছে!</b>\n\n"
            f"{EM_COLOMBIA} <b>দেশ:</b> কলম্বিয়া\n"
            f"{EM_PHONE} <b>নম্বর:</b> <code>+{phone}</code>\n"
            f"{EM_ID} <b>আইডি:</b> <code>{act_id}</code>\n\n"
            f"{EM_CLOCK} <b>ওটিপি (OTP)-এর জন্য অপেক্ষা করা হচ্ছে...</b>\n"
            f"*(ওটিপি পাওয়ার পরই কেবল আপনার অ্যাকাউন্ট থেকে ${price_to_charge} {EM_DOLLAR} কাটা হবে)*",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

        asyncio.create_task(
            poll_for_otp(
                user_id, act_id, phone, price_to_charge, callback.message, country_name="কলম্বিয়া"
            )
        )
    else:
        await callback.answer(
            "⚠️ বর্তমানে স্টকে নম্বর নেই! পরে চেষ্টা করুন।",
            show_alert=True,
        )


# --- USA Virtual হোয়াটসঅ্যাপ ---
@router.callback_query(F.data == "buy_usa_wa")
async def process_buy_usa(callback: CallbackQuery):
    user_id = callback.from_user.id

    if await has_active_order(user_id):
        await callback.answer(
            "⚠️ আপনার ইতিমধ্যে একটি নম্বর কেনা আছে! OTP পাওয়ার পর বা অর্ডারটি বাতিল না করা পর্যন্ত নতুন নম্বর কিনতে পারবেন না।",
            show_alert=True
        )
        return

    user = await users_col.find_one({"user_id": user_id})
    price_to_charge = float(await get_setting("wa_usa_price", 0.134))

    if not user or user.get("balance", 0.0) < price_to_charge:
        await callback.answer(
            "❌ আপনার পর্যাপ্ত ব্যালেন্স নেই। ডিপোজিট করুন।", show_alert=True
        )
        return

    url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=getNumber&service=wa&country=12&operator=cellular&maxPrice=0.134"

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
                "price": price_to_charge,
                "status": "WAITING_OTP",
            }
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ অর্ডার বাতিল করুন",
                        callback_data=f"cancel_act_{act_id}",
                    )
                ]
            ]
        )

        await callback.message.edit_text(
            f"{EM_CHECK} <b>নম্বর সফলভাবে পাওয়া গেছে!</b>\n\n"
            f"{EM_USA} <b>দেশ:</b> USA Virtual\n"
            f"{EM_PHONE} <b>নম্বর:</b> <code>+{phone}</code>\n"
            f"{EM_ID} <b>আইডি:</b> <code>{act_id}</code>\n\n"
            f"{EM_CLOCK} <b>ওটিপি (OTP)-এর জন্য অপেক্ষা করা হচ্ছে...</b>\n"
            f"*(ওটিপি পাওয়ার পরই কেবল আপনার অ্যাকাউন্ট থেকে ${price_to_charge} {EM_DOLLAR} কাটা হবে)*",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

        asyncio.create_task(
            poll_for_otp(
                user_id, act_id, phone, price_to_charge, callback.message, country_name="USA Virtual"
            )
        )
    else:
        await callback.answer(
            "⚠️ বর্তমানে স্টকে নম্বর নেই! পরে চেষ্টা করুন।",
            show_alert=True,
        )


async def poll_for_otp(
    user_id: int,
    act_id: str,
    phone: str,
    price: float,
    message: Message,
    country_name: str = "কলম্বিয়া",
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
                f"{EM_GIFT} <b>ওটিপি (OTP) রিসিভ হয়েছে!</b>\n\n"
                f"{EM_PHONE} <b>নম্বর:</b> <code>+{phone}</code>\n"
                f"{EM_KEY} <b>ওটিপি কোড:</b> <code>{otp_code}</code>\n\n"
                f"{EM_CHECK} আপনার অ্যাকাউন্ট থেকে <code>${price}</code> {EM_DOLLAR} কেটে নেওয়া হয়েছে।",
                parse_mode="HTML",
            )

            if OTP_GROUP_ID != 0:
                try:
                    await bot.send_message(
                        OTP_GROUP_ID,
                        f"📢 <b>নতুন ওটিপি প্রাপ্তি!</b>\n\n"
                        f"🌐 <b>দেশ:</b> {country_name} (হোয়াটসঅ্যাপ)\n"
                        f"{EM_PHONE} <b>নম্বর:</b> <code>+{phone}</code>\n"
                        f"{EM_KEY} <b>ওটিপি:</b> <code>{otp_code}</code>",
                        parse_mode="HTML",
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

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.answer()
    await callback.message.answer(
        f"{EM_CHECK} অর্ডারটি সফলভাবে বাতিল করা হয়েছে। এখন আপনি নতুন নম্বর কিনতে পারবেন।",
        parse_mode="HTML"
    )


# ==================== DEPOSIT SYSTEM ====================
@router.message(F.text == "📥 ডিপোজিট")
async def deposit_start(message: Message, state: FSMContext):
    await message.answer(
        f"{EM_MONEY} আপনি কত ডলার ডিপোজিট করতে চান?\n*(ন্যূনতম ডিপোজিট ০.৫০$)*",
        parse_mode="HTML"
    )
    await state.set_state(DepositStates.waiting_for_amount)


@router.message(DepositStates.waiting_for_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 0.5:
            await message.answer(f"{EM_CROSS} মিনিমাম ডিপোজিট ০.৫০$ হতে হবে।", parse_mode="HTML")
            return

        await state.update_data(amount=amount)
        binance_id = await get_setting("binance_id", "123456789")

        text = (
            f"{EM_DEPOSIT} <b>বাইন্যান্স ডিপোজিট তথ্য:</b>\n\n"
            f"{EM_ID} <b>বাইন্যান্স পে আইডি:</b> <code>{binance_id}</code>\n"
            f"{EM_DOLLAR} <b>পরিমাণ:</b> <code>${amount}</code>\n\n"
            f"টাকা পাঠানোর পর ট্রানজেকশন আইডি (TxID) অথবা অর্ডার আইডি লিখুন:"
        )
        await message.answer(text, parse_mode="HTML")
        await state.set_state(DepositStates.waiting_for_txid)
    except ValueError:
        await message.answer(f"{EM_CROSS} সঠিক সংখ্যা লিখুন:", parse_mode="HTML")


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
        f"{EM_CHECK} ডিপোজিট রিকোয়েস্ট সাবমিট হয়েছে! অ্যাডমিন অনুমোদন করবেন।",
        parse_mode="HTML"
    )

    admin_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ অনুমোদন করুন",
                    callback_data=f"dep_app_{user_id}_{amount}",
                ),
                InlineKeyboardButton(
                    text="❌ বাতিল করুন", callback_data=f"dep_rej_{user_id}"
                ),
            ]
        ]
    )

    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=photo_id,
        caption=f"🔔 <b>নতুন ডিপোজিট!</b>\n\n👤 ইউজার: <code>{user_id}</code>\n{EM_DOLLAR} পরিমাণ: <code>${amount}</code>\n🔖 TxID: <code>{txid}</code>",
        reply_markup=admin_keyboard,
        parse_mode="HTML",
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
        caption=callback.message.caption + f"\n\n{EM_CHECK} <b>অনুমোদিত হয়েছে</b>",
        parse_mode="HTML"
    )
    await bot.send_message(
        user_id, f"{EM_GIFT} ডিপোজিট অনুমোদন করা হয়েছে! <code>${amount}</code> {EM_DOLLAR} যোগ করা হয়েছে।", parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("dep_rej_"))
async def reject_deposit(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    _, _, user_id_str = callback.data.split("_")
    user_id = int(user_id_str)

    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n{EM_CROSS} <b>বাতিল করা হয়েছে</b>",
        parse_mode="HTML"
    )
    await bot.send_message(user_id, f"{EM_CROSS} ডিপোজিট রিকোয়েস্টটি বাতিল করা হয়েছে।", parse_mode="HTML")


# ==================== ADMIN PANEL ====================
@router.message(F.text == "⚙️ অ্যাডমিন প্যানেল")
async def admin_panel(message: Message):
    user_id = message.from_user.id
    if user_id not in [ADMIN_ID, SUB_ADMIN_ID]:
        return

    bot_status = await get_setting("bot_status", True)
    status_text = "🟢 चालू" if bot_status else "🔴 বন্ধ"
    col_price = await get_setting("wa_colombia_price", 0.132)
    usa_price = await get_setting("wa_usa_price", 0.134)

    keyboard_buttons = [
        [
            InlineKeyboardButton(
                text=f"বট স্ট্যাটাস: {status_text}",
                callback_data="admin_toggle_bot",
            )
        ]
    ]

    if user_id == ADMIN_ID:
        keyboard_buttons.extend([
            [
                InlineKeyboardButton(
                    text=f"🇨🇴 কলম্বিয়া প্রাইস (${col_price})",
                    callback_data="admin_set_colombia_price",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🇺🇸 USA Virtual প্রাইস (${usa_price})",
                    callback_data="admin_set_usa_price",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 ৩টি ফোর্স জয়েন সেটআপ করুন",
                    callback_data="admin_manage_channels",
                )
            ],
        ])

    keyboard_buttons.append([
        InlineKeyboardButton(text="📢 ব্রডকাস্ট", callback_data="admin_broadcast")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    panel_title = f"{EM_ADMIN} <b>মেইন অ্যাডমিন প্যানেল:</b>" if user_id == ADMIN_ID else f"{EM_ADMIN} <b>সাব-অ্যাডমিন প্যানেল:</b>"
    await message.answer(
        f"{panel_title}", reply_markup=keyboard, parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_toggle_bot")
async def admin_toggle_bot(callback: CallbackQuery):
    if callback.from_user.id not in [ADMIN_ID, SUB_ADMIN_ID]:
        return
    current_status = await get_setting("bot_status", True)
    await set_setting("bot_status", not current_status)
    await callback.answer("বট স্ট্যাটাস আপডেট করা হয়েছে!")


@router.callback_query(F.data == "admin_set_colombia_price")
async def admin_set_col_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("নতুন <b>কলম্বিয়া হোয়াটসঅ্যাপ</b> প্রাইস লিখুন:", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_colombia_price)


@router.message(AdminStates.waiting_for_colombia_price)
async def save_col_price(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        price = float(message.text)
        await set_setting("wa_colombia_price", price)
        await state.clear()
        await message.answer(f"{EM_CHECK} কলম্বিয়ার প্রাইস আপডেট করা হয়েছে: <code>${price}</code>", parse_mode="HTML")
    except ValueError:
        await message.answer(f"{EM_CROSS} সঠিক সংখ্যা দিন:", parse_mode="HTML")


@router.callback_query(F.data == "admin_set_usa_price")
async def admin_set_usa_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("নতুন <b>USA Virtual হোয়াটসঅ্যাপ</b> প্রাইস লিখুন:", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_usa_price)


@router.message(AdminStates.waiting_for_usa_price)
async def save_usa_price(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        price = float(message.text)
        await set_setting("wa_usa_price", price)
        await state.clear()
        await message.answer(f"{EM_CHECK} USA Virtual-এর প্রাইস আপডেট করা হয়েছে: <code>${price}</code>", parse_mode="HTML")
    except ValueError:
        await message.answer(f"{EM_CROSS} সঠিক সংখ্যা দিন:", parse_mode="HTML")


@router.callback_query(F.data == "admin_manage_channels")
async def admin_manage_channels(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    channels = await get_setting("force_join_channels", [None, None, None])
    ch1 = channels[0] if len(channels) > 0 and channels[0] else "সেট করা নেই"
    ch2 = channels[1] if len(channels) > 1 and channels[1] else "সেট করা নেই"
    ch3 = channels[2] if len(channels) > 2 and channels[2] else "সেট করা নেই"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"১ম চ্যানেল: {ch1}", callback_data="admin_set_ch_1")],
            [InlineKeyboardButton(text=f"২য় চ্যানেল: {ch2}", callback_data="admin_set_ch_2")],
            [InlineKeyboardButton(text=f"৩য় চ্যানেল: {ch3}", callback_data="admin_set_ch_3")],
            [InlineKeyboardButton(text="🗑️ সমস্ত চ্যানেল রিমুভ করুন", callback_data="admin_clear_all_ch")],
        ]
    )

    await callback.message.answer(
        "📢 <b>ফোর্স জয়েন চ্যানেল ম্যানেজমেন্ট:</b>\n\nনিচের বাটনগুলোতে ক্লিক করে আলাদাভাবে চ্যানেল Chat ID আইডি যোগ করুন:\n*(অবশ্যই বটকে উক্ত চ্যানেল/গ্রুপে অ্যাডমিন রাখতে হবে)*",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_set_ch_1")
async def set_ch_1(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("১ম চ্যানেলের <b>Chat ID</b> পাঠান (যেমন: <code>-100123456789</code>):", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_ch1)


@router.message(AdminStates.waiting_for_ch1)
async def save_ch_1(message: Message, state: FSMContext):
    try:
        cid = int(message.text.strip())
        channels = await get_setting("force_join_channels", [None, None, None])
        while len(channels) < 3:
            channels.append(None)
        channels[0] = cid
        await set_setting("force_join_channels", channels)
        await state.clear()
        await message.answer(f"{EM_CHECK} ১ম চ্যানেল সেট হয়েছে: <code>{cid}</code>", parse_mode="HTML")
    except ValueError:
        await message.answer(f"{EM_CROSS} সঠিক Chat ID দিন (যেমন: -100xxxxxxxx):", parse_mode="HTML")


@router.callback_query(F.data == "admin_set_ch_2")
async def set_ch_2(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("২য় চ্যানেলের <b>Chat ID</b> পাঠান (যেমন: <code>-100123456789</code>):", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_ch2)


@router.message(AdminStates.waiting_for_ch2)
async def save_ch_2(message: Message, state: FSMContext):
    try:
        cid = int(message.text.strip())
        channels = await get_setting("force_join_channels", [None, None, None])
        while len(channels) < 3:
            channels.append(None)
        channels[1] = cid
        await set_setting("force_join_channels", channels)
        await state.clear()
        await message.answer(f"{EM_CHECK} ২য় চ্যানেল সেট হয়েছে: <code>{cid}</code>", parse_mode="HTML")
    except ValueError:
        await message.answer(f"{EM_CROSS} সঠিক Chat ID দিন (যেমন: -100xxxxxxxx):", parse_mode="HTML")


@router.callback_query(F.data == "admin_set_ch_3")
async def set_ch_3(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("৩য় চ্যানেলের <b>Chat ID</b> পাঠান (যেমন: <code>-100123456789</code>):", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_ch3)


@router.message(AdminStates.waiting_for_ch3)
async def save_ch_3(message: Message, state: FSMContext):
    try:
        cid = int(message.text.strip())
        channels = await get_setting("force_join_channels", [None, None, None])
        while len(channels) < 3:
            channels.append(None)
        channels[2] = cid
        await set_setting("force_join_channels", channels)
        await state.clear()
        await message.answer(f"{EM_CHECK} ৩য় চ্যানেল সেট হয়েছে: <code>{cid}</code>", parse_mode="HTML")
    except ValueError:
        await message.answer(f"{EM_CROSS} সঠিক Chat ID দিন (যেমন: -100xxxxxxxx):", parse_model="HTML")


@router.callback_query(F.data == "admin_clear_all_ch")
async def clear_all_channels(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await set_setting("force_join_channels", [])
    await callback.message.edit_text(f"{EM_CHECK} সকল ফোর্স জয়েন চ্যানেল মুছে ফেলা হয়েছে।", parse_mode="HTML")


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in [ADMIN_ID, SUB_ADMIN_ID]:
        return
    await callback.message.answer("ব্রডকাস্ট মেসেজটি লিখুন:")
    await state.set_state(AdminStates.waiting_for_broadcast)


@router.message(AdminStates.waiting_for_broadcast)
async def execute_broadcast(message: Message, state: FSMContext):
    if message.from_user.id not in [ADMIN_ID, SUB_ADMIN_ID]:
        return
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
    await message.answer(f"{EM_CHECK} {success} জন ইউজারের কাছে মেসেজ পাঠানো হয়েছে।", parse_mode="HTML")


async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Railway Bot Running Perfectly...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
