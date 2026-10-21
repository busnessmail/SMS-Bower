import asyncio
import logging
import os
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
stock_col = db["canada_stock"]  # Canada numbers local stock database


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


# ==================== MULTI-FORCE JOIN CHECK ====================
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
        "⚠️ **আমাদের বট ব্যবহার করতে হলে নিচের চ্যানেল/গ্রুপগুলোতে জয়েন করতে হবে!**\n\n"
        "সবগুলোতে জয়েন করার পর **'ভেরিফাই করুন'** বাটনে ক্লিক করুন।"
    )

    if isinstance(message_or_query, Message):
        await message_or_query.answer(msg_text, reply_markup=keyboard, parse_mode="MARKDOWN")
    elif isinstance(message_or_query, CallbackQuery):
        await message_or_query.answer("❌ আগে চ্যানেলে জয়েন করুন!", show_alert=True)
        try:
            await message_or_query.message.answer(msg_text, reply_markup=keyboard, parse_mode="MARKDOWN")
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


# ==================== STATES ====================
class DepositStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_txid = State()
    waiting_for_screenshot = State()


class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_canada_price = State()
    waiting_for_txt_file = State()
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
    await message.answer(f"💰 **আপনার বর্তমান ব্যালেন্স:** `${user_bal:.2f}`", parse_mode="MARKDOWN")


# ==================== BUY CANADA NUMBER ====================
@router.message(F.text == "🛒 নম্বর কিনুন")
async def buy_number_menu(message: Message):
    canada_price = float(await get_setting("wa_canada_price", 0.150))

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🇨🇦 হোয়াটসঅ্যাপ কানাডা (${canada_price:.3f})",
                    callback_data="buy_canada_wa",
                )
            ]
        ]
    )
    await message.answer("সার্ভিস নির্বাচন করুন:", reply_markup=keyboard)


@router.callback_query(F.data == "buy_canada_wa")
async def process_buy_canada(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await users_col.find_one({"user_id": user_id})
    price_to_charge = float(await get_setting("wa_canada_price", 0.150))

    if not user or user.get("balance", 0.0) < price_to_charge:
        await callback.answer(
            "❌ আপনার পর্যাপ্ত ব্যালেন্স নেই। ডিপোজিট করুন।", show_alert=True
        )
        return

    # Pop/Get available Canada number from stock
    stock_item = await stock_col.find_one_and_delete({"status": "AVAILABLE"})
    if not stock_item:
        await callback.answer(
            "⚠️ বর্তমানে স্টকে কোনো কানাডা নম্বর নেই! পরে চেষ্টা করুন।", show_alert=True
        )
        return

    phone = stock_item["phone"]
    act_id = str(stock_item["_id"])

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
        f"✅ **নম্বর সফলভাবে পাওয়া গেছে!**\n\n"
        f"🇨🇦 **দেশ:** কানাডা (Canada)\n"
        f"📞 **নম্বর:** `+{phone}`\n"
        f"🆔 **আইডি:** `{act_id}`\n\n"
        f"⏳ **ওটিপি (OTP)-এর জন্য অপেক্ষা করা হচ্ছে...**\n"
        f"*(ওটিপি পাওয়ার পরই কেবল আপনার অ্যাকাউন্ট থেকে ${price_to_charge} কাটা হবে)*",
        reply_markup=keyboard,
        parse_mode="MARKDOWN",
    )

    asyncio.create_task(
        poll_for_canada_otp(
            user_id, act_id, phone, price_to_charge, callback.message
        )
    )


