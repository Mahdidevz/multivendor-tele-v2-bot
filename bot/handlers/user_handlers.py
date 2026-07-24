import io
import random
import logging
import time
from datetime import datetime
from typing import Any, Dict

import qrcode
from aiogram import F, Router, types
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import BUTTONS, MESSAGES
from bot.states import WalletChargeStates, UserStates, PurchaseStates
from core.database.crud import get_or_create_user
from core.database.models import Transaction, User, Vendor, Plan, Server, Ticket, ForceJoinChannel, VendorServer, DiscountCode
from core.services.panel_client import MarzbanClient

logger = logging.getLogger(__name__)

router = Router()

# --- ۱. هندلر استارت ---
@router.message(CommandStart())
async def cmd_start(message: types.Message, db_session: AsyncSession):
    if not message.from_user: return

    user_id: int = message.from_user.id
    first_name: str = message.from_user.first_name if message.from_user else MESSAGES["default_user"]

    # 🌟 [Bug 1 Fix] استخراج payload از دیپلینک (مثلاً /start ref_5)
    # فقط برای کاربران جدید استفاده میشود؛ کاربران موجود در crud.py پیوند دائمی دارند.
    deep_link_vendor_id: int | None = None
    if message.text:
        # پارس امن متن /start بدون وابستگی به attributeهای جادویی
        # فرمتها: "/start" یا "/start 5" یا "/start ref_5" یا "/start start_5"
        text_parts = message.text.split(maxsplit=1)
        if len(text_parts) > 1:
            raw_payload = text_parts[1].strip()
            numeric_part = raw_payload
            for prefix in ("ref_", "start_", "vendor_"):
                if numeric_part.startswith(prefix):
                    numeric_part = numeric_part[len(prefix):]
                    break
            if numeric_part.isdigit():
                deep_link_vendor_id = int(numeric_part)

    db_user = await get_or_create_user(
        session=db_session,
        telegram_id=user_id,
        deep_link_vendor_id=deep_link_vendor_id,
    )
    if not db_user:
        await message.answer("🛠 فروشگاه در حال راهاندازی است. لطفاً بعداً مراجعه کنید.")
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
            [InlineKeyboardButton(text=BUTTONS["free_test"], callback_data="free_test_start")],
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
            [InlineKeyboardButton(text=BUTTONS["free_test"], callback_data="free_test_start")],
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
    # 🌟 [Bug 2 Fix] استفاده از redirect_target_id (فیلد جدید) به جای redirect_to_id
    if vendor.redirect_target_id:
        stmt_target = select(Vendor).where(Vendor.id == vendor.redirect_target_id)
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


# --- ۶. مرحله تخفیف + صدور پیشفاکتور ---
# 🌟 [جدید] به جای ساخت مستقیم تراکنش، ابتدا مرحله «اعمال کد تخفیف» نمایش داده می‌شود.
@router.callback_query(F.data.startswith("charge_req_"))
async def apply_discount_start(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    if not callback.data or not callback.from_user: return

    plan_id_str = callback.data.replace("charge_req_", "")
    if not plan_id_str.isdigit(): return
    plan_id = int(plan_id_str)

    stmt_plan = select(Plan).where(Plan.id == plan_id)
    plan = (await db_session.execute(stmt_plan)).scalar_one_or_none()
    if not plan:
        await callback.answer("❌ پلن یافت نشد.", show_alert=True)
        return

    stmt = select(User, Vendor).join(Vendor, User.vendor_id == Vendor.id).where(User.telegram_id == callback.from_user.id)
    row = (await db_session.execute(stmt)).first()
    if not row: return
    user_row, vendor_row = row

    # ذخیره اطلاعات در FSM برای استفاده در مراحل بعد
    await state.update_data(
        plan_id=plan.id,
        user_id=user_row.id,
        vendor_id=vendor_row.id,
        original_price=int(plan.price),
    )

    text = (
        "🎁 <b>کد تخفیف</b>\n\n"
        f"💵 قیمت پلن: <code>{int(plan.price):,}</code> تومان\n\n"
        "اگر کد تخفیف دارید، آن را ارسال کنید.\n"
        "در غیر این صورت روی دکمه زیر کلیک کنید."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ ادامه بدون تخفیف", callback_data="skip_discount")],
        [InlineKeyboardButton(text="❌ لغو", callback_data="back_to_main")],
    ])

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=kb)

    await state.set_state(PurchaseStates.waiting_for_discount_code)
    await callback.answer()


