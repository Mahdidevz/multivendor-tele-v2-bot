import random
import logging
import time
from typing import Any, Dict

from aiogram import F, Router, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import BUTTONS, MESSAGES
from bot.states import WalletChargeStates, UserStates
from core.database.crud import get_or_create_user
from core.database.models import Transaction, User, Vendor, Plan, Server, Ticket
from core.services.panel_client import MarzbanClient

logger = logging.getLogger(__name__)

router = Router()

# --- ۱. هندلر استارت ---
@router.message(CommandStart())
async def cmd_start(message: types.Message, db_session: AsyncSession):
    if not message.from_user: return

    user_id: int = message.from_user.id
    first_name: str = message.from_user.first_name if message.from_user else MESSAGES["default_user"]

    db_user = await get_or_create_user(session=db_session, telegram_id=user_id)
    if not db_user:
        await message.answer("🛠 فروشگاه در حال راه‌اندازی است. لطفاً بعداً مراجعه کنید.")
        return

    wallet_balance = db_user.wallet_balance
    welcome_text = MESSAGES["welcome"].format(name=first_name, balance=wallet_balance)

    main_menu_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BUTTONS["buy_new_service"], callback_data="buy_new_service")],
            [
                InlineKeyboardButton(text=BUTTONS["my_services"], callback_data="my_services"),
                InlineKeyboardButton(text=BUTTONS["charge_wallet"], callback_data="charge_wallet"),
            ],
            [
                InlineKeyboardButton(text=BUTTONS["user_profile"], callback_data="user_profile"),
                InlineKeyboardButton(text=BUTTONS["support"], callback_data="support"),
            ],
        ]
    )
    await message.answer(text=welcome_text, reply_markup=main_menu_keyboard)


# --- ۲. بازگشت به منوی اصلی ---
@router.callback_query(F.data == "back_to_main")
async def process_back_to_main(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user: return

    first_name = callback.from_user.first_name if callback.from_user else MESSAGES["default_user"]

    stmt = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt)).scalar_one_or_none()
    wallet_balance = user.wallet_balance if user else 0.0

    welcome_text = MESSAGES["welcome"].format(name=first_name, balance=wallet_balance)

    main_menu_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BUTTONS["buy_new_service"], callback_data="buy_new_service")],
            [
                InlineKeyboardButton(text=BUTTONS["my_services"], callback_data="my_services"),
                InlineKeyboardButton(text=BUTTONS["charge_wallet"], callback_data="charge_wallet"),
            ],
            [
                InlineKeyboardButton(text=BUTTONS["user_profile"], callback_data="user_profile"),
                InlineKeyboardButton(text=BUTTONS["support"], callback_data="support"),
            ],
        ]
    )

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text=welcome_text, reply_markup=main_menu_keyboard)
    await callback.answer()