async def poll_for_canada_otp(
    user_id: int,
    act_id: str,
    phone: str,
    price: float,
    message: Message,
):
    for _ in range(60):  # Check every 5s for 5 minutes
        await asyncio.sleep(5)

        act_record = await active_numbers_col.find_one({"activation_id": act_id})
        if not act_record or act_record.get("status") in ["CANCELLED", "COMPLETED"]:
            return

        # Check if OTP has been added by admin or system
        if act_record.get("otp"):
            otp_code = act_record["otp"]

            # Charge User Balance
            await users_col.update_one(
                {"user_id": user_id}, {"$inc": {"balance": -price}}
            )

            await active_numbers_col.update_one(
                {"activation_id": act_id}, {"$set": {"status": "COMPLETED"}}
            )

            await message.edit_text(
                f"🎉 **ওটিপি (OTP) রিসিভ হয়েছে!**\n\n"
                f"🇨🇦 **দেশ:** কানাডা\n"
                f"📞 **নম্বর:** `+{phone}`\n"
                f"🔑 **ওটিপি কোড:** `{otp_code}`\n\n"
                f"✅ আপনার অ্যাকাউন্ট থেকে `${price}` কেটে নেওয়া হয়েছে।",
                parse_mode="MARKDOWN",
            )

            if OTP_GROUP_ID != 0:
                try:
                    await bot.send_message(
                        OTP_GROUP_ID,
                        f"📢 **নতুন ওটিপি প্রাপ্তি!**\n\n"
                        f"🌐 **দেশ:** কানাডা (WhatsApp)\n"
                        f"📞 **নম্বর:** `+{phone}`\n"
                        f"🔑 **ওটিপি:** `{otp_code}`",
                        parse_mode="MARKDOWN",
                    )
                except Exception as e:
                    logging.error(f"Group Forward Error: {e}")
            return


@router.callback_query(F.data.startswith("cancel_act_"))
async def cancel_activation(callback: CallbackQuery):
    act_id = callback.data.split("_")[2]

    act_record = await active_numbers_col.find_one({"activation_id": act_id})
    if act_record and act_record.get("status") == "WAITING_OTP":
        # Put back number to stock
        await stock_col.insert_one({
            "phone": act_record["phone"],
            "status": "AVAILABLE"
        })

    await active_numbers_col.update_one(
        {"activation_id": act_id}, {"$set": {"status": "CANCELLED"}}
    )

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.answer("অর্ডারটি বাতিল করা হয়েছে।", show_alert=True)


# ==================== DEPOSIT SYSTEM ====================
@router.message(F.text == "📥 ডিপোজিট")
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
            f"📥 **বাইন্যান্স ডিপোজিট তথ্য:**\n\n"
            f"🆔 **বাইন্যান্স পে আইডি:** `{binance_id}`\n"
            f"💵 **পরিমাণ:** `${amount}`\n\n"
            f"টাকা পাঠানোর পর ট্রানজেকশন আইডি (TxID) অথবা অর্ডার আইডি লিখুন:"
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
        "✅ ডিপোজিট রিকোয়েস্ট সাবমিট হয়েছে! অ্যাডমিন অনুমোদন করবেন।"
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
        caption=f"🔔 **নতুন ডিপোজিট!**\n\n👤 ইউজার: `{user_id}`\n💵 পরিমাণ: `${amount}`\n🔖 TxID: `{txid}`",
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
        caption=callback.message.caption + f"\n\n✅ **অনুমোদিত হয়েছে**"
    )
    await bot.send_message(
        user_id, f"🎉 ডিপোজিট অনুমোদন করা হয়েছে! `${amount}` যোগ করা হয়েছে।"
    )


@router.callback_query(F.data.startswith("dep_rej_"))
async def reject_deposit(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    _, _, user_id_str = callback.data.split("_")
    user_id = int(user_id_str)

    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n❌ **বাতিল করা হয়েছে**"
    )
    await bot.send_message(user_id, "❌ ডিপোজিট রিকোয়েস্টটি বাতিল করা হয়েছে।")


# ==================== ADMIN PANEL ====================
@router.message(F.text == "⚙️ অ্যাডমিন প্যানেল")
async def admin_panel(message: Message):
    user_id = message.from_user.id
    if user_id not in [ADMIN_ID, SUB_ADMIN_ID]:
        return

    bot_status = await get_setting("bot_status", True)
    status_text = "🟢 चालू" if bot_status else "🔴 বন্ধ"
    canada_price = await get_setting("wa_canada_price", 0.150)
    stock_count = await stock_col.count_documents({"status": "AVAILABLE"})

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
                    text=f"📁 টেক্সট ফাইল দিয়ে নম্বর আপলোড করুন (স্টক: {stock_count})",
                    callback_data="admin_upload_numbers",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🇨🇦 কানাডা প্রাইস সেট করুন (${canada_price})",
                    callback_data="admin_set_canada_price",
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
    panel_title = "⚙️ **মেইন অ্যাডমিন প্যানেল:**" if user_id == ADMIN_ID else "⚙️ **সাব-অ্যাডমিন প্যানেল:**"
    await message.answer(
        f"{panel_title}", reply_markup=keyboard, parse_mode="MARKDOWN"
    )