@router.callback_query(F.data == "skip_discount", PurchaseStates.waiting_for_discount_code)
async def skip_discount(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return
    data = await state.get_data()
    original_price = int(data.get("original_price") or 0)
    if isinstance(callback.message, types.Message):
        await _create_purchase_invoice(callback.message, state, db_session, 0, original_price)
    await callback.answer()


@router.message(PurchaseStates.waiting_for_discount_code)
async def process_discount_code(message: types.Message, state: FSMContext, db_session: AsyncSession):
    if not message.text or not message.from_user:
        return

    data = await state.get_data()
    plan_id = data.get("plan_id")
    vendor_id = data.get("vendor_id")
    user_id_db = data.get("user_id")
    original_price = int(data.get("original_price") or 0)

    if not plan_id or not vendor_id or not user_id_db:
        await message.answer("❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.")
        await state.clear()
        return

    code = message.text.strip()
    dc = (await db_session.execute(
        select(DiscountCode).where(
            DiscountCode.vendor_id == int(vendor_id),
            DiscountCode.code == code,
            DiscountCode.is_active == True,
        )
    )).scalar_one_or_none()

    if not dc:
        await message.answer(
            "❌ کد تخفیف نامعتبر یا منقضی است.\n"
            "لطفاً کد صحیح وارد کنید یا روی «ادامه بدون تخفیف» کلیک کنید."
        )
        return

    # 🌟 [جدید] بررسی تکبارگی استفاده به ازای هر کاربر:
    # اگر کاربر قبلاً این کد را در یک تراکنش pending یا approved استفاده کرده باشد، ممنوع میشود.
    # نکته: اگر ادمین تراکنش قبلی را reject کند (status == "rejected")، کاربر مجدداً میتواند از کد استفاده کند.
    existing_usage = await db_session.scalar(
        select(Transaction).where(
            Transaction.user_id == int(user_id_db),
            Transaction.discount_code_id == dc.id,
            Transaction.status.in_(["pending", "approved"]),
        )
    )
    if existing_usage is not None:
        await message.answer("❌ شما قبلاً از این کد تخفیف استفاده کرده‌اید.")
        return

    discount_percent = int(dc.discount_percent)
    discount_amount = original_price * discount_percent // 100
    final_price = original_price - discount_amount

    # 🌟 ذخیره discount_code_id در FSM تا هنگام ساخت تراکنش درج شود
    await state.update_data(
        discount_percent=discount_percent,
        final_price=final_price,
        discount_code_id=dc.id,
    )
    await _create_purchase_invoice(message, state, db_session, discount_percent, final_price)


async def _create_purchase_invoice(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
    discount_percent: int,
    final_price: int,
) -> None:
    """ساخت تراکنش و نمایش فاکتور پرداخت برای کاربر."""
    data = await state.get_data()
    plan_id = data.get("plan_id")
    user_id_db = data.get("user_id")
    vendor_id = data.get("vendor_id")
    original_price = int(data.get("original_price") or 0)
    discount_code_id = data.get("discount_code_id")  # 🌟 [جدید] ممکن است None باشد (بدون تخفیف)

    if not plan_id or not user_id_db or not vendor_id:
        await message.answer("❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.")
        await state.clear()
        return

    plan = (await db_session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if not plan:
        await message.answer("❌ پلن یافت نشد.")
        await state.clear()
        return

    # واکشی فروشنده و فروشنده هدف (ریدایرکت)
    vendor = (await db_session.execute(select(Vendor).where(Vendor.id == int(vendor_id)))).scalar_one_or_none()
    if not vendor:
        await message.answer("❌ فروشنده یافت نشد.")
        await state.clear()
        return

    target_vendor = vendor
    target_vendor_id = vendor.id
    if vendor.redirect_target_id:
        redirected = (await db_session.execute(select(Vendor).where(Vendor.id == vendor.redirect_target_id))).scalar_one_or_none()
        if redirected:
            target_vendor = redirected
            target_vendor_id = redirected.id

    if not target_vendor.card_number:
        await message.answer("❌ فروشگاه هنوز شماره کارتی برای پرداخت ثبت نکرده است.")
        await state.clear()
        return

    random_fee = random.randint(1, 900)
    amount_with_fee = final_price + random_fee

    # 🌟 ساخت تراکنش با فیلدهای تخفیف و ریدایرکت
    new_tx = Transaction(
        user_id=int(user_id_db),
        vendor_id=target_vendor_id,                                          # فروشنده هدف (مدیرکننده)
        origin_vendor_id=vendor.id if target_vendor_id != vendor.id else None,  # منشأ در صورت ریدایرکت
        plan_id=plan.id,
        amount=amount_with_fee,                                                # مبلغ نهایی پرداختی
        original_amount=original_price,                                        # قیمت اصلی پلن
        discount_percent=discount_percent,                                     # درصد تخفیف
        discount_code_id=int(discount_code_id) if discount_code_id else None,  # 🌟 [جدید] ارجاع به کد تخفیف
        destination_card=target_vendor.card_number,
        status="pending",
    )
    db_session.add(new_tx)
    await db_session.commit()
    await db_session.refresh(new_tx)
    await state.update_data(transaction_id=new_tx.id)

    # ساخت متن فاکتور
    discount_line = ""
    if discount_percent > 0:
        discount_amount = original_price - final_price
        discount_line = f"🎯 تخفیف ({discount_percent}%): <code>-{discount_amount:,}</code> تومان\n"

    text = (
        f"🧾 <b>فاکتور خرید ({plan.title})</b>\n\n"
        f"💵 قیمت اصلی: <code>{original_price:,}</code> تومان\n"
        f"{discount_line}"
        f"💰 مبلغ نهایی: <code>{final_price:,}</code> تومان\n\n"
        f"💳 لطفاً مبلغ زیر را به شماره کارت <b>{target_vendor.name}</b> واریز کنید:\n"
        f"<code>{target_vendor.card_number}</code>\n\n"
        f"💵 <b>مبلغ دقیق واریز:</b> <code>{amount_with_fee:,}</code> تومان\n\n"
        f"⚠️ مبلغ {random_fee} تومان جهت شناسایی سیستمی اضافه شده است.\n"
        "📸 پس از واریز، عکس رسید را همینجا بفرستید تا کانفیگ صادر شود:"
    )

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data="back_to_main")]])
    await message.answer(text, reply_markup=cancel_kb)
    await state.set_state(WalletChargeStates.waiting_for_receipt)



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
    # 🌟 [Bug 2 Fix] استفاده از redirect_target_id برای هدایت فیش به فروشنده هدف
    if vendor.redirect_target_id:
        stmt_redirect = select(Vendor).where(Vendor.id == vendor.redirect_target_id)
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
        f"💰 مبلغ واریز: <code>{int(transaction.amount):,}</code> تومان\n"
    )
    # 🌟 نمایش اطلاعات تخفیف در صورت وجود
    if transaction.discount_percent and transaction.discount_percent > 0:
        caption += (
            f"🎯 تخفیف ({transaction.discount_percent}%): <code>{int(transaction.original_amount):,}</code> ← <code>{int(transaction.amount):,}</code> تومان\n"
        )
    caption += f"📌 بابت: <b>{plan_text}</b>"

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
    # 🌟 [Bug 2 Fix] استفاده از redirect_target_id در هندلر شارژ کیف پول
    if vendor_row.redirect_target_id:
        stmt_target = select(Vendor).where(Vendor.id == vendor_row.redirect_target_id)
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
    # 🌟 [Bug 2 Fix] استفاده از redirect_target_id در هندلر پردازش مبلغ شارژ
    if vendor_row.redirect_target_id:
        stmt_target = select(Vendor).where(Vendor.id == vendor_row.redirect_target_id)
        redirected_vendor = (await db_session.execute(stmt_target)).scalar_one_or_none()
        if redirected_vendor: target_vendor = redirected_vendor

    if not target_vendor.card_number:
        await message.answer("❌ فروشگاه هنوز شماره کارتی برای پرداخت ثبت نکرده است. عملیات لغو شد.")
        await state.clear()
        await cmd_start(message, db_session)
        return

    random_fee = random.randint(1, 900)
    final_amount = base_amount + random_fee

    # 🌟 [Bug 4 Fix] حسابداری ریدایرکت در شارژ کیف پول: مثل _create_purchase_invoice
    # vendor_id = فروشنده هدف (مدیرکننده پول/پشتیبانی)
    # origin_vendor_id = فروشنده اصلی کاربر (اگر ریدایرکت شده)
    target_vendor_id = target_vendor.id
    origin_vendor_id: int | None = None
    if target_vendor_id != vendor_row.id:
        origin_vendor_id = vendor_row.id

    new_tx = Transaction(
        user_id=user_row.id,
        vendor_id=target_vendor_id,
        origin_vendor_id=origin_vendor_id,
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

    # 🌟 [Bug 2 Fix] تیکت پشتیبانی هم باید به redirect_target_id برود
    # کاربر به طور دائمی به user.vendor_id پیوند خورده، اما تیکت ممکن است به فروشنده هدف ریدایرکت شود.
    stmt_vendor = select(Vendor).where(Vendor.id == user.vendor_id)
    vendor_row = (await db_session.execute(stmt_vendor)).scalar_one_or_none()
    final_vendor_id: int = user.vendor_id
    if vendor_row and vendor_row.redirect_target_id:
        stmt_target = select(Vendor).where(Vendor.id == vendor_row.redirect_target_id)
        target_vendor = (await db_session.execute(stmt_target)).scalar_one_or_none()
        if target_vendor:
            final_vendor_id = target_vendor.id

    # ذخیره user_id و vendor_id نهایی (با درنظرگرفتن ریدایرکت) برای استفاده در هندلر بعدی
    await state.update_data(ticket_user_id=user.id, ticket_vendor_id=final_vendor_id)

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
        expire_val = api_data.get("expire")  # ممکن است ISO string یا timestamp باشد
        status = api_data.get("status") or "unknown"

        # محاسبه حجمها به GB
        total_gb = data_limit / (1024 ** 3) if data_limit else 0.0
        used_gb = used_traffic / (1024 ** 3) if used_traffic else 0.0
        remaining_gb = max(total_gb - used_gb, 0.0) if data_limit else 0.0

        # 🌟 محاسبه روزهای باقیمانده با پارس امن ISO 8601 یا timestamp
        # Marzban ممکن است expire را به صورت ISO string (مثلاً '2026-08-23T00:02:47Z') بفرستد
        remaining_days = 0
        has_expire = False
        if expire_val:
            try:
                if isinstance(expire_val, str) and "T" in expire_val:
                    # فرمت ISO 8601 (مثال: 2026-08-23T00:02:47Z)
                    dt = datetime.fromisoformat(expire_val.replace("Z", "+00:00"))
                    expire_ts = int(dt.timestamp())
                else:
                    expire_ts = int(expire_val)
                remaining_seconds = expire_ts - int(time.time())
                remaining_days = max(remaining_seconds // 86400, 0)
                has_expire = True
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse expire value '{expire_val}': {e}")
                has_expire = False

        # قالببندی خروجی
        if data_limit > 0:
            total_text = f"{total_gb:.2f} GB"
            remaining_text = f"{remaining_gb:.2f} GB"
        else:
            total_text = "نامحدود ∞"
            remaining_text = "نامحدود ∞"

        if has_expire:
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
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            # 🌟 اگر محتوا تغییر نکرده باشد (مثلاً حجم همان است) تلگرام خطا میدهد؛ نادیده بگیر
            pass
    await callback.answer("🔄 اطلاعات بروزرسانی شد")


# ==========================================
# 🎁 [جدید] دریافت تست رایگان + Force-Join
# ==========================================

# --- مرحله ۱: ورود به تست رایگان (فقط رندر کانالها) ---
@router.callback_query(F.data == "free_test_start")
async def free_test_start(callback: types.CallbackQuery, db_session: AsyncSession):
    # گاردهای ایمنی نوع
    if not callback.from_user or not callback.data:
        await callback.answer()
        return

    # واکشی کاربر
    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    # واکشی کانالهای عضویت اجباری این فروشنده
    stmt_channels = select(ForceJoinChannel).where(ForceJoinChannel.vendor_id == user.vendor_id)
    channels = (await db_session.execute(stmt_channels)).scalars().all()

    # اگر کانالی تعریف نشده → مستقیم به مرحله ۲ (نمایش سرورها)
    if not channels:
        new_callback = callback.model_copy(update={"data": "free_test_servers"})
        await free_test_show_servers(new_callback, db_session)
        return

    # گارد روی bot
    bot = callback.bot
    if bot is None:
        await callback.answer("❌ خطای داخلی رخ داده است.", show_alert=True)
        return

    # بررسی عضویت کاربر در هر کانال
    unjoined: list[ForceJoinChannel] = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch.chat_id, user_id=user.telegram_id)
            if member.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                unjoined.append(ch)
        except TelegramBadRequest as e:
            logger.warning(f"Force-join check failed for channel {ch.chat_id} : {e}")
            unjoined.append(ch)
        except Exception as e:
            logger.warning(f"Force-join membership check failed for channel {ch.chat_id}: {e}")
            unjoined.append(ch)

    # اگر در همه کانالها عضو بود → مستقیم به مرحله ۲
    if not unjoined:
        new_callback = callback.model_copy(update={"data": "free_test_servers"})
        await free_test_show_servers(new_callback, db_session)
        return

    # 🌟 ورود اولیه: فقط کیبورد کانالها را رندر کن. هیچ هشدار پاپآپی نده.
    kb_rows: list[list[InlineKeyboardButton]] = []
    for ch in unjoined:
        kb_rows.append([InlineKeyboardButton(text=ch.title, url=ch.url)])
    kb_rows.append([InlineKeyboardButton(text="✅ بررسی مجدد عضویت", callback_data="free_test_verify")])
    new_kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    text = "🔒 برای دریافت تست رایگان، ابتدا در کانالهای زیر عضو شوید:"

    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text=text, reply_markup=new_kb)
        except TelegramBadRequest:
            # محتوای تکراری → بیخیال ویرایش شو ولی پاپآپ هم نده (ورود اولیه است)
            pass

    await callback.answer()