# --- ۳. نمایش سرورها (به عنوان دسته‌بندی پلن‌ها) ---
@router.callback_query(F.data == "buy_new_service")
async def process_buy_new_service(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user: return

    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()

    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    # 🌟 واکشی سرورهایی که برای این فروشنده پلن فعال دارند
    stmt_servers = select(Server).join(Plan).where(Plan.vendor_id == user.vendor_id, Plan.is_active == True).distinct()
    servers = (await db_session.execute(stmt_servers)).scalars().all()

    if not servers:
        if isinstance(callback.message, types.Message):
            await callback.message.edit_text(
                "❌ فروشگاه شما هنوز هیچ سرویسی تعریف نکرده است.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="back_to_main")]])
            )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for srv in servers:
        builder.button(text=f"🌍 سرور: {srv.name}", callback_data=f"sel_srv_{srv.id}")

    builder.button(text="🔙 بازگشت به منوی اصلی", callback_data="back_to_main")
    builder.adjust(1)

    if isinstance(callback.message, types.Message):
        text = "🛍 <b>لیست سرورهای موجود</b>\n\nلطفاً یک سرور (موقعیت) را انتخاب کنید:"
        await callback.message.edit_text(text=text, reply_markup=builder.as_markup())

    await callback.answer()


# --- ۴. نمایش پلن‌های یک سرور خاص ---
@router.callback_query(F.data.startswith("sel_srv_"))
async def process_server_selection(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user: return
    srv_id_str = callback.data.replace("sel_srv_", "")
    if not srv_id_str.isdigit(): return
    srv_id = int(srv_id_str)

    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user: return

    stmt_plans = select(Plan).options(selectinload(Plan.server)).where(
        Plan.vendor_id == user.vendor_id,
        Plan.server_id == srv_id,
        Plan.is_active == True
    )
    plans = (await db_session.execute(stmt_plans)).scalars().all()

    builder = InlineKeyboardBuilder()
    server_name = plans[0].server.name if plans else "نامشخص"

    for plan in plans:
        btn_text = f"🪫 {plan.title} - {int(plan.price):,} تومان"
        builder.button(text=btn_text, callback_data=f"buy_plan_{plan.id}")

    builder.button(text="🔙 بازگشت به لیست سرورها", callback_data="buy_new_service")
    builder.adjust(1)

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            text=f"📁 <b>سرویس‌های {server_name}</b>\n\nپلن مورد نظر خود را انتخاب کنید:",
            reply_markup=builder.as_markup()
        )
    await callback.answer()


# --- ۵. انتخاب پلن و نمایش جزئیات ---
@router.callback_query(F.data.startswith("buy_plan_"))
async def process_plan_selection(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user: return

    plan_id_str = callback.data.replace("buy_plan_", "")
    if not plan_id_str.isdigit(): return
    plan_id = int(plan_id_str)

    stmt_plan = select(Plan).options(selectinload(Plan.server)).where(Plan.id == plan_id)
    plan = (await db_session.execute(stmt_plan)).scalar_one_or_none()

    if not plan:
        await callback.answer("❌ پلن مورد نظر یافت نشد.", show_alert=True)
        return

    stmt_user = select(User, Vendor).join(Vendor, User.vendor_id == Vendor.id).where(User.telegram_id == callback.from_user.id)
    row = (await db_session.execute(stmt_user)).first()
    if not row: return
    user, vendor = row

    wallet_balance = user.wallet_balance
    target_vendor = vendor
    if vendor.redirect_to_id:
        stmt_target = select(Vendor).where(Vendor.id == vendor.redirect_to_id)
        redirected_vendor = (await db_session.execute(stmt_target)).scalar_one_or_none()
        if redirected_vendor:
            target_vendor = redirected_vendor

    is_card_missing = not target_vendor.card_number

    vol_text = "نامحدود ∞" if plan.volume_gb == 0 else f"{plan.volume_gb} گیگابایت"
    days_text = "نامحدود ∞" if plan.days == 0 else f"{plan.days} روز"
    user_limit_text = "نامحدود ∞" if plan.user_limit == 0 else f"{plan.user_limit} کاربر"
    desc_text = plan.description if plan.description else "ندارد"

    plan_details = (
        f"🛍 <b>پلن:</b> {plan.title}\n"
        f"🖥 <b>سرور:</b> {plan.server.name}\n"
        f"💽 <b>حجم:</b> {vol_text}\n"
        f"⏳ <b>اعتبار:</b> {days_text}\n"
        f"👥 <b>کاربر همزمان:</b> {user_limit_text}\n"
        f"📝 <b>توضیحات:</b> {desc_text}\n"
        f"💵 <b>قیمت:</b> {int(plan.price):,} تومان\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"💰 <b>موجودی شما:</b> {int(wallet_balance):,} تومان\n\n"
    )

    builder = InlineKeyboardBuilder()

    if wallet_balance >= plan.price:
        builder.button(text=BUTTONS["confirm_buy"], callback_data=f"confirm_buy_{plan.id}")
        text = "✅ <b>موجودی شما کافی است.</b>\n\n" + plan_details + "برای صدور کانفیگ روی تایید کلیک کنید:"
    elif is_card_missing:
        text = "❌ <b>شماره کارت فروشگاه ثبت نشده است!</b>\n\n" + plan_details + "لطفاً پیش از اقدام به خرید، با پشتیبانی هماهنگ کنید."
    else:
        builder.button(text="💳 واریز و دریافت کانفیگ", callback_data=f"charge_req_{plan.id}")
        text = "❌ <b>موجودی شما کافی نیست.</b>\n\n" + plan_details + "برای پرداخت و دریافت کانفیگ روی دکمه زیر کلیک کنید:"

    builder.button(text="🔙 بازگشت به سرویس‌ها", callback_data=f"sel_srv_{plan.server_id}")
    builder.adjust(1)

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text=text, reply_markup=builder.as_markup())

    await callback.answer()


# --- ۶. صدور پیش‌فاکتور اتوماتیک ---
@router.callback_query(F.data.startswith("charge_req_"))
async def auto_charge_for_plan(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    if not callback.data or not callback.from_user: return

    plan_id_str = callback.data.replace("charge_req_", "")
    if not plan_id_str.isdigit(): return
    plan_id = int(plan_id_str)

    stmt_plan = select(Plan).where(Plan.id == plan_id)
    plan = (await db_session.execute(stmt_plan)).scalar_one_or_none()
    if not plan: return

    stmt = select(User, Vendor).join(Vendor, User.vendor_id == Vendor.id).where(User.telegram_id == callback.from_user.id)
    row = (await db_session.execute(stmt)).first()
    if not row: return
    user_row, vendor_row = row

    needed_amount = plan.price - user_row.wallet_balance
    if needed_amount <= 0:
        await callback.answer("✅ موجودی شما کافی است.", show_alert=True)
        return

    target_vendor = vendor_row
    if vendor_row.redirect_to_id:
        stmt_target = select(Vendor).where(Vendor.id == vendor_row.redirect_to_id)
        redirected_vendor = (await db_session.execute(stmt_target)).scalar_one_or_none()
        if redirected_vendor: target_vendor = redirected_vendor

    if not target_vendor.card_number:
        await callback.answer("❌ فروشگاه هنوز شماره کارتی برای پرداخت ثبت نکرده است.", show_alert=True)
        return

    random_fee = random.randint(1, 900)
    final_amount = int(needed_amount) + random_fee

    new_tx = Transaction(
        user_id=user_row.id,
        vendor_id=vendor_row.id,
        amount=final_amount,
        destination_card=target_vendor.card_number,
        status="pending",
        plan_id=plan.id
    )
    db_session.add(new_tx)
    await db_session.commit()
    await db_session.refresh(new_tx)

    await state.update_data(transaction_id=new_tx.id)

    text = (
        f"🧾 <b>فاکتور خرید سریع پلن ({plan.title})</b>\n\n"
        f"💳 لطفاً مبلغ زیر را به شماره کارت <b>{target_vendor.name}</b> واریز کنید:\n"
        f"<code>{target_vendor.card_number}</code>\n\n"
        f"💵 <b>مبلغ دقیق واریز:</b> <code>{final_amount:,}</code> تومان\n\n"
        f"⚠️ مبلغ {random_fee} تومان جهت شناسایی سیستمی اضافه شده است.\n"
        "📸 پس از واریز، عکس رسید را همینجا بفرستید تا کانفیگ صادر شود:"
    )

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data="back_to_main")]])
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=cancel_kb)

    await state.set_state(WalletChargeStates.waiting_for_receipt)
    await callback.answer()


