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


class DepositStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_txid = State()
    waiting_for_screenshot = State()


class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_colombia_price = State()
    waiting_for_usa_price = State()


@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    is_on = await get_setting("bot_status", True)
    if not is_on and user_id not in [ADMIN_ID, SUB_ADMIN_ID]:
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
            f"📊 **অ্যাডমিন ব্যালেন্স