# --- مرحله ۱.۵: بررسی مجدد عضویت (فقط هنگام کلیک روی دکمه «بررسی مجدد») ---
@router.callback_query(F.data == "free_test_verify")
async def free_test_verify(callback: types.CallbackQuery, db_session: AsyncSession):
    # گاردهای ایمنی نوع
    if not callback.from_user or not callback.data:
        await callback.answer()
        return

    # واکشی کاربر
    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    # واکشی کانالهای عضویت اجباری این فروشنده
    stmt_channels = select(ForceJoinChannel).where(ForceJoinChannel.vendor_id == user.vendor_id)
    channels = (await db_session.execute(stmt_channels)).scalars().all()

    # اگر کانالی تعریف نشده → مستقیم به مرحله ۲
    if not channels:
        new_callback = callback.model_copy(update={"data": "free_test_servers"})
        await free_test_show_servers(new_callback, db_session)
        return

    # گارد روی bot
    bot = callback.bot
    if bot is None:
        await callback.answer("❌ خطای داخلی رخ داده است.", show_alert=True)
        return

    # بررسی مجدد عضویت کاربر در هر کانال
    unjoined: list[ForceJoinChannel] = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch.chat_id, user_id=user.telegram_id)
            if member.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                unjoined.append(ch)
        except TelegramBadRequest:
            logger.warning(f"Force-join check failed for channel {ch.chat_id}")
            unjoined.append(ch)
        except Exception as e:
            logger.warning(f"Force-join membership check failed for channel {ch.chat_id}: {e}")
            unjoined.append(ch)

    # اگر در همه کانالها عضو بود → مرحله ۲
    if not unjoined:
        new_callback = callback.model_copy(update={"data": "free_test_servers"})
        await free_test_show_servers(new_callback, db_session)
        return

    # هنوز در همه کانالها عضو نشده → فقط پاپآپ هشدار بده و return.
    # کیبورد از قبل رندر شده، نیازی به ویرایش مجدد نیست.
    await callback.answer("❌ شما هنوز در تمام کانالها عضو نشدهاید!", show_alert=True)