# --- ۷. دریافت عکس و ارسال به پی‌وی ادمین ---
@router.message(WalletChargeStates.waiting_for_receipt, F.photo)
async def process_receipt_photo(message: types.Message, state: FSMContext, db_session: AsyncSession):
    if not message.photo or not message.from_user or not message.bot: return

    data = await state.get_data()
    transaction_id = data.get("transaction_id")

    if not transaction_id:
        await message.answer("❌ خطایی رخ داد. لطفاً مجددا تلاش کنید.")
        await state.clear()
        await cmd_start(message, db_session)
        return

    photo_file_id = message.photo[-1].file_id

    stmt = select(Transaction).options(selectinload(Transaction.plan).selectinload(Plan.server)).where(Transaction.id == transaction_id)
    transaction = (await db_session.execute(stmt)).scalar_one_or_none()

    if not transaction:
        await message.answer("❌ فاکتور شما در سیستم یافت نشد.")
        await state.clear()
        await cmd_start(message, db_session)
        return

    stmt_vendor = select(Vendor).where(Vendor.id == transaction.vendor_id)
    vendor = (await db_session.execute(stmt_vendor)).scalar_one_or_none()
    if not vendor: return

    transaction.receipt_file_id = photo_file_id
    transaction.status = "pending"
    await db_session.commit()

    admin_tg_id = vendor.telegram_id
    if vendor.redirect_to_id:
        stmt_redirect = select(Vendor).where(Vendor.id == vendor.redirect_to_id)
        target = (await db_session.execute(stmt_redirect)).scalar_one_or_none()
        if target: admin_tg_id = target.telegram_id

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ تایید فیش", callback_data=f"admin_approve_tx_{transaction.id}"),
            InlineKeyboardButton(text="❌ رد فیش", callback_data=f"admin_reject_tx_{transaction.id}")
        ]
    ])

    plan_text = "شارژ عادی کیف پول"
    if transaction.plan_id and transaction.plan:
        vol_str = "نامحدود" if transaction.plan.volume_gb == 0 else f"{transaction.plan.volume_gb}GB"
        server_name = transaction.plan.server.name if transaction.plan.server else "نامشخص"
        plan_text = f"خرید {transaction.plan.title} | سرور: {server_name} ({vol_str} / {transaction.plan.days} روز)"

    caption = (
        f"🧾 <b>فیش واریزی جدید</b>\n\n"
        f"👤 آیدی کاربر: <code>{message.from_user.id}</code>\n"
        f"💰 مبلغ واریز: <code>{transaction.amount:,}</code> تومان\n"
        f"📌 بابت: <b>{plan_text}</b>"
    )

    try:
        # 🌟 ارسال به پی‌وی ادمین به صورت امن
        await message.bot.send_photo(chat_id=admin_tg_id, photo=photo_file_id, caption=caption, reply_markup=kb)
    except Exception as e:
        print(f"Failed to send receipt to admin: {e}")

    await message.answer("✅ <b>فیش شما با موفقیت ثبت شد!</b>\n\nبه محض تایید ادمین، نتیجه به شما اعلام خواهد شد.")
    await state.clear()

    # 🌟 بازگشت خودکار به منوی اصلی
    await cmd_start(message, db_session)


