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
            KeyboardButton(text="🛒 Nombor Kinun"),
            KeyboardButton(text="💰 Balance"),
        ],
        [KeyboardButton(text="📥 Deposit")],
    ]
    if user_id in [ADMIN_ID, SUB_ADMIN_ID]:
        buttons.append([KeyboardButton(text="⚙️ Admin Panel")])

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
                unjoined_list.append((f"📢 Channel {idx}", invite_link))
        except Exception as e:
            logging.error(f"Force Join Check Error for {ch_id}: {e}")

    if unjoined_list:
        return False, unjoined_list
    return True, []


async def send_force_join_message(message_or_query, unjoined_list):
    keyboard_buttons = []
    for title, link in unjoined_list:
        if link:
            keyboard_buttons.append([InlineKeyboardButton(text=f"{title}-e Join Korun", url=link)])
    
    keyboard_buttons.append([InlineKeyboardButton(text="✅ Verify Korun", callback_data="verify_join")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    msg_text = (
        "⚠️ **Amader bot use korte hole nicher channel/group-gulo te obosshoi join korte hobe!**\n\n"
        "Shobgula te join korar por **'Verify Korun'** button-এ click korun."
    )

    if isinstance(message_or_query, Message):
        await message_or_query.answer(msg_text, reply_markup=keyboard, parse_mode="MARKDOWN")
    elif isinstance(message_or_query, CallbackQuery):
        await message_or_query.answer("❌ Age channel-e join korun!", show_alert=True)
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
        await message.answer("⚠️ Bot ekhon maintenance-er jonno bondho royeche.")
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
        "👋 **Swagotom!** Amader bot theke khub sohoje virtual number kinte paren:",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="MARKDOWN",
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
        await callback.answer("✅ Verification shofol hoyeche!", show_alert=True)

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
            "🎉 **Verification shofol!** Ekhon bot use korte paren:",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="MARKDOWN",
        )
    else:
        await callback.answer("❌ Apni ekhono shob channel/group-e join korenni!", show_alert=True)


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
            else "Truți"
        )

        msg_text = (
            f"📊 **Admin Balance Dashboard:**\n\n"
            f"🌐 **Website Balance:** `${web_bal}`\n"
            f"🤖 **Bot Internal Balance:** `${user_bal:.2f}`"
        )
    else:
        msg_text = f"💰 **Apnar Bortoman Balance:** `${user_bal:.2f}`"

    await message.answer(msg_text, parse_mode="MARKDOWN")


# ==================== BUY NUMBER & BATCH QUEUE SYSTEM ====================
@router.message(F.text == "🛒 Nombor Kinun")
async def buy_number_menu(message: Message):
    user_id = message.from_user.id

    active_order = await active_numbers_col.find_one({"user_id": user_id, "status": "WAITING_OTP"})
    if active_order:
        await message.answer(
            "⚠️ **Apnar already ekta number kena royeche ebong OTP-er jonno wait korche!**\n\n"
            "Ager number-tar OTP na asha porjonto ba order cancel na kora porjonto apni notun kono number kinte parben na."
        )
        return

    col_price = float(await get_setting("wa_colombia_price", 0.11))
    usa_price = float(await get_setting("wa_usa_price", 0.134))

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📱 WhatsApp Colombia (${col_price})",
                    callback_data="select_qty_colombia",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📱 WhatsApp USA Virtual (${usa_price})",
                    callback_data="select_qty_usa",
                )
            ],
        ]
    )
    await message.answer("Sothik Service Select Korun:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("select_qty_"))
async def select_quantity_menu(callback: CallbackQuery):
    service = callback.data.split("_")[2]  # colombia OR usa
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1", callback_data=f"buy_qty_1_{service}"),
                InlineKeyboardButton(text="2", callback_data=f"buy_qty_2_{service}"),
                InlineKeyboardButton(text="3", callback_data=f"buy_qty_3_{service}"),
            ],
            [
                InlineKeyboardButton(text="4", callback_data=f"buy_qty_4_{service}"),
                InlineKeyboardButton(text="5", callback_data=f"buy_qty_5_{service}"),
            ]
        ]
    )
    await callback.message.edit_text("🔢 **Apni koita number nite can? (Sorboccho 5-ta):**", reply_markup=keyboard, parse_mode="MARKDOWN")