# --- مرحله ۲: بررسی واجلبودن + نمایش لیست سرورها ---
@router.callback_query(F.data == "free_test_servers")
async def free_test_show_servers(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user or not callback.data:
        await callback.answer()
        return

    # واکشی کاربر
    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    # بررسی اینکه آیا قبلاً تست دریافت کرده است؟
    if user.has_received_test:
        await callback.answer("❌ شما قبلاً سرویس تست دریافت کردهاید.", show_alert=True)
        return

    # واکشی سرورهای فعال در دسترس این فروشنده (owner یا shared)
    shared_subq = select(VendorServer.server_id).where(VendorServer.vendor_id == user.vendor_id)
    stmt_servers = select(Server).where(
        or_(Server.vendor_id == user.vendor_id, Server.id.in_(shared_subq)),
        Server.is_active == True,
    )
    servers = (await db_session.execute(stmt_servers)).scalars().all()

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=BUTTONS["back_to_main"], callback_data="back_to_main")]
    ])

    if not servers:
        if isinstance(callback.message, types.Message):
            try:
                await callback.message.edit_text(
                    "❌ در حال حاضر سرور فعالی برای تست موجود نیست.",
                    reply_markup=back_kb
                )
            except TelegramBadRequest:
                pass
        await callback.answer()
        return

    # ساخت دکمههای سرورها
    builder = InlineKeyboardBuilder()
    for srv in servers:
        builder.button(text=f"🌍 {srv.name}", callback_data=f"req_test_{srv.id}")
    builder.button(text=BUTTONS["back_to_main"], callback_data="back_to_main")
    builder.adjust(1)

    text = (
        "🎁 <b>دریافت تست رایگان</b>\n\n"
        "لطفاً سرور مورد نظر خود را انتخاب کنید:\n\n"
        "📦 حجم تست: 75 مگابایت\n"
        "⏳ مدت: ۱ روز\n"
        "👥 کاربر همزمان: ۱"
    )
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text=text, reply_markup=builder.as_markup())
        except TelegramBadRequest:
            pass

    await callback.answer()