@router.message(WalletChargeStates.waiting_for_receipt)
async def process_receipt_invalid(message: types.Message):
    await message.answer("❌ لطفاً رسید خود را به صورت یک **عکس (Photo)** ارسال کنید.")


# --- ۸. هندلرهای شارژ عادی کیف پول ---
@router.callback_query(F.data == "charge_wallet")
async def ask_charge_amount(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    # 🌟 گارد امنیتی: اگر کارت ادمین ثبت نشده بود اجازه شارژ ندهیم
    stmt = select(User, Vendor).join(Vendor, User.vendor_id == Vendor.id).where(User.telegram_id == callback.from_user.id)
    row = (await db_session.execute(stmt)).first()
    if not row: return

    _, vendor_row = row
    target_vendor = vendor_row
    if vendor_row.redirect_to_id:
        stmt_target = select(Vendor).where(Vendor.id == vendor_row.redirect_to_id)
        redirected_vendor = (await db_session.execute(stmt_target)).scalar_one_or_none()
        if redirected_vendor: target_vendor = redirected_vendor

    if not target_vendor.card_number:
        await callback.answer("❌ فروشگاه هنوز شماره کارتی برای پرداخت ثبت نکرده است.", show_alert=True)
        return

    text = (
        "💰 <b>شارژ کیف پول</b>\n\n"
        "لطفاً مبلغی که قصد دارید کیف پول خود را شارژ کنید، به تومان و به صورت عدد ارسال کنید.\n"
        "مثال: 50000"
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ انصراف", callback_data="back_to_main")]])

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=cancel_kb)

    await state.set_state(WalletChargeStates.waiting_for_amount)
    await callback.answer()

@router.message(WalletChargeStates.waiting_for_amount)
async def process_charge_amount(message: types.Message, state: FSMContext, db_session: AsyncSession):
    if not message.from_user or not message.text: return

    if not message.text.isdigit():
        await message.answer("❌ لطفاً مبلغ را فقط به صورت عدد وارد کنید.")
        return

    base_amount = int(message.text)
    if base_amount < 10000:
        await message.answer("❌ حداقل مبلغ شارژ ۱۰,۰۰۰ تومان است.")
        return

    stmt = select(User, Vendor).join(Vendor, User.vendor_id == Vendor.id).where(User.telegram_id == message.from_user.id)
    row = (await db_session.execute(stmt)).first()
    if not row: return
    user_row, vendor_row = row

    target_vendor = vendor_row
    if vendor_row.redirect_to_id:
        stmt_target = select(Vendor).where(Vendor.id == vendor_row.redirect_to_id)
        redirected_vendor = (await db_session.execute(stmt_target)).scalar_one_or_none()
        if redirected_vendor: target_vendor = redirected_vendor

    if not target_vendor.card_number:
        await message.answer("❌ فروشگاه هنوز شماره کارتی برای پرداخت ثبت نکرده است. عملیات لغو شد.")
        await state.clear()
        await cmd_start(message, db_session)
        return

    random_fee = random.randint(1, 900)
    final_amount = base_amount + random_fee

    new_tx = Transaction(
        user_id=user_row.id,
        vendor_id=vendor_row.id,
        amount=final_amount,
        destination_card=target_vendor.card_number,
        status="pending"
    )
    db_session.add(new_tx)
    await db_session.commit()
    await db_session.refresh(new_tx)
    await state.update_data(transaction_id=new_tx.id)

    text = (
        f"🧾 <b>اطلاعات پرداخت</b>\n\n"
        f"💳 لطفاً مبلغ زیر را به شماره کارت <b>{target_vendor.name}</b> واریز کنید:\n"
        f"<code>{target_vendor.card_number}</code>\n\n"
        f"💵 <b>مبلغ دقیق واریز:</b> <code>{final_amount:,}</code> تومان\n\n"
        f"⚠️ مبلغ {random_fee} تومان جهت شناسایی سیستمی اضافه شده است.\n"
        "📸 پس از واریز، عکس رسید خود را همینجا بفرستید:"
    )

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data="back_to_main")]])
    await message.answer(text, reply_markup=cancel_kb)
    await state.set_state(WalletChargeStates.waiting_for_receipt)


