import io
import random
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

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
async def cmd_start(message: types.Message, db_session: AsyncSession) -> None:
    if not message.from_user: return

    user_id: int = message.from_user.id
    first_name: str = message.from_user.first_name if message.from_user else MESSAGES["default_user"]

    deep_link_vendor_id: Optional[int] = None
    if message.text:
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
        await message.answer("🛠 فروشگاه در حال راه‌اندازی است. لطفاً بعداً مراجعه کنید.")
        return

    wallet_balance = int(db_user.wallet_balance)
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
async def process_back_to_main(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
    if not callback.from_user: return

    first_name = callback.from_user.first_name if callback.from_user else MESSAGES["default_user"]

    stmt = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt)).scalar_one_or_none()
    wallet_balance = int(user.wallet_balance) if user else 0

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
async def process_buy_new_service(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
    if not callback.from_user: return

    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()

    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    shared_subq = select(VendorServer.server_id).where(VendorServer.vendor_id == user.vendor_id)
    stmt_servers = (
        select(Server)
        .join(Plan, Plan.server_id == Server.id)
        .where(
            Plan.is_active == True,
            Plan.is_deleted == False,
            Server.is_active == True,
            Server.is_deleted == False,
            or_(
                Server.vendor_id == user.vendor_id,
                Server.id.in_(shared_subq),
            ),
        )
        .distinct()
    )
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
async def process_server_selection(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
    if not callback.data or not callback.from_user: return
    srv_id_str = callback.data.replace("sel_srv_", "")
    if not srv_id_str.isdigit(): return
    srv_id = int(srv_id_str)

    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user: return

    shared_subq = select(VendorServer.server_id).where(VendorServer.vendor_id == user.vendor_id)
    stmt_plans = (
        select(Plan)
        .options(selectinload(Plan.server))
        .join(Server, Server.id == Plan.server_id)
        .where(
            Plan.server_id == srv_id,
            Plan.is_active == True,
            Plan.is_deleted == False,
            Server.is_active == True,
            Server.is_deleted == False,
            or_(
                Server.vendor_id == user.vendor_id,
                Server.id.in_(shared_subq),
            ),
        )
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
async def process_plan_selection(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
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

    wallet_balance = int(user.wallet_balance)

    target_vendor = vendor
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
        f"💰 <b>موجودی شما:</b> {wallet_balance:,} تومان\n\n"
    )

    builder = InlineKeyboardBuilder()

    if is_card_missing and wallet_balance < plan.price:
        text = "❌ <b>شماره کارت فروشگاه ثبت نشده است!</b>\n\n" + plan_details + "لطفاً پیش از اقدام به خرید، با پشتیبانی هماهنگ کنید."
    else:
        builder.button(text="🎁 اعمال کد تخفیف / شارژ", callback_data=f"charge_req_{plan.id}")

        if wallet_balance >= plan.price:
            builder.button(text="✅ خرید مستقیم از کیف پول", callback_data=f"wallet_buy_{plan.id}_0")

        text = "🛍 <b>مشخصات سرویس</b>\n\n" + plan_details + "لطفاً یک گزینه را انتخاب کنید:"

    builder.button(text="🔙 بازگشت به سرویس‌ها", callback_data=f"sel_srv_{plan.server_id}")
    builder.adjust(1)

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text=text, reply_markup=builder.as_markup())

    await callback.answer()


# --- ۶. مرحله تخفیف + صدور پیش‌فاکتور ---
@router.callback_query(F.data.startswith("charge_req_"))
async def apply_discount_start(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
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
async def skip_discount(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    if not callback.from_user:
        await callback.answer()
        return
    data = await state.get_data()
    original_price_raw = data.get("original_price")
    original_price = int(original_price_raw) if original_price_raw is not None else 0

    if isinstance(callback.message, types.Message):
        await _create_purchase_invoice(callback.message, state, db_session, 0, original_price)
    await callback.answer()


@router.message(PurchaseStates.waiting_for_discount_code)
async def process_discount_code(message: types.Message, state: FSMContext, db_session: AsyncSession) -> None:
    if not message.text or not message.from_user:
        return

    data = await state.get_data()
    plan_id_raw = data.get("plan_id")
    vendor_id_raw = data.get("vendor_id")
    user_id_raw = data.get("user_id")
    original_price_raw = data.get("original_price")

    # 🌟 گاردِ تایپ‌سیف (محافظت از تبدیل None به int)
    if plan_id_raw is None or vendor_id_raw is None or user_id_raw is None:
        await message.answer("❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.")
        await state.clear()
        return

    plan_id = int(plan_id_raw)
    vendor_id = int(vendor_id_raw)
    user_id_db = int(user_id_raw)
    original_price = int(original_price_raw) if original_price_raw is not None else 0

    code = message.text.strip()
    allowed_vendor_ids: List[int] = [vendor_id]

    stmt_plan_srv = (
        select(Server.vendor_id)
        .join(Plan, Plan.server_id == Server.id)
        .where(Plan.id == plan_id)
    )
    server_owner_id = (await db_session.execute(stmt_plan_srv)).scalar_one_or_none()
    if server_owner_id is not None and server_owner_id not in allowed_vendor_ids:
        allowed_vendor_ids.append(int(server_owner_id))

    dc = (await db_session.execute(
        select(DiscountCode).where(
            DiscountCode.vendor_id.in_(allowed_vendor_ids),
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

    existing_usage = await db_session.scalar(
        select(Transaction).where(
            Transaction.user_id == user_id_db,
            Transaction.discount_code_id == dc.id,
            Transaction.status.in_(["pending", "approved"]),
        )
    )
    if existing_usage is not None:
        await message.answer("❌ شما قبلاً از این کد تخفیف استفاده کرده‌اید.")
        return

    discount_percent = int(dc.discount_percent)
    discount_amount = original_price * discount_percent // 100
    price_after_discount = original_price - discount_amount

    await state.update_data(discount_code_id=dc.id)
    await _create_purchase_invoice(message, state, db_session, discount_percent, price_after_discount)


async def _create_purchase_invoice(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
    discount_percent: int,
    price_after_discount: int,
) -> None:
    data = await state.get_data()
    plan_id_raw = data.get("plan_id")
    user_id_raw = data.get("user_id")
    vendor_id_raw = data.get("vendor_id")
    original_price_raw = data.get("original_price")
    discount_code_id_raw = data.get("discount_code_id")

    # 🌟 گاردِ تایپ‌سیف
    if plan_id_raw is None or user_id_raw is None or vendor_id_raw is None:
        await message.answer("❌ خطایی رخ داد.")
        await state.clear()
        return

    plan_id = int(plan_id_raw)
    user_id_db = int(user_id_raw)
    vendor_id = int(vendor_id_raw)
    original_price = int(original_price_raw) if original_price_raw is not None else 0
    discount_code_id: Optional[int] = int(discount_code_id_raw) if discount_code_id_raw is not None else None

    user = (await db_session.execute(select(User).where(User.id == user_id_db))).scalar_one_or_none()
    plan = (await db_session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    vendor = (await db_session.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one_or_none()

    if not user or not plan or not vendor:
        await message.answer("❌ خطایی رخ داد.")
        await state.clear()
        return

    wallet_balance = int(user.wallet_balance)
    dc_id_str = str(discount_code_id) if discount_code_id else "0"

    discount_line = ""
    if discount_percent > 0:
        discount_amount = original_price - price_after_discount
        discount_line = f"🎯 تخفیف ({discount_percent}%): <code>-{discount_amount:,}</code> تومان\n"

    # 🟢 حالت اول: کیف پول به تنهایی تمام هزینه را پوشش می‌دهد
    if wallet_balance >= price_after_discount:
        text = (
            f"🧾 <b>تایید نهایی خرید ({plan.title})</b>\n\n"
            f"💵 قیمت اصلی: <code>{original_price:,}</code> تومان\n"
            f"{discount_line}"
            f"💰 مبلغ نهایی پرداختی: <code>{price_after_discount:,}</code> تومان\n"
            f"💳 موجودی کیف پول شما: <code>{wallet_balance:,}</code> تومان\n\n"
            "✅ <b>موجودی شما برای این خرید کافی است.</b>\n"
            "برای کسر از کیف پول و دریافت آنی کانفیگ، روی دکمه زیر کلیک کنید:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ پرداخت اتوماتیک و دریافت کانفیگ", callback_data=f"wallet_buy_{plan.id}_{dc_id_str}")],
            [InlineKeyboardButton(text="❌ لغو", callback_data="back_to_main")]
        ])
        await message.answer(text, reply_markup=kb)
        return

    # 🔴 حالت دوم: موجودی کافی نیست (فاکتور پرداخت کارت به کارت صادر می‌شود)
    wallet_used = wallet_balance
    payable_via_card = price_after_discount - wallet_used

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
    amount_with_fee = payable_via_card + random_fee
    amount_with_fee_rial = amount_with_fee * 10

    new_tx = Transaction(
        user_id=user_id_db,
        vendor_id=target_vendor_id,
        origin_vendor_id=vendor.id if target_vendor_id != vendor.id else None,
        plan_id=plan.id,
        amount=payable_via_card,
        original_amount=original_price,
        discount_percent=discount_percent,
        discount_code_id=discount_code_id,
        destination_card=target_vendor.card_number,
        status="pending",
    )
    db_session.add(new_tx)
    await db_session.commit()
    await db_session.refresh(new_tx)

    await state.update_data(
        transaction_id=new_tx.id,
        wallet_used=wallet_used
    )

    wallet_line = ""
    if wallet_used > 0:
        wallet_line = f"💎 کسر از کیف پول: <code>-{wallet_used:,}</code> تومان\n"

    text = (
        f"🧾 <b>فاکتور خرید ({plan.title})</b>\n\n"
        f"💵 قیمت اصلی: <code>{original_price:,}</code> تومان\n"
        f"{discount_line}"
        f"{wallet_line}"
        f"💰 مبلغ قابل پرداخت: <code>{payable_via_card:,}</code> تومان\n\n"
        f"💳 لطفاً مبلغ زیر را به شماره کارت <b>{target_vendor.name}</b> واریز کنید:\n"
        f"<code>{target_vendor.card_number}</code>\n\n"
        f"💵 <b>مبلغ دقیق واریز:</b> <code>{amount_with_fee:,}</code> تومان - (<code>{amount_with_fee_rial:,}</code> ریال)\n\n"
        f"⚠️ مبلغ {random_fee} تومان جهت شناسایی سیستمی اضافه شده است.\n"
        "📸 پس از واریز، عکس رسید را همینجا بفرستید تا کانفیگ صادر شود:"
    )

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data="back_to_main")]])
    await message.answer(text, reply_markup=cancel_kb)
    await state.set_state(WalletChargeStates.waiting_for_receipt)


# --- ۶.۵. هندلر پرداخت مستقیم و اتوماتیک از کیف پول ---
@router.callback_query(F.data.startswith("wallet_buy_"))
async def process_wallet_buy(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
    if not callback.data or not callback.from_user: return

    parts = callback.data.split("_")
    if len(parts) != 4: return
    plan_id = int(parts[2])
    dc_id = int(parts[3])

    user = (await db_session.execute(select(User).where(User.telegram_id == callback.from_user.id))).scalar_one_or_none()
    plan = (await db_session.execute(select(Plan).options(selectinload(Plan.server)).where(Plan.id == plan_id))).scalar_one_or_none()

    if not user or not plan:
        await callback.answer("❌ خطایی رخ داد.", show_alert=True)
        return

    original_price = int(plan.price)
    discount_percent = 0
    if dc_id > 0:
        dc = (await db_session.execute(select(DiscountCode).where(DiscountCode.id == dc_id))).scalar_one_or_none()
        if dc: discount_percent = dc.discount_percent

    discount_amount = original_price * discount_percent // 100
    final_price = original_price - discount_amount

    if user.wallet_balance < final_price:
        await callback.answer("❌ موجودی کیف پول شما تغییر کرده و کافی نیست!", show_alert=True)
        return

    user.wallet_balance -= final_price

    new_tx = Transaction(
        user_id=user.id,
        vendor_id=plan.vendor_id,
        plan_id=plan.id,
        amount=0,
        original_amount=original_price,
        discount_percent=discount_percent,
        discount_code_id=dc_id if dc_id > 0 else None,
        status="approved"
    )
    db_session.add(new_tx)
    await db_session.commit()
    await db_session.refresh(new_tx)

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text("⏳ در حال ارتباط با سرور و ساخت کانفیگ...")

    server = plan.server
    username = f"U_{user.telegram_id}_{new_tx.id}_{int(new_tx.created_at.timestamp())}"
    data_limit_bytes = int(plan.volume_gb * (1024**3)) if plan.volume_gb > 0 else 0

    # 🌟 رفع خطای Type-Hinting مرزبان با ارسال String صریح به جای None
    expire_iso: str = str(int(time.time()) + (plan.days * 86400)) if plan.days > 0 else "0"

    client = MarzbanClient(
        base_url=server.panel_url,
        username=server.username,
        password=server.password,
    )

    api_result = None
    sub_url = ""
    try:
        await client.login()
        api_result = await client.create_user(
            username=username,
            data_limit_bytes=data_limit_bytes,
            expire_iso=expire_iso,
            hwid_limit=plan.user_limit if plan.user_limit > 0 else 0,
        )
        if api_result:
            sub_url = str(api_result.get("subscription_url", ""))
    except Exception as e:
        logger.error(f"Config creation via Wallet Buy failed: {e}")
    finally:
        await client.close()

    if api_result and sub_url:
        caption = (
            f"✅ <b>خرید شما با موفقیت انجام شد!</b>\n\n"
            f"🛍 پلن: {plan.title}\n"
            f"🖥 سرور: {server.name}\n\n"
            f"🔗 <b>لینک اشتراک:</b>\n<code>{sub_url}</code>"
        )
        try:
            import qrcode.constants as _qr_constants # type: ignore[import-untyped]
            qr = qrcode.QRCode(version=1, error_correction=_qr_constants.ERROR_CORRECT_M, box_size=10, border=4) # type: ignore[call-overload]
            qr.add_data(sub_url)
            qr.make(fit=True)
            qr_image = qr.make_image(fill_color="black", back_color="white")
            stream = io.BytesIO()
            qr_image.save(stream, "PNG") # type: ignore[call-arg]
            stream.seek(0)
            qr_file = BufferedInputFile(stream.read(), filename="sub_qr.png")
            if isinstance(callback.message, types.Message):
                await callback.message.delete()
            if callback.bot is not None:
                await callback.bot.send_photo(chat_id=user.telegram_id, photo=qr_file, caption=caption)
        except Exception:
            if isinstance(callback.message, types.Message):
                await callback.message.edit_text(caption)
    else:
        user.wallet_balance += final_price
        new_tx.status = "failed"
        await db_session.commit()
        if isinstance(callback.message, types.Message):
            await callback.message.edit_text("❌ متاسفانه در ارتباط با سرور مشکلی پیش آمد. مبلغ به طور کامل به کیف پول شما برگشت داده شد.")


# --- ۷. دریافت عکس رسید و ارسال به پی‌وی ادمین ---
@router.message(WalletChargeStates.waiting_for_receipt, F.photo)
async def process_receipt_photo(message: types.Message, state: FSMContext, db_session: AsyncSession) -> None:
    if not message.photo or not message.from_user or not message.bot: return

    data = await state.get_data()
    tx_id_raw = data.get("transaction_id")
    wallet_used_raw = data.get("wallet_used")

    # 🌟 گارد امنیتی Type-Safe
    if tx_id_raw is None:
        await message.answer("❌ خطایی رخ داد. لطفاً مجددا تلاش کنید.")
        await state.clear()
        await cmd_start(message, db_session)
        return

    transaction_id = int(tx_id_raw)
    wallet_used = int(wallet_used_raw) if wallet_used_raw is not None else 0

    photo_file_id = message.photo[-1].file_id

    stmt = select(Transaction).options(selectinload(Transaction.plan).selectinload(Plan.server)).where(Transaction.id == transaction_id)
    transaction = (await db_session.execute(stmt)).scalar_one_or_none()

    if not transaction:
        await message.answer("❌ فاکتور شما در سیستم یافت نشد.")
        await state.clear()
        await cmd_start(message, db_session)
        return

    if wallet_used > 0:
        stmt_user = select(User).where(User.id == transaction.user_id)
        user = (await db_session.execute(stmt_user)).scalar_one_or_none()
        if not user or user.wallet_balance < wallet_used:
            await message.answer("❌ موجودی کیف پول شما تغییر کرده است. این فاکتور نامعتبر شد.")
            transaction.status = "canceled"
            await db_session.commit()
            await state.clear()
            return

        user.wallet_balance -= wallet_used

    stmt_vendor = select(Vendor).where(Vendor.id == transaction.vendor_id)
    vendor = (await db_session.execute(stmt_vendor)).scalar_one_or_none()
    if not vendor: return

    transaction.receipt_file_id = photo_file_id
    transaction.status = "pending"
    await db_session.commit()

    admin_tg_id = vendor.telegram_id
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

    amount_rial = int(transaction.amount) * 10
    caption = (
        f"🧾 <b>فیش واریزی جدید</b>\n\n"
        f"👤 آیدی کاربر: <code>{message.from_user.id}</code>\n"
        f"💰 مبلغ واریز (رسید): <code>{int(transaction.amount):,}</code> تومان - (<code>{amount_rial:,}</code> ریال)\n"
    )
    if wallet_used > 0:
        caption += f"💎 کسر شده از کیف پول: <code>{wallet_used:,}</code> تومان\n"

    if transaction.discount_percent and transaction.discount_percent > 0:
        caption += (
            f"🎯 تخفیف ({transaction.discount_percent}%)\n"
        )
    caption += f"📌 بابت: <b>{plan_text}</b>"

    try:
        await message.bot.send_photo(chat_id=admin_tg_id, photo=photo_file_id, caption=caption, reply_markup=kb)
    except Exception as e:
        logger.error(f"Failed to send receipt to admin: {e}")

    await message.answer("✅ <b>فیش شما با موفقیت ثبت شد!</b>\n\nبه محض تایید ادمین، نتیجه به شما اعلام خواهد شد.")
    await state.clear()
    await cmd_start(message, db_session)


@router.message(WalletChargeStates.waiting_for_receipt)
async def process_receipt_invalid(message: types.Message) -> None:
    await message.answer("❌ لطفاً رسید خود را به صورت یک **عکس (Photo)** ارسال کنید.")


# --- ۸. هندلرهای شارژ عادی کیف پول ---
@router.callback_query(F.data == "charge_wallet")
async def ask_charge_amount(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    if not callback.from_user: return
    stmt = select(User, Vendor).join(Vendor, User.vendor_id == Vendor.id).where(User.telegram_id == callback.from_user.id)
    row = (await db_session.execute(stmt)).first()
    if not row: return

    _, vendor_row = row
    target_vendor = vendor_row
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
async def process_charge_amount(message: types.Message, state: FSMContext, db_session: AsyncSession) -> None:
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

    target_vendor_id = target_vendor.id
    origin_vendor_id: Optional[int] = None
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

    await state.update_data(transaction_id=new_tx.id, wallet_used=0)
    final_amount_rial = final_amount * 10
    text = (
        f"🧾 <b>اطلاعات پرداخت</b>\n\n"
        f"💳 لطفاً مبلغ زیر را به شماره کارت <b>{target_vendor.name}</b> واریز کنید:\n"
        f"<code>{target_vendor.card_number}</code>\n\n"
        f"💵 <b>مبلغ دقیق واریز:</b> <code>{final_amount:,}</code> تومان - (<code>{final_amount_rial:,}</code> ریال)\n\n"
        f"⚠️ مبلغ {random_fee} تومان جهت شناسایی سیستمی اضافه شده است.\n"
        "📸 پس از واریز، عکس رسید خود را همینجا بفرستید:"
    )

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data="back_to_main")]])
    await message.answer(text, reply_markup=cancel_kb)
    await state.set_state(WalletChargeStates.waiting_for_receipt)