# --- مرحله ۳: ساخت کانفیگ تست ---
@router.callback_query(F.data.startswith("req_test_"))
async def free_test_generate(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user or not callback.data:
        await callback.answer()
        return

    # پارس شناسه سرور
    server_id_str = callback.data.replace("req_test_", "")
    if not server_id_str.isdigit():
        await callback.answer("❌ شناسه سرور نامعتبر است.", show_alert=True)
        return
    server_id = int(server_id_str)

    # واکشی کاربر
    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    # 🌟 بررسی مجدد (محافظ در برابر رقابت/race-condition)
    if user.has_received_test:
        await callback.answer("❌ شما قبلاً تست دریافت کردهاید.", show_alert=True)
        return

    # واکشی سرور
    stmt_server = select(Server).where(Server.id == server_id)
    server = (await db_session.execute(stmt_server)).scalar_one_or_none()
    if not server or not server.is_active:
        await callback.answer("❌ سرور مورد نظر یافت نشد یا غیرفعال است.", show_alert=True)
        return

    # توقف اسپینر تلگرام
    await callback.answer("⏳ در حال ساخت کانفیگ تست...")

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=BUTTONS["back_to_main"], callback_data="back_to_main")]
    ])

    # گارد روی bot
    bot = callback.bot
    if bot is None:
        if isinstance(callback.message, types.Message):
            try:
                await callback.message.edit_text(
                    "❌ خطای داخلی رخ داده است.",
                    reply_markup=back_kb
                )
            except TelegramBadRequest:
                pass
        return

    # پارامترهای کانفیگ تست
    username = f"TEST_{user.telegram_id}_{int(time.time())}"
    data_limit_bytes = 75 * 1024 * 1024  # 75 MB
    expire_iso = str(int(time.time()) + 86400)  # ۱ روز
    hwid_limit = 1

    client = MarzbanClient(
        base_url=server.panel_url,
        username=server.username,
        password=server.password,
    )

    api_result: Dict[str, Any] | None = None
    sub_url = ""
    try:
        await client.login()
        api_result = await client.create_user(
            username=username,
            data_limit_bytes=data_limit_bytes,
            expire_iso=expire_iso,
            hwid_limit=hwid_limit,
        )
        if api_result:
            sub_url = str(api_result.get("subscription_url", ""))
    except Exception as e:
        logger.error(f"Free test create_user failed for user {user.telegram_id} on server {server.id}: {e}")
        api_result = None
    finally:
        await client.close()

    # 🎁 موفقیت: ارسال QR + ثبت در دیتابیس
    if api_result and sub_url:
        user.has_received_test = True
        await db_session.commit()

        caption = (
            "🎁 <b>کانفیگ تست شما ساخته شد!</b>\n\n"
            f"🖥 سرور: {server.name}\n"
            "💽 حجم: 75 مگابایت\n"
            "⏳ اعتبار: ۱ روز\n"
            "👥 کاربر همزمان: ۱\n\n"
            "🔗 <b>لینک اشتراک:</b>\n"
            f"<code>{sub_url}</code>\n\n"
            "💡 لینک بالا را در برنامه VPN خود وارد کنید."
        )

        # ارسال QR code (همان الگوی admin_handlers.py)
        try:
            import qrcode.constants as _qr_constants  # type: ignore[attr-defined]
            qr = qrcode.QRCode(version=1, error_correction=_qr_constants.ERROR_CORRECT_M, box_size=10, border=4)
            qr.add_data(sub_url)
            qr.make(fit=True)
            qr_image = qr.make_image(fill_color="black", back_color="white")
            stream = io.BytesIO()
            qr_image.save(stream, "PNG")  # type: ignore[call-arg]
            stream.seek(0)
            qr_file = BufferedInputFile(stream.read(), filename="test_sub_qr.png")
            await bot.send_photo(chat_id=user.telegram_id, photo=qr_file, caption=caption)
        except Exception as e:
            logger.error(f"Failed to generate/send test QR: {e}")
            # fallback: ارسال فقط متن
            await bot.send_message(chat_id=user.telegram_id, text=caption)

        # ویرایش پیام جاری به تأیید کوچک
        if isinstance(callback.message, types.Message):
            try:
                await callback.message.edit_text(
                    "✅ کانفیگ تست با موفقیت ساخته شد و در پیوی شما ارسال شد.",
                    reply_markup=back_kb
                )
            except TelegramBadRequest:
                pass
        return

    # ❌ شکست
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(
                "❌ ساخت کانفیگ تست ناموفق بود. لطفاً بعداً مجدداً تلاش کنید.",
                reply_markup=back_kb
            )
        except TelegramBadRequest:
            pass