# ==========================================
# 🌟 [جدید] پروفایل کاربر
# ==========================================
@router.callback_query(F.data == "user_profile")
async def process_user_profile(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    # واکشی کاربر بههمراه فروشندهاش
    stmt = (
        select(User)
        .options(selectinload(User.vendor))
        .where(User.telegram_id == callback.from_user.id)
    )
    user = (await db_session.execute(stmt)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    # شمارش تعداد سرویسهای خریداریشده (تراکنشهای تاییدشده با پلن)
    count_stmt = select(func.count(Transaction.id)).where(
        Transaction.user_id == user.id,
        Transaction.status == "approved",
        Transaction.plan_id.is_not(None),
    )
    total_services = (await db_session.execute(count_stmt)).scalar_one()

    shop_name = user.vendor.name if user.vendor else "نامشخص"

    text = (
        "👤 <b>پروفایل کاربری</b>\n\n"
        f"🆔 <b>شناسه تلگرام:</b> <code>{user.telegram_id}</code>\n"
        f"🏪 <b>فروشگاه:</b> {shop_name}\n"
        f"💰 <b>موجودی کیف پول:</b> <code>{int(user.wallet_balance):,}</code> تومان\n"
        f"🛍 <b>تعداد سرویسها:</b> {total_services}\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="back_to_main")]
    ])
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# ==========================================
# 🌟 [جدید] پشتیبانی / تیکتینگ
# ==========================================
@router.callback_query(F.data == "support")
async def process_support(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    # بررسی وجود کاربر در سیستم
    stmt = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    # ذخیره user_id و vendor_id برای استفاده در هندلر بعدی
    await state.update_data(ticket_user_id=user.id, ticket_vendor_id=user.vendor_id)

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="back_to_main")]
    ])
    text = (
        "🎧 <b>پشتیبانی</b>\n\n"
        "لطفاً پیام خود را به صورت متن ارسال کنید.\n"
        "پیام شما برای پشتیبانی فروشگاه ارسال خواهد شد."
    )
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=cancel_kb)
    await state.set_state(UserStates.waiting_for_support_message)
    await callback.answer()


