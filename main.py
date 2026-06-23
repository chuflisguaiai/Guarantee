import asyncio
import logging
import os
import uuid
import aiosqlite
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

# ── Конфигурация ──────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = (
    list(map(int, os.environ["ADMIN_IDS"].split(",")))
    if os.environ.get("ADMIN_IDS")
    else []
)
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "@FunPayTgHelper")
COMMISSION_PERCENT = float(os.environ.get("COMMISSION_PERCENT", "3"))
DB_PATH = "deals.db"
LOGO_PATH = os.path.join(os.path.dirname(__file__), "funpay_logo.jpg")

# ── Логирование ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Dispatcher ────────────────────────────────────────────────────────────────

dp = Dispatcher(storage=MemoryStorage())

# ── Статусы сделок ────────────────────────────────────────────────────────────

STATUS_WAITING   = "waiting"
STATUS_ACTIVE    = "active"
STATUS_PAID      = "paid"
STATUS_DISPUTE   = "dispute"
STATUS_DONE      = "done"
STATUS_CANCELLED = "cancelled"

STATUS_LABEL = {
    STATUS_WAITING:   "⏳ Ожидание участника",
    STATUS_ACTIVE:    "🔵 Активна",
    STATUS_PAID:      "💰 Оплата подтверждена",
    STATUS_DISPUTE:   "⚠️ Спор открыт",
    STATUS_DONE:      "✅ Завершена",
    STATUS_CANCELLED: "❌ Отменена",
}

# ── FSM-состояния ─────────────────────────────────────────────────────────────

class CreateDeal(StatesGroup):
    amount      = State()
    description = State()

class SetRequisite(StatesGroup):
    ton            = State()
    card           = State()
    stars_username = State()
    cny            = State()
    thb            = State()
    usdt           = State()