@router.callback_query(F.data == "admin_toggle_bot")
async def admin_toggle_bot(callback: CallbackQuery):
    if callback.from_user.id not in [ADMIN_ID, SUB_ADMIN_ID]:
        return
    current_status = await get_setting("bot_status", True)
    await set_setting("bot_status", not current_status)
    await callback.answer("বট স্ট্যাটাস আপডেট করা হয়েছে!", show_alert=True)


# --- TXT FILE NUMBER UPLOAD ---
@router.callback_query(F.data == "admin_upload_numbers")
async def admin_upload_numbers_prompt(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer(
        "📁 কানাডা নম্বর যোগ করতে একটি **.txt** ফাইল আপলোড করুন।\n\n"
        "ফাইল ফরম্যাট: প্রতি লাইনে একটি করে নম্বর লিখুন (যেমন: `15551234567`)\n",
        parse_mode="MARKDOWN"
    )
    await state.set_state(AdminStates.waiting_for_txt_file)


@router.message(AdminStates.waiting_for_txt_file, F.document)
async def save_uploaded_numbers(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    doc = message.document
    if not doc.file_name.endswith(".txt"):
        await message.answer("❌ শুধুমাত্র `.txt` ফাইল পাঠান।")
        return

    file_info = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file_info.file_path)
    content = file_bytes.read().decode("utf-8")

    lines = content.splitlines()
    added_count = 0

    for line in lines:
        phone = line.strip().replace("+", "")
        if phone:
            await stock_col.insert_one({
                "phone": phone,
                "status": "AVAILABLE"
            })
            added_count += 1

    await state.clear()
    await message.answer(f"✅ সফলভাবে `{added_count}` টি কানাডা নম্বর স্টকে যোগ করা হয়েছে!", parse_mode="MARKDOWN")


# --- SET CANADA PRICE ---
@router.callback_query(F.data == "admin_set_canada_price")
async def admin_set_can_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("নতুন **কানাডা হোয়াটসঅ্যাপ** প্রাইস লিখুন (যেমন: `0.15`):")
    await state.set_state(AdminStates.waiting_for_canada_price)


@router.message(AdminStates.waiting_for_canada_price)
async def save_can_price(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        price = float(message.text)
        await set_setting("wa_canada_price", price)
        await state.clear()
        await message.answer(f"✅ কানাডার প্রাইস আপডেট করা হয়েছে: ${price}")
    except ValueError:
        await message.answer("❌ সঠিক সংখ্যা দিন:")


# --- MULTI-CHANNEL FORCE JOIN MANAGEMENT ---
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
        "📢 **ফোর্স জয়েন চ্যানেল ম্যানেজমেন্ট:**\n\nনিচের বাটনগুলোতে ক্লিক করে আলাদাভাবে চ্যানেল Chat ID আইডি যোগ করুন:",
        reply_markup=keyboard,
        parse_mode="MARKDOWN",
    )


@router.callback_query(F.data == "admin_set_ch_1")
async def set_ch_1(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("১ম চ্যানেলের **Chat ID** পাঠান:")
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
        await message.answer(f"✅ ১ম চ্যানেল সেট হয়েছে: `{cid}`", parse_mode="MARKDOWN")
    except ValueError:
        await message.answer("❌ সঠিক Chat ID দিন:")


@router.callback_query(F.data == "admin_set_ch_2")
async def set_ch_2(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("২য় চ্যানেলের **Chat ID** পাঠান:")
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
        await message.answer(f"✅ ২য় চ্যানেল সেট হয়েছে: `{cid}`", parse_mode="MARKDOWN")
    except ValueError:
        await message.answer("❌ সঠিক Chat ID দিন:")


@router.callback_query(F.data == "admin_set_ch_3")
async def set_ch_3(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("৩য় চ্যানেলের **Chat ID** পাঠান:")
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
        await message.answer(f"✅ ৩য় চ্যানেল সেট হয়েছে: `{cid}`", parse_mode="MARKDOWN")
    except ValueError:
        await message.answer("❌ সঠিক Chat ID দিন:")


@router.callback_query(F.data == "admin_clear_all_ch")
async def clear_all_channels(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await set_setting("force_join_channels", [])
    await callback.message.edit_text("✅ সকল ফোর্স জয়েন চ্যানেল মুছে ফেলা হয়েছে।")


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
    await message.answer(f"✅ {success} জন ইউজারের কাছে মেসেজ পাঠানো হয়েছে।")


async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Bot Running with Only Canada Support...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