@router.message(UserStates.waiting_for_support_message)
async def process_support_message(message: types.Message, state: FSMContext, db_session: AsyncSession):
    if not message.text or not message.from_user:
        return

    data = await state.get_data()
    ticket_user_id = data.get("ticket_user_id")
    ticket_vendor_id = data.get("ticket_vendor_id")

    # گارد: بررسی وجود شناسه‌های لازم
    if not ticket_user_id or not ticket_vendor_id:
        await message.answer("❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.")
        await state.clear()
        await cmd_start(message, db_session)
        return

    message_text = message.text.strip()
    if not message_text:
        await message.answer("❌ پیام نمیتواند خالی باشد.")
        return

    # ساخت تیکت جدید
    new_ticket = Ticket(
        user_id=int(ticket_user_id),
        vendor_id=int(ticket_vendor_id),
        message_text=message_text,
        status="pending",
    )
    db_session.add(new_ticket)
    await db_session.commit()
    await state.clear()

    await message.answer("✅ پیام شما برای پشتیبانی ارسال شد.")

    # بازگشت خودکار به منوی اصلی
    await cmd_start(message, db_session)


# ==========================================
# 🌟 [جدید] سرویسهای من + مانیتورینگ زنده
# ==========================================
@router.callback_query(F.data == "my_services")
async def process_my_services(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    # واکشی کاربر
    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    # واکشی تراکنشهای تاییدشده که دارای پلن هستند
    stmt = (
        select(Transaction)
        .options(selectinload(Transaction.plan))
        .where(
            Transaction.user_id == user.id,
            Transaction.status == "approved",
            Transaction.plan_id.is_not(None),
        )
        .order_by(Transaction.created_at.desc())
    )
    transactions = (await db_session.execute(stmt)).scalars().all()

    if not transactions:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="back_to_main")]
        ])
        if isinstance(callback.message, types.Message):
            await callback.message.edit_text(
                "📦 <b>سرویسهای من</b>\n\nشما هنوز هیچ سرویسی خریداری نکرده‌اید.",
                reply_markup=kb
            )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for tx in transactions:
        plan_title = tx.plan.title if tx.plan else "نامشخص"
        builder.button(text=f"📦 {plan_title}", callback_data=f"usr_srv_{tx.id}")
    builder.button(text="🔙 بازگشت به منوی اصلی", callback_data="back_to_main")
    builder.adjust(1)

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "📦 <b>سرویسهای من</b>\n\nبرای مشاهده جزئیات و وضعیت زنده، یک سرویس را انتخاب کنید:",
            reply_markup=builder.as_markup()
        )
    await callback.answer()