# ── База данных ───────────────────────────────────────────────────────────────

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                id           TEXT PRIMARY KEY,
                creator_id   INTEGER NOT NULL,
                partner_id   INTEGER,
                creator_role TEXT NOT NULL,
                amount       REAL NOT NULL,
                description  TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'waiting',
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS balances (
                user_id  INTEGER PRIMARY KEY,
                balance  REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS requisites (
                user_id        INTEGER PRIMARY KEY,
                ton            TEXT,
                card           TEXT,
                stars_username TEXT,
                cny            TEXT,
                thb            TEXT,
                usdt           TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                created_at  TEXT NOT NULL
            )
        """)
        await db.commit()


async def get_balance(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM balances WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0.0


async def add_balance(user_id: int, amount: float) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO balances (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
        """, (user_id, amount))
        await db.commit()
        async with db.execute(
            "SELECT balance FROM balances WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0.0


async def deduct_balance(user_id: int, amount: float) -> bool:
    """Списывает сумму с баланса. Возвращает True если успешно, False если недостаточно средств."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM balances WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            current = row[0] if row else 0.0
        if current < amount:
            return False
        await db.execute(
            "UPDATE balances SET balance = balance - ? WHERE user_id=?",
            (amount, user_id)
        )
        await db.commit()
        return True


async def create_deal(
    deal_id: str,
    creator_id: int,
    creator_role: str,
    amount: float,
    description: str,
) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?)",
            (deal_id, creator_id, None, creator_role, amount, description, STATUS_WAITING, now, now),
        )
        await db.commit()


async def get_deal(deal_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_deal(deal_id: str, **fields) -> None:
    fields["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [deal_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE deals SET {set_clause} WHERE id=?", values)
        await db.commit()


async def get_user_deals(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM deals WHERE creator_id=? OR partner_id=?"
            " ORDER BY created_at DESC LIMIT 10",
            (user_id, user_id),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_disputed_deals() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM deals WHERE status=?", (STATUS_DISPUTE,)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_requisites(user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM requisites WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
            return {"user_id": user_id, "ton": None, "card": None,
                    "stars_username": None, "cny": None, "thb": None, "usdt": None}


async def set_requisite(user_id: int, field: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"""
            INSERT INTO requisites (user_id, {field}) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET {field} = excluded.{field}
        """, (user_id, value))
        await db.commit()


async def add_referral(referrer_id: int, referred_id: int) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO referrals (referrer_id, referred_id, created_at) VALUES (?,?,?)",
                (referrer_id, referred_id, datetime.utcnow().isoformat())
            )
            await db.commit()
            return True
    except Exception:
        return False


async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def is_referred(referred_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM referrals WHERE referred_id=?", (referred_id,)
        ) as cur:
            return await cur.fetchone() is not None

# ── Клавиатуры ────────────────────────────────────────────────────────────────

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 Создать сделку",    callback_data="create_deal")],
        [
            InlineKeyboardButton(text="🧳 Мои сделки",    callback_data="my_deals"),
            InlineKeyboardButton(text="💠 Рефералы",       callback_data="referrals"),
        ],
        [
            InlineKeyboardButton(text="ℹ️ Подробнее",     callback_data="howto"),
            InlineKeyboardButton(text="💳 Реквизиты",      callback_data="requisites"),
        ],
        [
            InlineKeyboardButton(text="🌐 Язык",           callback_data="language"),
            InlineKeyboardButton(text="👤 Поддержка",      callback_data="support"),
        ],
    ])


def kb_role() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Я покупатель", callback_data="role_buyer")],
        [InlineKeyboardButton(text="💼 Я продавец",   callback_data="role_seller")],
        [InlineKeyboardButton(text="❌ Отмена",         callback_data="cancel_create")],
    ])


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])


def kb_requisites() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Изменить TON",          callback_data="req_ton")],
        [InlineKeyboardButton(text="💳 Изменить карту",         callback_data="req_card")],
        [InlineKeyboardButton(text="⭐ Изменить Stars юзернейм", callback_data="req_stars")],
        [InlineKeyboardButton(text="🀄 Изменить CNY",           callback_data="req_cny")],
        [InlineKeyboardButton(text="🀄 Изменить THB",           callback_data="req_thb")],
        [InlineKeyboardButton(text="💵 Изменить USDT",          callback_data="req_usdt")],
        [InlineKeyboardButton(text="↩️ Вернуться в меню",      callback_data="main_menu")],
    ])


def kb_language() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Русский ✅",   callback_data="lang_ru")],
        [InlineKeyboardButton(text="🇬🇧 English",     callback_data="lang_en")],
        [InlineKeyboardButton(text="↩️ Назад",        callback_data="main_menu")],
    ])


def _is_buyer(deal: dict, user_id: int) -> bool:
    return (
        (deal["creator_role"] == "buyer"  and deal["creator_id"] == user_id) or
        (deal["creator_role"] == "seller" and deal["partner_id"] == user_id)
    )


def _is_seller(deal: dict, user_id: int) -> bool:
    return (
        (deal["creator_role"] == "seller" and deal["creator_id"] == user_id) or
        (deal["creator_role"] == "buyer"  and deal["partner_id"] == user_id)
    )


def kb_deal_actions(deal: dict, user_id: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    status = deal["status"]
    buyer  = _is_buyer(deal, user_id)
    seller = _is_seller(deal, user_id)

    if status == STATUS_ACTIVE:
        if buyer:
            buttons.append([InlineKeyboardButton(
                text="💰 Подтвердить оплату", callback_data=f"pay_{deal['id']}")])
        if seller:
            buttons.append([InlineKeyboardButton(
                text="📦 Подтвердить передачу товара", callback_data=f"deliver_{deal['id']}")])
        buttons.append([InlineKeyboardButton(
            text="⚠️ Открыть спор", callback_data=f"dispute_{deal['id']}")])
        buttons.append([InlineKeyboardButton(
            text="❌ Отменить сделку", callback_data=f"cancel_deal_{deal['id']}")])

    elif status == STATUS_PAID:
        if seller:
            buttons.append([InlineKeyboardButton(
                text="📦 Подтвердить передачу товара", callback_data=f"deliver_{deal['id']}")])
        buttons.append([InlineKeyboardButton(
            text="⚠️ Открыть спор", callback_data=f"dispute_{deal['id']}")])

    buttons.append([InlineKeyboardButton(
        text="🔄 Обновить статус", callback_data=f"refresh_{deal['id']}")])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_admin_resolve(deal_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Завершить (покупатель прав)", callback_data=f"adm_done_{deal_id}")],
        [InlineKeyboardButton(
            text="↩️ Отменить (продавец прав)",   callback_data=f"adm_cancel_{deal_id}")],
    ])

# ── Вспомогательные функции ───────────────────────────────────────────────────

def deal_card(deal: dict, show_link: bool = False, bot_username: str = "") -> str:
    creator_label = "покупатель" if deal["creator_role"] == "buyer" else "продавец"
    partner_label = f"ID {deal['partner_id']}" if deal["partner_id"] else "—"
    commission = deal["amount"] * COMMISSION_PERCENT / 100
    text = (
        f"<b>🤝 Сделка #{deal['id'][:8].upper()}</b>\n\n"
        f"💰 <b>Сумма:</b> {deal['amount']:,.2f} ₽\n"
        f"📊 <b>Комиссия ({COMMISSION_PERCENT}%):</b> {commission:,.2f} ₽\n"
        f"📝 <b>Описание:</b> {deal['description']}\n"
        f"👤 <b>Инициатор ({creator_label}):</b> ID {deal['creator_id']}\n"
        f"👥 <b>Второй участник:</b> {partner_label}\n"
        f"📊 <b>Статус:</b> {STATUS_LABEL[deal['status']]}\n"
        f"📅 <b>Создана:</b> {deal['created_at'][:16].replace('T', ' ')} UTC\n"
    )
    if show_link and bot_username:
        text += (
            f"\n🔗 <b>Ссылка для второго участника:</b>\n"
            f"<code>https://t.me/{bot_username}?start=join_{deal['id']}</code>"
        )
    return text


async def notify(bot: Bot, user_id: int | None, text: str) -> None:
    if user_id:
        try:
            await bot.send_message(user_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def notify_both(bot: Bot, deal: dict, text: str, skip_id: int | None = None) -> None:
    for uid in (deal["creator_id"], deal["partner_id"]):
        if uid and uid != skip_id:
            await notify(bot, uid, text)


async def notify_admins(bot: Bot, text: str, reply_markup=None) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id, text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except Exception:
            pass


async def safe_edit_text(message: Message, text: str, reply_markup=None, parse_mode=ParseMode.HTML) -> None:
    """edit_text — но если сообщение содержит фото, отправляем новым сообщением."""
    try:
        await message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "there is no text in the message" in str(e) or "message can't be edited" in str(e):
            await message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
        else:
            raise


async def send_logo_with_text(message: Message, text: str, reply_markup=None) -> None:
    if os.path.exists(LOGO_PATH):
        try:
            await message.answer_photo(photo=FSInputFile(LOGO_PATH))
        except Exception:
            pass
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


def requisites_card(req: dict) -> str:
    def val(v):
        return v if v else "—"
    return (
        f"💳 <b>Ваши реквизиты:</b>\n\n"
        f"🌐 <b>TON:</b> {val(req.get('ton'))}\n"
        f"💳 <b>Карта/СБП:</b> {val(req.get('card'))}\n"
        f"⭐ <b>Stars юзернейм:</b> {val(req.get('stars_username'))}\n"
        f"🀄 <b>CNY:</b> {val(req.get('cny'))}\n"
        f"🀄 <b>THB:</b> {val(req.get('thb'))}\n"
        f"💵 <b>USDT:</b> {val(req.get('usdt'))}\n"
    )

# ── /start ────────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    args = message.text.split(maxsplit=1)

    # Присоединение к сделке
    if len(args) > 1 and args[1].startswith("join_"):
        deal_id = args[1][5:]
        deal = await get_deal(deal_id)

        if not deal:
            await message.answer("❌ Сделка не найдена.", reply_markup=kb_back())
            return
        if deal["status"] != STATUS_WAITING:
            await message.answer("❌ Сделка уже недоступна для присоединения.", reply_markup=kb_back())
            return
        if deal["creator_id"] == message.from_user.id:
            await message.answer("❌ Нельзя присоединиться к собственной сделке.", reply_markup=kb_back())
            return

        await update_deal(deal_id, partner_id=message.from_user.id, status=STATUS_ACTIVE)
        deal = await get_deal(deal_id)

        await message.answer(
            f"✅ <b>Вы присоединились к сделке!</b>\n\n{deal_card(deal)}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_deal_actions(deal, message.from_user.id),
        )
        await notify(
            bot, deal["creator_id"],
            f"✅ <b>Второй участник присоединился к вашей сделке!</b>\n\n{deal_card(deal)}",
        )
        return

    # Реферальная ссылка
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_id = int(args[1][4:])
        if referrer_id != message.from_user.id and not await is_referred(message.from_user.id):
            added = await add_referral(referrer_id, message.from_user.id)
            if added:
                await notify(bot, referrer_id,
                    f"🎉 <b>По вашей реферальной ссылке зарегистрировался новый пользователь!</b>")

    welcome_text = (
        f"👋 <b>Добро пожаловать!</b>\n\n"
        f"🧳 <b>FunPay</b> — специализированный сервис по обеспечению "
        f"безопасности внебиржевых сделок.\n\n"
        f"✨ Автоматизированный алгоритм исполнения.\n"
        f"⚡ Скорость и автоматизация.\n"
        f"💳 Удобный и быстрый вывод средств.\n\n"
        f"<blockquote>• Комиссия сервиса: {COMMISSION_PERCENT}%\n"
        f"• Режим работы: 24/7\n"
        f"• Техническая поддержка: {SUPPORT_USERNAME}</blockquote>\n\n"
        f"🛡 <b>Выберите нужный раздел ниже:</b>"
    )
    await send_logo_with_text(message, welcome_text, reply_markup=kb_main())


@dp.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🏠 Главное меню:", reply_markup=kb_main())


@dp.message(Command("moneyk"))
async def cmd_moneyk(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "❌ Укажите сумму. Пример: <code>/moneyk 500</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        amount = float(parts[1].replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректную сумму. Пример: <code>/moneyk 500</code>", parse_mode=ParseMode.HTML)
        return

    new_balance = await add_balance(message.from_user.id, amount)
    await message.answer(
        f"✅ <b>Баланс пополнен на {amount:,.2f} ₽</b>\n\n"
        f"💳 Текущий баланс: <b>{new_balance:,.2f} ₽</b>",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    balance = await get_balance(message.from_user.id)
    await message.answer(
        f"💳 <b>Ваш баланс:</b> {balance:,.2f} ₽",
        parse_mode=ParseMode.HTML,
    )

# ── Создание сделки ───────────────────────────────────────────────────────────

@dp.callback_query(F.data == "create_deal")
async def cb_create_deal(call: CallbackQuery, state: FSMContext) -> None:
    await safe_edit_text(call.message,
        "💼 <b>Создание сделки</b>\n\nКем вы являетесь в этой сделке?",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_role(),
    )


@dp.callback_query(F.data == "cancel_create")
async def cb_cancel_create(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_text(call.message,"🏠 Главное меню:", reply_markup=kb_main())


@dp.callback_query(F.data.in_({"role_buyer", "role_seller"}))
async def cb_role(call: CallbackQuery, state: FSMContext) -> None:
    role = "buyer" if call.data == "role_buyer" else "seller"
    await state.update_data(role=role)
    await state.set_state(CreateDeal.amount)
    await call.message.delete()
    await send_logo_with_text(
        call.message,
        "💰 <b>Введите сумму сделки</b> (в рублях):\n\nПример: <code>1500</code>",
    )


@dp.message(CreateDeal.amount)
async def fsm_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = float(message.text.replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректную сумму (положительное число).")
        return

    await state.update_data(amount=amount)
    await state.set_state(CreateDeal.description)
    await message.answer(
        "📝 <b>Опишите предмет сделки</b> (товар, услуга, аккаунт и т.д.):",
        parse_mode=ParseMode.HTML,
    )


@dp.message(CreateDeal.description)
async def fsm_description(message: Message, state: FSMContext, bot: Bot) -> None:
    description = message.text.strip()
    if len(description) < 3:
        await message.answer("❌ Описание слишком короткое. Напишите подробнее.")
        return

    data = await state.get_data()
    deal_id = str(uuid.uuid4())
    await create_deal(deal_id, message.from_user.id, data["role"], data["amount"], description)
    await state.clear()

    deal = await get_deal(deal_id)
    me = await bot.get_me()
    await send_logo_with_text(
        message,
        f"✅ <b>Сделка создана!</b>\n\n"
        f"{deal_card(deal, show_link=True, bot_username=me.username)}\n\n"
        f"Отправьте ссылку второму участнику. Как только он перейдёт — сделка станет активной.",
        reply_markup=kb_back(),
    )

# ── Просмотр и обновление ─────────────────────────────────────────────────────

@dp.callback_query(F.data == "my_deals")
async def cb_my_deals(call: CallbackQuery) -> None:
    deals = await get_user_deals(call.from_user.id)
    if not deals:
        await safe_edit_text(call.message,
            "📋 У вас пока нет сделок.\n\nСоздайте первую!",
            reply_markup=kb_main(),
        )
        return

    buttons = []
    for d in deals:
        label = f"{STATUS_LABEL[d['status']]} | {d['amount']:,.0f}₽ | #{d['id'][:6].upper()}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"view_{d['id']}")])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])

    await safe_edit_text(call.message,
        "📋 <b>Ваши сделки (последние 10):</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("view_"))
async def cb_view_deal(call: CallbackQuery, bot: Bot) -> None:
    deal_id = call.data[5:]
    deal = await get_deal(deal_id)
    if not deal:
        await call.answer("❌ Сделка не найдена", show_alert=True)
        return
    me = await bot.get_me()
    await safe_edit_text(call.message,
        deal_card(deal, show_link=(deal["status"] == STATUS_WAITING), bot_username=me.username),
        parse_mode=ParseMode.HTML,
        reply_markup=kb_deal_actions(deal, call.from_user.id),
    )


@dp.callback_query(F.data.startswith("refresh_"))
async def cb_refresh(call: CallbackQuery, bot: Bot) -> None:
    deal_id = call.data[8:]
    deal = await get_deal(deal_id)
    if not deal:
        await call.answer("❌ Сделка не найдена", show_alert=True)
        return
    me = await bot.get_me()
    await safe_edit_text(call.message,
        deal_card(deal, show_link=(deal["status"] == STATUS_WAITING), bot_username=me.username),
        parse_mode=ParseMode.HTML,
        reply_markup=kb_deal_actions(deal, call.from_user.id),
    )
    await call.answer("Обновлено ✓")


@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_text(call.message,"🏠 Главное меню:", reply_markup=kb_main())


@dp.callback_query(F.data == "howto")
async def cb_howto(call: CallbackQuery) -> None:
    await safe_edit_text(call.message,
        "ℹ️ <b>Как работает бот-гарант:</b>\n\n"
        "1️⃣ <b>Создайте сделку</b> — укажите роль (покупатель/продавец), сумму и описание.\n\n"
        "2️⃣ <b>Пригласите второго участника</b> — отправьте ему ссылку. "
        "Когда он перейдёт — сделка становится активной.\n\n"
        "3️⃣ <b>Покупатель</b> переводит средства и нажимает «Подтвердить оплату».\n\n"
        "4️⃣ <b>Продавец</b> передаёт товар/услугу и нажимает «Подтвердить передачу».\n\n"
        "5️⃣ Сделка <b>завершена</b>! ✅\n\n"
        f"💰 <b>Комиссия сервиса:</b> {COMMISSION_PERCENT}%\n"
        f"🕐 <b>Режим работы:</b> 24/7\n"
        f"👤 <b>Поддержка:</b> {SUPPORT_USERNAME}\n\n"
        "⚠️ Если что-то пошло не так — нажмите «Открыть спор». "
        "Администратор рассмотрит ситуацию и вынесет решение.\n\n"
        "<i>Бот фиксирует каждый шаг и выступает нейтральным посредником.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_back(),
    )

# ── Реквизиты ─────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "requisites")
async def cb_requisites(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    req = await get_requisites(call.from_user.id)
    await safe_edit_text(call.message,
        requisites_card(req),
        parse_mode=ParseMode.HTML,
        reply_markup=kb_requisites(),
    )


@dp.callback_query(F.data == "req_ton")
async def cb_req_ton(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetRequisite.ton)
    await safe_edit_text(call.message,
        "🌐 <b>Введите ваш TON-адрес кошелька:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Вернуться в меню", callback_data="requisites")]
        ]),
    )


@dp.callback_query(F.data == "req_card")
async def cb_req_card(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetRequisite.card)
    await safe_edit_text(call.message,
        "💳 <b>Введите номер карты / телефона СБП</b> (10–19 цифр, можно +7):",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Вернуться в меню", callback_data="requisites")]
        ]),
    )


@dp.callback_query(F.data == "req_stars")
async def cb_req_stars(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetRequisite.stars_username)
    await safe_edit_text(call.message,
        "⭐ <b>Введите ваш Telegram юзернейм для получения Stars:</b>\n\n"
        "Пример: <code>@username</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Вернуться в меню", callback_data="requisites")]
        ]),
    )


@dp.callback_query(F.data == "req_cny")
async def cb_req_cny(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetRequisite.cny)
    await safe_edit_text(call.message,
        "🀄 <b>Введите ваши реквизиты для CNY (китайский юань):</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Вернуться в меню", callback_data="requisites")]
        ]),
    )


@dp.callback_query(F.data == "req_thb")
async def cb_req_thb(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetRequisite.thb)
    await safe_edit_text(call.message,
        "🀄 <b>Введите ваши реквизиты для THB (тайский бат):</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Вернуться в меню", callback_data="requisites")]
        ]),
    )


@dp.callback_query(F.data == "req_usdt")
async def cb_req_usdt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetRequisite.usdt)
    await safe_edit_text(call.message,
        "💵 <b>Введите ваш USDT-адрес кошелька (TRC-20 или ERC-20):</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Вернуться в меню", callback_data="requisites")]
        ]),
    )


@dp.message(SetRequisite.ton)
async def fsm_set_ton(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    await set_requisite(message.from_user.id, "ton", value)
    await state.clear()
    req = await get_requisites(message.from_user.id)
    await message.answer(
        f"✅ <b>TON-адрес сохранён!</b>\n\n{requisites_card(req)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_requisites(),
    )


@dp.message(SetRequisite.card)
async def fsm_set_card(message: Message, state: FSMContext) -> None:
    value = message.text.strip().replace(" ", "")
    digits = value.lstrip("+")
    if not digits.isdigit() or not (10 <= len(digits) <= 19):
        await message.answer(
            "❌ Некорректный номер. Введите 10–19 цифр (номер карты или телефона СБП)."
        )
        return
    await set_requisite(message.from_user.id, "card", value)
    await state.clear()
    req = await get_requisites(message.from_user.id)
    await message.answer(
        f"✅ <b>Карта/СБП сохранены!</b>\n\n{requisites_card(req)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_requisites(),
    )


@dp.message(SetRequisite.stars_username)
async def fsm_set_stars(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    if not value.startswith("@"):
        value = "@" + value
    await set_requisite(message.from_user.id, "stars_username", value)
    await state.clear()
    req = await get_requisites(message.from_user.id)
    await message.answer(
        f"✅ <b>Stars юзернейм сохранён!</b>\n\n{requisites_card(req)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_requisites(),
    )


@dp.message(SetRequisite.cny)
async def fsm_set_cny(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    await set_requisite(message.from_user.id, "cny", value)
    await state.clear()
    req = await get_requisites(message.from_user.id)
    await message.answer(
        f"✅ <b>CNY реквизиты сохранены!</b>\n\n{requisites_card(req)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_requisites(),
    )


@dp.message(SetRequisite.thb)
async def fsm_set_thb(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    await set_requisite(message.from_user.id, "thb", value)
    await state.clear()
    req = await get_requisites(message.from_user.id)
    await message.answer(
        f"✅ <b>THB реквизиты сохранены!</b>\n\n{requisites_card(req)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_requisites(),
    )


@dp.message(SetRequisite.usdt)
async def fsm_set_usdt(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    await set_requisite(message.from_user.id, "usdt", value)
    await state.clear()
    req = await get_requisites(message.from_user.id)
    await message.answer(
        f"✅ <b>USDT-адрес сохранён!</b>\n\n{requisites_card(req)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_requisites(),
    )

# ── Рефералы ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "referrals")
async def cb_referrals(call: CallbackQuery, bot: Bot) -> None:
    me = await bot.get_me()
    count = await get_referral_count(call.from_user.id)
    ref_link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"
    await safe_edit_text(call.message,
        f"💠 <b>Реферальная программа</b>\n\n"
        f"👥 <b>Ваших рефералов:</b> {count}\n\n"
        f"🔗 <b>Ваша реферальная ссылка:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"<i>Поделитесь ссылкой с друзьями. За каждого нового пользователя "
        f"вы получите уведомление!</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_back(),
    )

# ── Язык ──────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "language")
async def cb_language(call: CallbackQuery) -> None:
    await safe_edit_text(call.message,
        "🌐 <b>Выберите язык / Choose language:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_language(),
    )


@dp.callback_query(F.data.in_({"lang_ru", "lang_en"}))
async def cb_set_language(call: CallbackQuery) -> None:
    if call.data == "lang_ru":
        await call.answer("🇷🇺 Язык установлен: Русский", show_alert=False)
    else:
        await call.answer("🇬🇧 Language set: English", show_alert=False)
    await safe_edit_text(call.message,"🏠 Главное меню:", reply_markup=kb_main())

# ── Поддержка ─────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery) -> None:
    await safe_edit_text(call.message,
        f"👤 <b>Поддержка</b>\n\n"
        f"По всем вопросам обращайтесь к нашему менеджеру:\n"
        f"➡️ {SUPPORT_USERNAME}\n\n"
        f"<i>Время ответа: до 30 минут, режим работы 24/7</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_back(),
    )

# ── Действия со сделкой ───────────────────────────────────────────────────────

def kb_payment_retry(deal_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"pay_{deal_id}")],
        [InlineKeyboardButton(text="❌ Выйти из сделки",   callback_data=f"cancel_deal_{deal_id}")],
    ])


@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay(call: CallbackQuery, bot: Bot) -> None:
    deal_id = call.data[4:]
    deal = await get_deal(deal_id)
    if not deal or deal["status"] != STATUS_ACTIVE:
        await call.answer("❌ Действие недоступно", show_alert=True)
        return
    if not _is_buyer(deal, call.from_user.id):
        await call.answer("❌ Это действие доступно только покупателю", show_alert=True)
        return

    total = deal["amount"] + deal["amount"] * COMMISSION_PERCENT / 100
    paid = await deduct_balance(call.from_user.id, total)

    if not paid:
        await call.answer("❌ Недостаточно средств", show_alert=True)
        return

    await update_deal(deal_id, status=STATUS_PAID)
    deal = await get_deal(deal_id)
    commission = deal["amount"] * COMMISSION_PERCENT / 100

    await safe_edit_text(call.message,
        f"✅ <b>Оплата успешно проведена!</b>\n\n"
        f"🎁 Сделка <b>#{deal['id'][:8].upper()}</b>\n"
        f"🔗 Оплачено: <b>{deal['amount']:,.2f} ₽</b>\n"
        f"📊 Комиссия: <b>{commission:,.2f} ₽</b>\n"
        f"📝 За: <i>{deal['description']}</i>\n\n"
        f"Ожидайте передачи товара/услуги от продавца!",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_deal_actions(deal, call.from_user.id),
    )
    await notify_both(bot, deal, f"💰 <b>Покупатель подтвердил оплату!</b>\n\n{deal_card(deal)}", skip_id=call.from_user.id)


@dp.callback_query(F.data.startswith("deliver_"))
async def cb_deliver(call: CallbackQuery, bot: Bot) -> None:
    deal_id = call.data[8:]
    deal = await get_deal(deal_id)
    if not deal or deal["status"] not in (STATUS_ACTIVE, STATUS_PAID):
        await call.answer("❌ Действие недоступно", show_alert=True)
        return
    if not _is_seller(deal, call.from_user.id):
        await call.answer("❌ Это действие доступно только продавцу", show_alert=True)
        return

    await update_deal(deal_id, status=STATUS_DONE)
    deal = await get_deal(deal_id)

    await safe_edit_text(call.message,
        f"✅ <b>Сделка успешно завершена!</b>\n\n{deal_card(deal)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_back(),
    )
    await notify_both(bot, deal, f"✅ <b>Сделка завершена!</b>\n\n{deal_card(deal)}", skip_id=call.from_user.id)
    await notify_admins(bot, f"✅ Сделка #{deal_id[:8].upper()} завершена успешно.")


@dp.callback_query(F.data.startswith("dispute_"))
async def cb_dispute(call: CallbackQuery, bot: Bot) -> None:
    deal_id = call.data[8:]
    deal = await get_deal(deal_id)
    if not deal or deal["status"] not in (STATUS_ACTIVE, STATUS_PAID):
        await call.answer("❌ Действие недоступно", show_alert=True)
        return
    if deal["creator_id"] != call.from_user.id and deal["partner_id"] != call.from_user.id:
        await call.answer("❌ Вы не участник этой сделки", show_alert=True)
        return

    await update_deal(deal_id, status=STATUS_DISPUTE)
    deal = await get_deal(deal_id)

    await safe_edit_text(call.message,
        f"⚠️ <b>Спор открыт!</b>\n\nАдминистратор рассмотрит ситуацию и примет решение.\n\n{deal_card(deal)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_back(),
    )
    await notify_both(
        bot, deal,
        f"⚠️ <b>По вашей сделке открыт спор!</b>\n\nОжидайте решения администратора.\n\n{deal_card(deal)}",
        skip_id=call.from_user.id,
    )
    await notify_admins(
        bot,
        f"⚠️ <b>Открыт спор!</b>\n\n{deal_card(deal)}",
        reply_markup=kb_admin_resolve(deal_id),
    )


@dp.callback_query(F.data.startswith("cancel_deal_"))
async def cb_cancel_deal(call: CallbackQuery, bot: Bot) -> None:
    deal_id = call.data[12:]
    deal = await get_deal(deal_id)
    if not deal or deal["status"] not in (STATUS_WAITING, STATUS_ACTIVE):
        await call.answer("❌ Действие недоступно", show_alert=True)
        return
    if deal["creator_id"] != call.from_user.id and deal["partner_id"] != call.from_user.id:
        await call.answer("❌ Вы не участник этой сделки", show_alert=True)
        return

    await update_deal(deal_id, status=STATUS_CANCELLED)
    deal = await get_deal(deal_id)

    await safe_edit_text(call.message,
        f"❌ <b>Сделка отменена.</b>\n\n{deal_card(deal)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_back(),
    )
    await notify_both(bot, deal, f"❌ <b>Сделка отменена участником.</b>\n\n{deal_card(deal)}", skip_id=call.from_user.id)

# ── Админ-панель ──────────────────────────────────────────────────────────────

@dp.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет доступа.")
        return

    disputes = await get_disputed_deals()
    if not disputes:
        await message.answer("✅ Активных споров нет.")
        return

    for deal in disputes:
        await message.answer(
            f"⚠️ <b>Спор:</b>\n\n{deal_card(deal)}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_admin_resolve(deal["id"]),
        )


@dp.callback_query(F.data.startswith("adm_done_"))
async def cb_adm_done(call: CallbackQuery, bot: Bot) -> None:
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("❌ Нет доступа", show_alert=True)
        return

    deal_id = call.data[9:]
    deal = await get_deal(deal_id)
    if not deal:
        await call.answer("❌ Сделка не найдена", show_alert=True)
        return

    await update_deal(deal_id, status=STATUS_DONE)
    deal = await get_deal(deal_id)

    await safe_edit_text(call.message,
        f"✅ <b>Решение:</b> сделка завершена в пользу покупателя.\n\n{deal_card(deal)}",
        parse_mode=ParseMode.HTML,
    )
    await notify_both(
        bot, deal,
        f"✅ <b>Администратор разрешил спор.</b>\n"
        f"Сделка закрыта в пользу покупателя.\n\n{deal_card(deal)}",
    )


@dp.callback_query(F.data.startswith("adm_cancel_"))
async def cb_adm_cancel(call: CallbackQuery, bot: Bot) -> None:
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("❌ Нет доступа", show_alert=True)
        return

    deal_id = call.data[11:]
    deal = await get_deal(deal_id)
    if not deal:
        await call.answer("❌ Сделка не найдена", show_alert=True)
        return

    await update_deal(deal_id, status=STATUS_CANCELLED)
    deal = await get_deal(deal_id)

    await safe_edit_text(call.message,
        f"↩️ <b>Решение:</b> сделка отменена в пользу продавца.\n\n{deal_card(deal)}",
        parse_mode=ParseMode.HTML,
    )
    await notify_both(
        bot, deal,
        f"↩️ <b>Администратор разрешил спор.</b>\n"
        f"Сделка отменена в пользу продавца.\n\n{deal_card(deal)}",
    )

# ── Запуск ────────────────────────────────────────────────────────────────────

async def main() -> None:
    await init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await bot.delete_my_commands()
    logger.info("Бот-гарант запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