@router.callback_query(F.data.startswith("buy_qty_"))
async def process_batch_buy(callback: CallbackQuery):
    user_id = callback.from_user.id
    _, _, qty_str, service = callback.data.split("_")
    qty = int(qty_str)

    active_order = await active_numbers_col.find_one({"user_id": user_id, "status": "WAITING_OTP"})
    if active_order:
        await callback.answer("❌ Apnar already ekta choloman order royeche!", show_alert=True)
        return

    user = await users_col.find_one({"user_id": user_id})
    
    if service == "colombia":
        price_per_num = float(await get_setting("wa_colombia_price", 0.11))
        country_code = "33"
        country_name = "Colombia"
        max_p = 0.11
        api_url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=getNumber&service=wa&country={country_code}&maxPrice={max_p}"
    else:
        price_per_num = float(await get_setting("wa_usa_price", 0.134))
        country_code = "12"
        country_name = "USA Virtual"
        max_p = 0.134
        api_url = f"https://smsbower.page/stubs/handler_api.php?api_key={SMSBOWER_API_KEY}&action=getNumber&service=wa&country={country_code}&operator=cellular&maxPrice={max_p}"

    total_cost = price_per_num * qty

    if not user or user.get("balance", 0.0) < total_cost:
        await callback.answer(
            f"❌ Apnar porjapto balance nei. Minimum ${total_cost:.2f} lagbe. Deposit korun.", show_alert=True
        )
        return

    await callback.message.edit_text(
        f"⏳ **Order Queue-te dewa hoyeche...**\n\n"
        f"🌐 **Country:** {country_name}\n"
        f"📦 **Quantity:** {qty}-ta\n\n"
        f"Website-e number stock-e asha matro (Waiting for number queue) ek ek kore apnar kache chole ashbe. Anugroho kore opekkha korun..."
    )

    asyncio.create_task(
        fetch_and_queue_numbers(user_id, qty, api_url, price_per_num, country_name, callback.message)
    )


async def fetch_and_queue_numbers(user_id: int, target_qty: int, api_url: str, price: float, country_name: str, message: Message):
    obtained_count = 0

    while obtained_count < target_qty:
        # Check if user cancelled during waiting queue loop
        active_check = await active_numbers_col.find_one({"user_id": user_id, "status": "WAITING_OTP"})
        # If no active order and we haven't finished getting all quantities, user might have cancelled
        # Let's track active batch state properly or handle queue
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                response = await resp.text()

        if response.startswith("ACCESS_NUMBER"):
            obtained_count += 1
            _, act_id, phone = response.split(":")

            await active_numbers_col.insert_one(
                {
                    "user_id": user_id,
                    "activation_id": act_id,
                    "phone": phone,
                    "price": price,
                    "status": "WAITING_OTP",
                }
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Order Cancel Korun",
                            callback_data=f"cancel_act_{act_id}",
                        )
                    ]
                ]
            )

            msg = await bot.send_message(
                user_id,
                f"✅ **Number ({obtained_count}/{target_qty}) Paoya Geche!**\n\n"
                f"🇨🇴 **Country:** {country_name}\n"
                f"📞 **Number:** `+{phone}`\n"
                f"🆔 **ID:** `{act_id}`\n\n"
                f"⏳ **OTP-er jonno wait kora hocche...**\n"
                f"*(OTP paoya ba order cancel korar ag porjonto notun number kinte parben na)*",
                reply_markup=keyboard,
                parse_mode="MARKDOWN",
            )

            # Wait for OTP for this number before processing next one in batch
            await poll_for_otp(user_id, act_id, phone, price, msg, country_name=country_name)
            
            # Check if after polling it was cancelled
            final_check = await active_numbers_col.find_one({"activation_id": act_id})
            if final_check and final_check.get("status") == "CANCELLED":
                break
        else:
            # Stock out or waiting queue - check if user cancelled via custom flag or command, then sleep and retry
            await asyncio.sleep(5)