@router.callback_query(F.data.startswith("usr_srv_"))
async def process_service_monitoring(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    tx_id_str = callback.data.replace("usr_srv_", "")
    if not tx_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    tx_id = int(tx_id_str)

    # واکشی تراکنش بههمراه پلن و سرور
    stmt = (
        select(Transaction)
        .options(
            selectinload(Transaction.user),
            selectinload(Transaction.plan).selectinload(Plan.server),
        )
        .where(Transaction.id == tx_id)
    )
    tx = (await db_session.execute(stmt)).scalar_one_or_none()
    if not tx or not tx.user or not tx.plan or not tx.plan.server:
        await callback.answer("❌ سرویس یافت نشد.", show_alert=True)
        return

    # بررسی مالکیت سرویس
    if tx.user.telegram_id != callback.from_user.id:
        await callback.answer("❌ دسترسی غیرمجاز.", show_alert=True)
        return

    server: Server = tx.plan.server
    username = f"U_{tx.user.telegram_id}_{tx.id}"

    # فراخوانی API برای گرفتن وضعیت زنده کاربر
    client = MarzbanClient(
        base_url=server.panel_url,
        username=server.username,
        password=server.password,
    )
    api_data: Dict[str, Any] | None = None
    try:
        api_data = await client.get_user(username=username)
    except Exception as e:
        logger.error(f"Live monitoring failed for tx {tx_id}: {e}")
        api_data = None
    finally:
        await client.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data=f"usr_srv_{tx.id}")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="my_services")],
    ])

    if api_data is None:
        text = (
            "📡 <b>مانیتورینگ سرویس</b>\n\n"
            f"🛍 <b>سرویس:</b> {tx.plan.title if tx.plan else 'نامشخص'}\n"
            f"🖥 <b>سرور:</b> {server.name}\n\n"
            "❌ <b>سرور در حال حاضر قابل دسترسی نیست.</b>\n"
            "لطفاً بعداً مجدداً تلاش کنید."
        )
    else:
        # استخراج مقادیر از پاسخ API (با گارد روی مقادیر ممکن None/غیرموجود)
        data_limit = api_data.get("data_limit") or 0  # بایت
        used_traffic = api_data.get("used_traffic") or 0  # بایت
        expire_ts = api_data.get("expire") or 0
        status = api_data.get("status") or "unknown"

        # محاسبه حجمها به GB
        total_gb = data_limit / (1024 ** 3) if data_limit else 0.0
        used_gb = used_traffic / (1024 ** 3) if used_traffic else 0.0
        remaining_gb = max(total_gb - used_gb, 0.0) if data_limit else 0.0

        # محاسبه روزهای باقیمانده
        remaining_days = 0
        if expire_ts:
            remaining_seconds = int(expire_ts) - int(time.time())
            remaining_days = max(remaining_seconds // 86400, 0)

        # قالببندی خروجی
        if data_limit > 0:
            total_text = f"{total_gb:.2f} GB"
            remaining_text = f"{remaining_gb:.2f} GB"
        else:
            total_text = "نامحدود ∞"
            remaining_text = "نامحدود ∞"

        if expire_ts:
            days_text = f"{remaining_days} روز"
        else:
            days_text = "نامحدود ∞"

        status_emoji = "🟢" if status == "active" else "🔴"

        text = (
            "📡 <b>مانیتورینگ زنده سرویس</b>\n\n"
            f"🛍 <b>سرویس:</b> {tx.plan.title}\n"
            f"🖥 <b>سرور:</b> {server.name}\n"
            f"{status_emoji} <b>وضعیت کاربر:</b> {status}\n"
            "〰️〰️〰️〰️〰️〰️〰️\n"
            f"💽 <b>حجم کل:</b> {total_text}\n"
            f"📊 <b>حجم مصرفشده:</b> {used_gb:.2f} GB\n"
            f"✨ <b>حجم باقیمانده:</b> {remaining_text}\n"
            f"⏳ <b>روزهای باقیمانده:</b> {days_text}\n"
        )

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()