# ==========================================
# 🌟 پروفایل کاربر
# ==========================================
@router.callback_query(F.data == "user_profile")
async def process_user_profile(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
    if not callback.from_user:
        await callback.answer()
        return

    stmt = (
        select(User)
        .options(selectinload(User.vendor))
        .where(User.telegram_id == callback.from_user.id)
    )
    user = (await db_session.execute(stmt)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

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
# 🌟 پشتیبانی / تیکتینگ
# ==========================================
@router.callback_query(F.data == "support")
async def process_support(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    if not callback.from_user:
        await callback.answer()
        return

    stmt = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    stmt_vendor = select(Vendor).where(Vendor.id == user.vendor_id)
    vendor_row = (await db_session.execute(stmt_vendor)).scalar_one_or_none()
    final_vendor_id: int = user.vendor_id
    if vendor_row and vendor_row.redirect_target_id:
        stmt_target = select(Vendor).where(Vendor.id == vendor_row.redirect_target_id)
        target_vendor = (await db_session.execute(stmt_target)).scalar_one_or_none()
        if target_vendor:
            final_vendor_id = target_vendor.id

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
async def process_support_message(message: types.Message, state: FSMContext, db_session: AsyncSession) -> None:
    if not message.text or not message.from_user:
        return

    data = await state.get_data()
    ticket_user_id_raw = data.get("ticket_user_id")
    ticket_vendor_id_raw = data.get("ticket_vendor_id")

    # 🌟 گاردِ تایپ‌سیف
    if ticket_user_id_raw is None or ticket_vendor_id_raw is None:
        await message.answer("❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.")
        await state.clear()
        await cmd_start(message, db_session)
        return

    message_text = message.text.strip()
    if not message_text:
        await message.answer("❌ پیام نمیتواند خالی باشد.")
        return

    new_ticket = Ticket(
        user_id=int(ticket_user_id_raw),
        vendor_id=int(ticket_vendor_id_raw),
        message_text=message_text,
        status="pending",
    )
    db_session.add(new_ticket)
    await db_session.commit()
    await state.clear()

    await message.answer("✅ پیام شما برای پشتیبانی ارسال شد.")
    await cmd_start(message, db_session)


# ==========================================
# 🌟 سرویسهای من + مانیتورینگ زنده
# ==========================================
@router.callback_query(F.data == "my_services")
async def process_my_services(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
    if not callback.from_user:
        await callback.answer()
        return

    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

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
async def process_service_monitoring(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    tx_id_str = callback.data.replace("usr_srv_", "")
    if not tx_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    tx_id = int(tx_id_str)

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

    if tx.user.telegram_id != callback.from_user.id:
        await callback.answer("❌ دسترسی غیرمجاز.", show_alert=True)
        return

    server: Server = tx.plan.server
    username = f"U_{tx.user.telegram_id}_{tx.id}_{int(tx.created_at.timestamp())}"

    client = MarzbanClient(
        base_url=server.panel_url,
        username=server.username,
        password=server.password,
    )
    api_data: Optional[Dict[str, Any]] = None
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
        data_limit = int(api_data.get("data_limit") or 0)
        used_traffic = int(api_data.get("used_traffic") or 0)
        expire_val = api_data.get("expire")
        status = str(api_data.get("status") or "unknown")

        total_gb = data_limit / (1024 ** 3) if data_limit else 0.0
        used_gb = used_traffic / (1024 ** 3) if used_traffic else 0.0
        remaining_gb = max(total_gb - used_gb, 0.0) if data_limit else 0.0

        remaining_days = 0
        has_expire = False
        if expire_val is not None:
            try:
                if isinstance(expire_val, str) and "T" in expire_val:
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
            pass
    await callback.answer("🔄 اطلاعات بروزرسانی شد")


# ==========================================
# 🎁 دریافت تست رایگان + Force-Join
# ==========================================
@router.callback_query(F.data == "free_test_start")
async def free_test_start(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
    if not callback.from_user or not callback.data:
        await callback.answer()
        return

    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    stmt_channels = select(ForceJoinChannel).where(ForceJoinChannel.vendor_id == user.vendor_id)
    channels = (await db_session.execute(stmt_channels)).scalars().all()

    if not channels:
        new_callback = callback.model_copy(update={"data": "free_test_servers"})
        await free_test_show_servers(new_callback, db_session)
        return

    bot = callback.bot
    if bot is None:
        await callback.answer("❌ خطای داخلی رخ داده است.", show_alert=True)
        return

    unjoined: List[ForceJoinChannel] = []
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

    if not unjoined:
        new_callback = callback.model_copy(update={"data": "free_test_servers"})
        await free_test_show_servers(new_callback, db_session)
        return

    kb_rows: List[List[InlineKeyboardButton]] = []
    for ch in unjoined:
        kb_rows.append([InlineKeyboardButton(text=ch.title, url=ch.url)])
    kb_rows.append([InlineKeyboardButton(text="✅ بررسی مجدد عضویت", callback_data="free_test_verify")])
    new_kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    text = "🔒 برای دریافت تست رایگان، ابتدا در کانالهای زیر عضو شوید:"

    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text=text, reply_markup=new_kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.callback_query(F.data == "free_test_verify")
async def free_test_verify(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
    if not callback.from_user or not callback.data:
        await callback.answer()
        return

    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    stmt_channels = select(ForceJoinChannel).where(ForceJoinChannel.vendor_id == user.vendor_id)
    channels = (await db_session.execute(stmt_channels)).scalars().all()

    if not channels:
        new_callback = callback.model_copy(update={"data": "free_test_servers"})
        await free_test_show_servers(new_callback, db_session)
        return

    bot = callback.bot
    if bot is None:
        await callback.answer("❌ خطای داخلی رخ داده است.", show_alert=True)
        return

    unjoined: List[ForceJoinChannel] = []
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

    if not unjoined:
        new_callback = callback.model_copy(update={"data": "free_test_servers"})
        await free_test_show_servers(new_callback, db_session)
        return

    await callback.answer("❌ شما هنوز در تمام کانالها عضو نشدهاید!", show_alert=True)


@router.callback_query(F.data == "free_test_servers")
async def free_test_show_servers(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
    if not callback.from_user or not callback.data:
        await callback.answer()
        return

    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    if user.has_received_test:
        await callback.answer("❌ شما قبلاً سرویس تست دریافت کردهاید.", show_alert=True)
        return

    shared_subq = select(VendorServer.server_id).where(VendorServer.vendor_id == user.vendor_id)
    stmt_servers = select(Server).where(
        or_(Server.vendor_id == user.vendor_id, Server.id.in_(shared_subq)),
        Server.is_active == True,
        Server.is_deleted == False,
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


@router.callback_query(F.data.startswith("req_test_"))
async def free_test_generate(callback: types.CallbackQuery, db_session: AsyncSession) -> None:
    if not callback.from_user or not callback.data:
        await callback.answer()
        return

    server_id_str = callback.data.replace("req_test_", "")
    if not server_id_str.isdigit():
        await callback.answer("❌ شناسه سرور نامعتبر است.", show_alert=True)
        return
    server_id = int(server_id_str)

    stmt_user = select(User).where(User.telegram_id == callback.from_user.id)
    user = (await db_session.execute(stmt_user)).scalar_one_or_none()
    if not user:
        await callback.answer("❌ اطلاعات شما یافت نشد.", show_alert=True)
        return

    if user.has_received_test:
        await callback.answer("❌ شما قبلاً تست دریافت کردهاید.", show_alert=True)
        return

    stmt_server = select(Server).where(Server.id == server_id)
    server = (await db_session.execute(stmt_server)).scalar_one_or_none()
    if not server or not server.is_active:
        await callback.answer("❌ سرور مورد نظر یافت نشد یا غیرفعال است.", show_alert=True)
        return

    await callback.answer("⏳ در حال ساخت کانفیگ تست...")

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=BUTTONS["back_to_main"], callback_data="back_to_main")]
    ])

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

    username = f"TEST_{user.telegram_id}_{int(time.time())}"
    data_limit_bytes = 75 * 1024 * 1024

    # 🌟 رفع خطای Type-Hinting مرزبان با استفاده از String صریح
    expire_iso: str = str(int(time.time()) + 86400)
    hwid_limit = 1

    client = MarzbanClient(
        base_url=server.panel_url,
        username=server.username,
        password=server.password,
    )

    api_result: Optional[Dict[str, Any]] = None
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

        try:
            import qrcode.constants as _qr_constants # type: ignore[import-untyped]
            qr = qrcode.QRCode(version=1, error_correction=_qr_constants.ERROR_CORRECT_M, box_size=10, border=4) # type: ignore[call-overload]
            qr.add_data(sub_url)
            qr.make(fit=True)
            qr_image = qr.make_image(fill_color="black", back_color="white")
            stream = io.BytesIO()
            qr_image.save(stream, "PNG") # type: ignore[call-arg]
            stream.seek(0)
            qr_file = BufferedInputFile(stream.read(), filename="test_sub_qr.png")
            await bot.send_photo(chat_id=user.telegram_id, photo=qr_file, caption=caption)
        except Exception as e:
            logger.error(f"Failed to generate/send test QR: {e}")
            await bot.send_message(chat_id=user.telegram_id, text=caption)

        if isinstance(callback.message, types.Message):
            try:
                await callback.message.edit_text(
                    "✅ کانفیگ تست با موفقیت ساخته شد و در پیوی شما ارسال شد.",
                    reply_markup=back_kb
                )
            except TelegramBadRequest:
                pass
        return

    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(
                "❌ ساخت کانفیگ تست ناموفق بود. لطفاً بعداً مجدداً تلاش کنید.",
                reply_markup=back_kb
            )
        except TelegramBadRequest:
            pass