async def poll_for_otp(
    user_id: int,
    act_id: str,
    phone: str,
    price: float,
    message: Message,
    country_name: str = "Colombia",
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
                f"🎉 **OTP Recieve hoyeche!**\n\n"
                f"📞 **Number:** `+{phone}`\n"
                f"🔑 **OTP Code:** `{otp_code}`\n\n"
                f"✅ Apnar account theke `${price}` kete neya hoyeche.",
                parse_mode="MARKDOWN",
            )

            if OTP_GROUP_ID != 0:
                try:
                    await bot.send_message(
                        OTP_GROUP_ID,
                        f"📢 **Notun OTP Prapti!**\n\n"
                        f"🌐 **Country:** {country_name} (WhatsApp)\n"
                        f"📞 **Number:** `+{phone}`\n"
                        f"🔑 **OTP:** `{otp_code}`",
                        parse_mode="MARKDOWN",
                    )
                except Exception as e:
                    logging.error(f"Group Forward Error: {e}")
            return

        elif res_text in ["STATUS_CANCEL", "NO_ACTIVATION"]:
            await active_numbers_col.update_one(
                {"activation_id": act_id}, {"$set": {"status": "CANCELLED"}}
            )
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

    await callback.answer("Order cancel kora hoyeche!")
    await callback.message.answer("❌ Order-ti website theke o cancel kora hoyeche. Ekhon apni caile notun number kinte parben.")


# ==================== DEPOSIT SYSTEM ====================
@router.message(F.text == "📥 Deposit")
async def deposit_start(message: Message, state: FSMContext):
    await message.answer(
        "💵 Apni koto dollar deposit korte can?\n*(Minimum deposit 0.50$)*"
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
            f"Taka pathanor por Transaction ID (TxID) ba Order ID likhun:"
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
                    text="✅ Approve Korun",
                    callback_data=f"dep_app_{user_id}_{amount}",
                ),
                InlineKeyboardButton(
                    text="❌ Cancel Korun", callback_data=f"dep_rej_{user_id}"
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
        caption=callback.message.caption + f"\n\n✅ **Approved hoyeche**"
    )
    await bot.send_message(
        user_id, f"🎉 Deposit approve kora hoyeche! `${amount}` jog kora hoyeche."
    )


@router.callback_query(F.data.startswith("dep_rej_"))
async def reject_deposit(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    _, _, user_id_str = callback.data.split("_")
    user_id = int(user_id_str)

    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n❌ **Cancelled kora hoyeche**"
    )
    await bot.send_message(user_id, "❌ Deposit request-ti cancel kora hoyeche.")


# ==================== ADMIN PANEL ====================
@router.message(F.text == "⚙️ Admin Panel")
async def admin_panel(message: Message):
    user_id = message.from_user.id
    if user_id not in [ADMIN_ID, SUB_ADMIN_ID]:
        return

    bot_status = await get_setting("bot_status", True)
    status_text = "🟢 On" if bot_status else "🔴 Off"
    col_price = await get_setting("wa_colombia_price", 0.11)
    usa_price = await get_setting("wa_usa_price", 0.134)

    keyboard_buttons = [
        [
            InlineKeyboardButton(
                text=f"Bot Status: {status_text}",
                callback_data="admin_toggle_bot",
            )
        ]
    ]

    if user_id == ADMIN_ID:
        keyboard_buttons.extend([
            [
                InlineKeyboardButton(
                    text=f"🇨🇴 Colombia Price (${col_price})",
                    callback_data="admin_set_colombia_price",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🇺🇸 USA Virtual Price (${usa_price})",
                    callback_data="admin_set_usa_price",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 3ta Force Join Setup Korun",
                    callback_data="admin_manage_channels",
                )
            ],
        ])

    keyboard_buttons.append([
        InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    panel_title = "⚙️ **Main Admin Panel:**" if user_id == ADMIN_ID else "⚙️ **Sub-Admin Panel:**"
    await message.answer(
        f"{panel_title}", reply_markup=keyboard, parse_mode="MARKDOWN"
    )


@router.callback_query(F.data == "admin_toggle_bot")
async def admin_toggle_bot(callback: CallbackQuery):
    if callback.from_user.id not in [ADMIN_ID, SUB_ADMIN_ID]:
        return
    current_status = await get_setting("bot_status", True)
    await set_setting("bot_status", not current_status)
    await callback.answer("Bot status update kora hoyeche!")


@router.callback_query(F.data == "admin_set_colombia_price")
async def admin_set_col_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Notun **Colombia WhatsApp** price likhun:")
    await state.set_state(AdminStates.waiting_for_colombia_price)


@router.message(AdminStates.waiting_for_colombia_price)
async def save_col_price(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        price = float(message.text)
        await set_setting("wa_colombia_price", price)
        await state.clear()
        await message.answer(f"✅ Colombia-r price update kora hoyeche: ${price}")
    except ValueError:
        await message.answer("❌ Sothik shongkha din:")


@router.callback_query(F.data == "admin_set_usa_price")
async def admin_set_usa_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Notun **USA Virtual WhatsApp** price likhun:")
    await state.set_state(AdminStates.waiting_for_usa_price)


@router.message(AdminStates.waiting_for_usa_price)
async def save_usa_price(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        price = float(message.text)
        await set_setting("wa_usa_price", price)
        await state.clear()
        await message.answer(f"✅ USA Virtual-er price update kora hoyeche: ${price}")
    except ValueError:
        await message.answer("❌ Sothik shongkha din:")


@router.callback_query(F.data == "admin_manage_channels")
async def admin_manage_channels(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    channels = await get_setting("force_join_channels", [None, None, None])
    ch1 = channels[0] if len(channels) > 0 and channels[0] else "Set kora nei"
    ch2 = channels[1] if len(channels) > 1 and channels[1] else "Set kora nei"
    ch3 = channels[2] if len(channels) > 2 and channels[2] else "Set kora nei"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"1st Channel: {ch1}", callback_data="admin_set_ch_1")],
            [InlineKeyboardButton(text=f"2nd Channel: {ch2}", callback_data="admin_set_ch_2")],
            [InlineKeyboardButton(text=f"3rd Channel: {ch3}", callback_data="admin_set_ch_3")],
            [InlineKeyboardButton(text="🗑️ Shob Channel Remove Korun", callback_data="admin_clear_all_ch")],
        ]
    )

    await callback.message.answer(
        "📢 **Force Join Channel Management:**\n\nNicher button-gulo te click kore alada bhabe channel Chat ID add korun:\n*(Obosshoi bot-ke channel/group-e admin rakhte hobe)*",
        reply_markup=keyboard,
        parse_mode="MARKDOWN",
    )


@router.callback_query(F.data == "admin_set_ch_1")
async def set_ch_1(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("1st channel-er **Chat ID** pathan (Jemon: `-100123456789`):")
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
        await message.answer(f"✅ 1st channel set hoyeche: `{cid}`", parse_mode="MARKDOWN")
    except ValueError:
        await message.answer("❌ Sothik Chat ID din (Jemon: -100xxxxxxxx):")


@router.callback_query(F.data == "admin_set_ch_2")
async def set_ch_2(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("2nd channel-er **Chat ID** pathan (Jemon: `-100123456789`):")
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
        await message.answer(f"✅ 2nd channel set hoyeche: `{cid}`", parse_mode="MARKDOWN")
    except ValueError:
        await message.answer("❌ Sothik Chat ID din (Jemon: -100xxxxxxxx):")


@router.callback_query(F.data == "admin_set_ch_3")
async def set_ch_3(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("3rd channel-er **Chat ID** pathan (Jemon: `-100123456789`):")
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
        await message.answer(f"✅ 3rd channel set hoyeche: `{cid}`", parse_mode="MARKDOWN")
    except ValueError:
        await message.answer("❌ Sothik Chat ID din (Jemon: -100xxxxxxxx):")


@router.callback_query(F.data == "admin_clear_all_ch")
async def clear_all_channels(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await set_setting("force_join_channels", [])
    await callback.message.edit_text("✅ Shob force join channel muche phela hoyeche.")


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in [ADMIN_ID, SUB_ADMIN_ID]:
        return
    await callback.message.answer("Broadcast message-ti likhun:")
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
    await message.answer(f"✅ {success} jon user-er kache message pathano hoyeche.")


async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Railway Bot Running with Quantity Selection (up to 5) & Queue Support...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
