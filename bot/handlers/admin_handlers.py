import asyncio
import io
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional, Sequence

import qrcode
from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.states import (
    AddPlanStates,
    AddServerStates,
    AdminCustomerStates,
    AdminDiscountStates,
    AdminForceJoinStates,
    AdminPartnerStates,
    AdminRedirectStates,
    AdminStates,
    AdminTicketStates,
    EditPlanStates,
)
from core.database.models import (
    DiscountCode,
    ForceJoinChannel,
    Plan,
    Server,
    Ticket,
    Transaction,
    User,
    Vendor,
    VendorServer,
)
from core.services.panel_client import MarzbanClient

logger = logging.getLogger(__name__)

router = Router()


# ==========================================
# 🌟 توابع کمکی و منوی اصلی
# ==========================================
async def get_admin_panel_content(user_id: int, db_session: AsyncSession):
    stmt = select(Vendor).where(Vendor.telegram_id == user_id)
    vendor = (await db_session.execute(stmt)).scalar_one_or_none()
    if not vendor:
        return None, None

    owner_id_str = os.getenv("OWNER_ID")
    is_owner = bool(owner_id_str and user_id == int(owner_id_str))

    # 🌟 شمارش فیش‌ها و تیکت‌های در انتظار برای نمایش Badge
    pending_tx_count = await db_session.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.vendor_id == vendor.id,
            Transaction.status == "pending",
            Transaction.receipt_file_id.is_not(None),
        )
    )
    pending_ticket_count = await db_session.scalar(
        select(func.count(Ticket.id)).where(
            Ticket.vendor_id == vendor.id,
            Ticket.status == "pending",
        )
    )

    pending_tx_count = int(pending_tx_count or 0)
    pending_ticket_count = int(pending_ticket_count or 0)

    receipts_label = f"💳 بررسی فیشها ({pending_tx_count})"
    tickets_label = f"📨 پیامهای پشتیبانی ({pending_ticket_count})"

    kb = [
        [
            InlineKeyboardButton(
                text="👥 مدیریت مشتریان", callback_data="admin_my_customers"
            ),
            InlineKeyboardButton(text=receipts_label, callback_data="admin_receipts"),
        ],
        [
            InlineKeyboardButton(
                text="🖥 مدیریت سرورها", callback_data="admin_view_servers"
            ),
            InlineKeyboardButton(
                text="🛍 مدیریت پلنها", callback_data="admin_manage_plans"
            ),
        ],
        [
            InlineKeyboardButton(
                text="⚙️ اطلاعات پرداخت من", callback_data="admin_payment_info"
            ),
            InlineKeyboardButton(text="📊 گزارش مالی", callback_data="admin_reports"),
        ],
        [
            InlineKeyboardButton(
                text="🎁 کدهای تخفیف", callback_data="admin_discounts"
            ),
            InlineKeyboardButton(
                text="🔄 تنظیمات ریدایرکت", callback_data="admin_redirect"
            ),
        ],
        [
            InlineKeyboardButton(
                text=tickets_label, callback_data="admin_support_tickets"
            ),
            InlineKeyboardButton(
                text="🔒 عضویت اجباری", callback_data="admin_force_join"
            ),
        ],
    ]

    if is_owner:
        kb.append(
            [
                InlineKeyboardButton(
                    text="👑 مدیریت شرکا", callback_data="owner_manage_vendors"
                )
            ]
        )

    kb.append(
        [InlineKeyboardButton(text="🔙 بستن پنل", callback_data="close_admin_panel")]
    )
    reply_markup = InlineKeyboardMarkup(inline_keyboard=kb)

    status_text = "🟢 فعال" if vendor.is_active else "🔴 غیرفعال"
    redirect_text = "ندارد" if not vendor.redirect_target_id else "روشن 🔄"

    text = (
        f"👨‍💼 <b>پنل مدیریت شرکا</b>\n\n"
        f"👤 نام فروشگاه: <b>{vendor.name}</b>\n"
        f"وضعیت اکانت: {status_text}\n"
        f"حالت ریدایرکت: {redirect_text}\n\n"
        "لطفاً یک گزینه را انتخاب کنید:"
    )
    return text, reply_markup


@router.message(Command(commands=["admin", "panel"]))
async def cmd_admin_panel(message: types.Message, db_session: AsyncSession):
    if not message.from_user:
        return
    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if not text:
        await message.answer("❌ شما دسترسی به پنل مدیریت ندارید.")
        return
    await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "back_to_admin_panel")
async def callback_back_to_admin_panel(
    callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession
):
    await state.clear()
    if not callback.from_user:
        return
    text, reply_markup = await get_admin_panel_content(
        callback.from_user.id, db_session
    )
    if text and isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=reply_markup)
    await callback.answer()


@router.callback_query(F.data == "close_admin_panel")
async def close_panel(callback: types.CallbackQuery):
    if isinstance(callback.message, types.Message):
        await callback.message.delete()
    await callback.answer()


# ==========================================
# 🌟 لیست فیش‌های در انتظار (Pending Receipts)
# ==========================================
@router.callback_query(F.data == "admin_receipts")
async def admin_receipts_list(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    stmt = (
        select(Transaction)
        .options(
            selectinload(Transaction.user),
            selectinload(Transaction.plan),
        )
        .where(
            Transaction.vendor_id == vendor.id,
            Transaction.status == "pending",
            Transaction.receipt_file_id.is_not(None),
        )
        .order_by(Transaction.created_at.desc())
    )
    transactions = (await db_session.execute(stmt)).scalars().all()

    if not transactions:
        empty_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
                    )
                ]
            ]
        )
        list_text = "💳 <b>فیشهای در انتظار</b>\n\nهیچ فیش جدیدی وجود ندارد."
        if isinstance(callback.message, types.Message):
            if callback.message.photo:
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except TelegramBadRequest:
                    pass
                await callback.message.answer(text=list_text, reply_markup=empty_kb)
            else:
                try:
                    await callback.message.edit_text(list_text, reply_markup=empty_kb)
                except TelegramBadRequest:
                    pass
        await callback.answer()
        return

    kb: list[list[InlineKeyboardButton]] = []
    for tx in transactions:
        user_display = tx.user.telegram_id if tx.user else "نامشخص"
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"🧾 فیش کاربر {user_display} - {int(tx.amount):,} ت",
                    callback_data=f"adm_rcp_{tx.id}",
                )
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
            )
        ]
    )

    list_text = "💳 <b>فیشهای در انتظار</b>\n\nلطفاً یک فیش را برای بررسی انتخاب کنید:"
    list_kb = InlineKeyboardMarkup(inline_keyboard=kb)

    if isinstance(callback.message, types.Message):
        if callback.message.photo:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            await callback.message.answer(text=list_text, reply_markup=list_kb)
        else:
            try:
                await callback.message.edit_text(text=list_text, reply_markup=list_kb)
            except TelegramBadRequest:
                pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rcp_"))
async def admin_receipt_detail(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    tx_id_str = callback.data.replace("adm_rcp_", "")
    if not tx_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    tx_id = int(tx_id_str)

    stmt = (
        select(Transaction)
        .options(
            selectinload(Transaction.user),
            selectinload(Transaction.plan),
        )
        .where(Transaction.id == tx_id)
    )
    tx = (await db_session.execute(stmt)).scalar_one_or_none()
    if not tx or tx.status != "pending":
        await callback.answer(
            "❌ این فیش قبلاً بررسی شده یا وجود ندارد.", show_alert=True
        )
        return

    bot = callback.bot
    if not bot:
        await callback.answer("❌ خطای سیستمی: بات در دسترس نیست.", show_alert=True)
        return

    user = tx.user
    if not user:
        await callback.answer("❌ کاربر مرتبط یافت نشد.", show_alert=True)
        return

    plan_text = "شارژ عادی کیف پول"
    if tx.plan:
        plan_text = f"{tx.plan.title}"

    random_fee = (tx.id * 73) % 899 + 1
    amount_with_fee = int(tx.amount) + random_fee
    amount_with_fee_rial = amount_with_fee * 10

    caption = (
        f"🧾 <b>بررسی فیش</b>\n\n"
        f"👤 <b>کاربر:</b> <code>{user.telegram_id}</code>\n"
        f"💰 <b>مبلغ واریز به کارت:</b> <code>{amount_with_fee:,}</code> تومان (<code>{amount_with_fee_rial:,}</code> ریال)\n"
    )

    if tx.discount_percent and tx.discount_percent > 0:
        caption += (
            f"💵 <b>قیمت اصلی پلن:</b> <code>{int(tx.original_amount):,}</code> تومان\n"
            f"🎯 <b>درصد تخفیف:</b> {tx.discount_percent}%\n"
        )
    caption += (
        f"📌 <b>بابت:</b> {plan_text}\n"
        f"🕒 <b>زمان:</b> {tx.created_at.strftime('%Y-%m-%d %H:%M')}\n"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تایید فیش", callback_data=f"admin_approve_tx_{tx.id}"
                ),
                InlineKeyboardButton(
                    text="❌ رد فیش", callback_data=f"admin_reject_tx_{tx.id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت به لیست", callback_data="admin_receipts"
                )
            ],
        ]
    )

    if isinstance(callback.message, types.Message):
        await callback.message.edit_reply_markup(reply_markup=None)

    if tx.receipt_file_id:
        try:
            await bot.send_photo(
                chat_id=callback.from_user.id,
                photo=tx.receipt_file_id,
                caption=caption,
                reply_markup=kb,
            )
        except Exception as e:
            logger.error(f"Failed to send receipt photo for tx {tx_id}: {e}")
            await bot.send_message(
                chat_id=callback.from_user.id, text=caption, reply_markup=kb
            )
    else:
        await bot.send_message(
            chat_id=callback.from_user.id, text=caption, reply_markup=kb
        )

    await callback.answer()


# ==========================================
# 🌟 تایید و رد فیش (مجهز به سیستم محاسباتی هوشمند)
# ==========================================
@router.callback_query(F.data.startswith("admin_approve_tx_"))
async def admin_approve_transaction(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    tx_id_str = callback.data.replace("admin_approve_tx_", "")
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

    if not tx or tx.status != "pending":
        await callback.answer(
            "❌ این فیش قبلاً بررسی شده یا وجود ندارد.", show_alert=True
        )
        return

    user = tx.user
    if not user:
        await callback.answer("❌ کاربر مرتبط با این فیش یافت نشد.", show_alert=True)
        return

    bot = callback.bot
    if not bot:
        await callback.answer("❌ خطای سیستمی: بات در دسترس نیست.", show_alert=True)
        return

    tx.status = "approved"

    random_fee = (tx.id * 73) % 899 + 1
    if not tx.plan_id:
        # 🌟 شارژ عادی کیف پول (بدون خرید پلن)
        user.wallet_balance += (int(tx.amount) + random_fee)
        await db_session.commit()

        if isinstance(callback.message, types.Message):
            try:
                await callback.message.delete()
            except TelegramBadRequest:
                pass

        await bot.send_message(
            chat_id=user.telegram_id,
            text=(
                f"✅ <b>واریزی شما تایید شد!</b>\n\n"
                f"مبلغ <code>{int(tx.amount):,}</code> تومان به کیف پول شما اضافه گردید.\n"
                f"موجودی فعلی: <code>{int(user.wallet_balance):,}</code> تومان"
            ),
        )
        tx.receipt_file_id = None
        await db_session.commit()
    else:
        # 🌟 خرید پلن (مخلوط کارت + کیف پول)
        plan = tx.plan
        if not plan or not plan.server:
            await callback.answer("❌ پلن یا سرور مرتبط یافت نشد.", show_alert=True)
            return
        server = plan.server

        # 🌐 ساخت کاربر واقعی در پنل Marzban
        username = f"U_{user.telegram_id}_{tx.id}_{int(tx.created_at.timestamp())}"
        expire_timestamp = (
            str(int(time.time()) + (plan.days * 86400)) if plan.days > 0 else "0"
        )
        data_limit_bytes = int(plan.volume_gb * 1073741824) if plan.volume_gb > 0 else 0

        sub_url = ""
        client = MarzbanClient(
            base_url=server.panel_url,
            username=server.username,
            password=server.password,
        )
        try:
            await client.login()
            api_result = await client.create_user(
                username=username,
                data_limit_bytes=data_limit_bytes,
                expire_iso=expire_timestamp,
                hwid_limit=plan.user_limit,
            )
            sub_url = str(api_result.get("subscription_url", ""))
            if not sub_url:
                sub_url = "خطا در دریافت لینک اشتراک از پنل."
        except Exception as e:
            logger.error(f"Failed to create Marzban user {username}: {e}")
            sub_url = f"خطا در ارتباط با پنل: {e}"
        finally:
            await client.close()

        if isinstance(callback.message, types.Message):
            try:
                await callback.message.delete()
            except TelegramBadRequest:
                pass

        msg_to_user = (
            f"✅ <b>پرداخت شما تایید شد!</b>\n\n🛍 <b>سرویس:</b> {plan.title}\n"
        )

        if tx.discount_percent and tx.discount_percent > 0:
            discount_amount = int(tx.original_amount) * int(tx.discount_percent) // 100
            msg_to_user += (
                f"💵 قیمت اصلی: <code>{int(tx.original_amount):,}</code> تومان\n"
                f"🎯 تخفیف ({tx.discount_percent}%): <code>-{discount_amount:,}</code> تومان\n"
            )

        msg_to_user += (
            f"🔗 <b>لینک اشتراک شما:</b>\n<code>{sub_url}</code>\n\n"
            f"💡 آموزش اتصال: ابتدا لینک بالا را کپی کرده و در نرم‌افزار خود وارد کنید."
        )

        # تولید QR Code
        if isinstance(callback.message, types.Message):
            try:
                import qrcode.constants as _qr_constants  # type: ignore[import-untyped]

                qr = qrcode.QRCode(
                    version=1,
                    error_correction=_qr_constants.ERROR_CORRECT_M,
                    box_size=10,
                    border=4,
                )  # type: ignore[call-overload]
                qr.add_data(sub_url)
                qr.make(fit=True)
                qr_image = qr.make_image(fill_color="black", back_color="white")
                stream = io.BytesIO()
                qr_image.save(stream, "PNG")  # type: ignore[call-arg]
                stream.seek(0)
                qr_file = BufferedInputFile(stream.read(), filename="sub_qr.png")

                await bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=qr_file,
                    caption=msg_to_user,
                )
            except Exception as e:
                logger.error(f"Failed to generate/send QR code for tx {tx_id}: {e}")
                await bot.send_message(chat_id=user.telegram_id, text=msg_to_user)

        tx.receipt_file_id = None
        await db_session.commit()

    await callback.answer()


@router.callback_query(F.data.startswith("admin_reject_tx_"))
async def admin_reject_transaction(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    tx_id_str = callback.data.replace("admin_reject_tx_", "")
    if not tx_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    tx_id = int(tx_id_str)

    tx = (
        await db_session.execute(
            select(Transaction)
            .options(selectinload(Transaction.user))
            .where(Transaction.id == tx_id)
        )
    ).scalar_one_or_none()

    if not tx or tx.status != "pending":
        await callback.answer("❌ فیش قبلاً بررسی شده.", show_alert=True)
        return

    user = tx.user
    if not user:
        await callback.answer("❌ کاربر مرتبط با این فیش یافت نشد.", show_alert=True)
        return

    bot = callback.bot
    if not bot:
        await callback.answer("❌ خطای سیستمی: بات در دسترس نیست.", show_alert=True)
        return

    # ---------------------------------------------------------
    # 🌟 [سیستم بازگشت وجه - Refund]:
    # اگر کاربر برای خرید پلن، بخشی از هزینه را از کیف پول داده و سپس فیش را برای بقیه پول ارسال کرده بوده،
    # پولی که از کیف پولش بلاک (کسر) شده را محاسبه و دوباره به حسابش واریز (Refund) می‌کنیم!
    # ---------------------------------------------------------
    if tx.plan_id:
        # ۱. قیمت واقعی پلن بعد از تخفیف را در می‌آوریم
        actual_plan_price = int(tx.original_amount) - (
            int(tx.original_amount) * int(tx.discount_percent or 0) // 100
        )

        # ۲. مبلغی که باید به کارت واریز می‌شده دقیقاً tx.amount است (بدون کارمزد رندوم)
        payable_via_card = int(tx.amount)

        # ۳. پس پولی که از کیف پول کاربر بلاک شده بوده دقیقاً اختلاف این دو عدد است:
        wallet_used = actual_plan_price - payable_via_card

        # ۴. اگر پولی درگیر بوده، دقیقاً همان مقدار را به حسابش برمی‌گردانیم
        if wallet_used > 0:
            user.wallet_balance += wallet_used
    tx.status = "rejected"
    await db_session.commit()

    if isinstance(callback.message, types.Message):
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass

    if isinstance(callback.message, types.Message):
        await bot.send_message(
            chat_id=user.telegram_id,
            text=(
                f"❌ <b>پرداخت شما تایید نشد.</b>\n\n"
                f"فیش ارسالی برای مبلغ <code>{int(tx.amount):,}</code> تومان توسط پشتیبانی رد شد.\n"
                "در صورتی که مبلغی از کیف پول شما کسر شده بود، به حساب شما بازگردانده شد."
            ),
        )
    await callback.answer()


# ==========================================
# 🌟 اطلاعات پرداخت ادمین
# ==========================================
@router.callback_query(F.data == "admin_payment_info")
async def ask_vendor_card(
    callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession
):
    if not callback.from_user:
        return
    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        return

    current_card = vendor.card_number or "ثبت نشده"
    text = f"💳 <b>اطلاعات پرداخت شما</b>\n\nشماره کارت فعلی: <code>{current_card}</code>\n\nلطفاً شماره کارت جدید خود را ارسال کنید:"
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت", callback_data="back_to_admin_panel"
                )
            ]
        ]
    )
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=cancel_kb)
    await state.set_state(AdminStates.waiting_for_card_number)
    await callback.answer()


@router.message(AdminStates.waiting_for_card_number)
async def process_vendor_card(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return
    new_card = message.text.strip()
    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == message.from_user.id)
        )
    ).scalar_one_or_none()
    if vendor:
        vendor.card_number = new_card
        await db_session.commit()
        await message.answer(
            f"✅ شماره کارت با موفقیت به <code>{new_card}</code> تغییر یافت."
        )
    await state.clear()

    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if text:
        await message.answer(text, reply_markup=reply_markup)


# ==========================================
# 🌟 بقیه توابع مدیریتی بدون تغییر و ایمن
# ==========================================


@router.callback_query(F.data == "admin_view_servers")
async def admin_view_servers(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ دسترسی یافت نشد.", show_alert=True)
        return

    owner_id_str = os.getenv("OWNER_ID")
    is_owner = bool(owner_id_str and callback.from_user.id == int(owner_id_str))

    shared_subq = select(VendorServer.server_id).where(
        VendorServer.vendor_id == vendor.id
    )
    stmt = select(Server).where(
        or_(Server.vendor_id == vendor.id, Server.id.in_(shared_subq)),
        Server.is_deleted == False,
    )
    servers = (await db_session.execute(stmt)).scalars().all()

    kb: list[list[InlineKeyboardButton]] = []
    if is_owner:
        kb.append(
            [
                InlineKeyboardButton(
                    text="➕ افزودن سرور (اشتراک انتخابی)",
                    callback_data="add_srv_selective",
                )
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                text="➕ افزودن سرور اختصاصی", callback_data="add_srv_private"
            )
        ]
    )

    for srv in servers:
        icon = "🟢" if srv.is_active else "🔴"
        ownership = "مالک" if srv.vendor_id == vendor.id else "اشتراکی"
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {srv.name} ({ownership})",
                    callback_data=f"srv_det_{srv.id}",
                )
            ]
        )

    kb.append(
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
            )
        ]
    )
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "🖥 <b>مدیریت سرورها</b>\n\nسرورهای قابل دسترس در سیستم:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
    await callback.answer()


@router.callback_query(F.data.in_(["add_srv_selective", "add_srv_private"]))
async def start_add_server(callback: types.CallbackQuery, state: FSMContext):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    is_selective = callback.data == "add_srv_selective"
    await state.update_data(is_selective=is_selective, is_private=(not is_selective))

    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="admin_view_servers")]
        ]
    )
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "🖥 <b>افزودن سرور</b>\n\nنام سرور را وارد کنید (مثال: سرور آلمان ۱):",
            reply_markup=cancel_kb,
        )
    await state.set_state(AddServerStates.waiting_for_name)
    await callback.answer()


@router.message(AddServerStates.waiting_for_name)
async def srv_name(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(srv_name=message.text.strip())
    await message.answer(
        "🌐 <b>آدرس پنل</b> را وارد کنید (مثال: https://sub.domain.com:8000):"
    )
    await state.set_state(AddServerStates.waiting_for_url)


@router.message(AddServerStates.waiting_for_url)
async def srv_url(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(srv_url=message.text.strip())
    await message.answer("👤 <b>نام کاربری (Username)</b> ادمین پنل:")
    await state.set_state(AddServerStates.waiting_for_username)


@router.message(AddServerStates.waiting_for_username)
async def srv_user(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(srv_user=message.text.strip())
    await message.answer("🔑 <b>رمز عبور (Password)</b>:")
    await state.set_state(AddServerStates.waiting_for_password)


@router.message(AddServerStates.waiting_for_password)
async def srv_pass(message: types.Message, state: FSMContext, db_session: AsyncSession):
    if not message.text or not message.from_user:
        return

    data = await state.get_data()
    password = message.text.strip()

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == message.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await message.answer("❌ فروشنده یافت نشد.")
        await state.clear()
        return

    srv_url: str = str(data.get("srv_url", ""))
    srv_user: str = str(data.get("srv_user", ""))
    is_connected = await _test_marzban_connection(srv_url, srv_user, password)
    if not is_connected:
        await message.answer(
            "❌ <b>اتصال به پنل ناموفق بود.</b>\n"
            "لطفاً آدرس پنل، نام کاربری و رمز عبور را بررسی کنید و دوباره تلاش کنید.\n"
            "برای شروع مجدد از منوی مدیریت سرورها استفاده کنید."
        )
        await state.clear()
        text, reply_markup = await get_admin_panel_content(
            message.from_user.id, db_session
        )
        if text:
            await message.answer(text, reply_markup=reply_markup)
        return

    is_selective = bool(data.get("is_selective", False))

    if is_selective:
        stmt_vendors = (
            select(Vendor)
            .where(Vendor.is_active == True, Vendor.id != vendor.id)
            .order_by(Vendor.id.asc())
        )
        all_vendors = (await db_session.execute(stmt_vendors)).scalars().all()

        if not all_vendors:
            new_srv = Server(
                vendor_id=vendor.id,
                is_shared=False,
                name=data["srv_name"],
                panel_url=data["srv_url"],
                username=data["srv_user"],
                password=password,
                is_active=True,
            )
            db_session.add(new_srv)
            await db_session.commit()

            await message.answer(
                "✅ <b>سرور ثبت شد.</b>\n"
                "ℹ️ هیچ شریک فعالی برای اشتراک‌گذاری وجود نداشت، بنابراین سرور اختصاصی شما باقی ماند."
            )
            await state.clear()
            text, reply_markup = await get_admin_panel_content(
                message.from_user.id, db_session
            )
            if text:
                await message.answer(text, reply_markup=reply_markup)
            return

        await state.update_data(
            srv_password=password,
            owner_vendor_id=vendor.id,
            selected_vendor_ids=[],
        )

        kb = _build_vendor_selection_keyboard(all_vendors, selected_ids=[])
        if isinstance(message, types.Message):
            await message.answer(
                "👥 <b>مرحله اشتراک‌گذاری انتخابی</b>\n\n"
                "سرور با موفقیت به API متصل شد.\n"
                "اکنون انتخاب کنید کدام شرکا به این سرور دسترسی داشته باشند:\n"
                "✅ = انتخاب شده | ⬜ = انتخاب نشده",
                reply_markup=kb,
            )
        await state.set_state(AddServerStates.waiting_for_vendor_selection)
        return

    new_srv = Server(
        vendor_id=vendor.id,
        is_shared=False,
        name=data["srv_name"],
        panel_url=data["srv_url"],
        username=data["srv_user"],
        password=password,
        is_active=True,
    )
    db_session.add(new_srv)
    await db_session.commit()

    await message.answer(f"✅ <b>سرور {data['srv_name']} ثبت شد.</b>")
    await state.clear()

    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if text:
        await message.answer(text, reply_markup=reply_markup)


async def _test_marzban_connection(base_url: str, username: str, password: str) -> bool:
    if not base_url or not username or not password:
        return False
    client = MarzbanClient(base_url=base_url, username=username, password=password)
    is_connected = False
    try:
        is_connected = await client.login()
    except Exception as e:
        logger.error(f"Marzban connection test failed for {base_url}: {e}")
        is_connected = False
    finally:
        await client.close()
    return is_connected


def _build_vendor_selection_keyboard(
    vendors: Sequence[Vendor], selected_ids: list[int]
) -> InlineKeyboardMarkup:
    kb: list[list[InlineKeyboardButton]] = []
    for v in vendors:
        mark = "✅" if v.id in selected_ids else "⬜"
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {v.name}",
                    callback_data=f"srv_togv_{v.id}",
                )
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                text="💾 ثبت نهایی سرور", callback_data="srv_save_vendor_sharing"
            )
        ]
    )
    kb.append([InlineKeyboardButton(text="❌ لغو", callback_data="admin_view_servers")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(
    AddServerStates.waiting_for_vendor_selection, F.data.startswith("srv_togv_")
)
async def toggle_vendor_in_sharing(
    callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    vendor_id_str = callback.data.replace("srv_togv_", "")
    if not vendor_id_str.isdigit():
        await callback.answer("❌ داده نامعتبر.", show_alert=True)
        return
    toggled_vendor_id = int(vendor_id_str)

    data = await state.get_data()
    selected_ids: list[int] = list(data.get("selected_vendor_ids", []))

    if toggled_vendor_id in selected_ids:
        selected_ids.remove(toggled_vendor_id)
    else:
        selected_ids.append(toggled_vendor_id)

    await state.update_data(selected_vendor_ids=selected_ids)

    stmt_vendors = (
        select(Vendor)
        .where(Vendor.is_active == True, Vendor.id != data.get("owner_vendor_id"))
        .order_by(Vendor.id.asc())
    )
    all_vendors = (await db_session.execute(stmt_vendors)).scalars().all()

    kb = _build_vendor_selection_keyboard(all_vendors, selected_ids=selected_ids)
    if isinstance(callback.message, types.Message):
        await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


@router.callback_query(
    AddServerStates.waiting_for_vendor_selection, F.data == "srv_save_vendor_sharing"
)
async def save_server_with_vendor_sharing(
    callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession
):
    if not callback.from_user:
        await callback.answer()
        return

    data = await state.get_data()
    required_keys = (
        "srv_name",
        "srv_url",
        "srv_user",
        "srv_password",
        "owner_vendor_id",
    )
    if not all(k in data for k in required_keys):
        await callback.answer(
            "❌ اطلاعات سرور ناقص است. لطفاً دوباره تلاش کنید.", show_alert=True
        )
        await state.clear()
        text, reply_markup = await get_admin_panel_content(
            callback.from_user.id, db_session
        )
        if text and isinstance(callback.message, types.Message):
            await callback.message.edit_text(text, reply_markup=reply_markup)
        return

    owner_vendor_id: int = int(data["owner_vendor_id"])
    selected_vendor_ids: list[int] = list(data.get("selected_vendor_ids", []))

    new_srv = Server(
        vendor_id=owner_vendor_id,
        is_shared=False,
        name=data["srv_name"],
        panel_url=data["srv_url"],
        username=data["srv_user"],
        password=data["srv_password"],
        is_active=True,
    )
    db_session.add(new_srv)
    await db_session.flush()

    for vid in selected_vendor_ids:
        db_session.add(VendorServer(vendor_id=vid, server_id=new_srv.id))

    await db_session.commit()
    await state.clear()

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            f"✅ <b>سرور {new_srv.name} با موفقیت ثبت شد.</b>\n"
            f"👥 تعداد شرکای دسترسی‌یافته: <b>{len(selected_vendor_ids)}</b>"
        )

    text, reply_markup = await get_admin_panel_content(
        callback.from_user.id, db_session
    )
    if text and isinstance(callback.message, types.Message):
        await callback.message.answer(text, reply_markup=reply_markup)
    await callback.answer()


@router.callback_query(F.data.startswith("srv_det_"))
async def admin_server_details(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    srv_id_str = callback.data.replace("srv_det_", "")
    if not srv_id_str.isdigit():
        await callback.answer("❌ شناسه سرور نامعتبر است.", show_alert=True)
        return

    stmt = (
        select(Server)
        .options(
            selectinload(Server.vendor),
            selectinload(Server.vendor_servers).selectinload(VendorServer.vendor),
        )
        .where(Server.id == int(srv_id_str))
    )
    server = (await db_session.execute(stmt)).scalar_one_or_none()
    if not server:
        await callback.answer("❌ سرور یافت نشد.", show_alert=True)
        return

    owner_id_str = os.getenv("OWNER_ID")
    is_owner = bool(owner_id_str and callback.from_user.id == int(owner_id_str))

    status_text = "🟢 در حال کار" if server.is_active else "🔴 غیرفعال"
    type_text = "اشتراکی (عمومی)" if server.is_shared else "اختصاصی"

    shared_vendors: list[Vendor] = [
        vs.vendor for vs in server.vendor_servers if vs.vendor is not None
    ]
    if shared_vendors:
        shared_names = "\n".join(f"   • {v.name}" for v in shared_vendors)
    else:
        shared_names = "   —"

    text = (
        f"🖥 <b>سرور: {server.name}</b> ({type_text})\n\n"
        f"🔗 <b>آدرس:</b> <code>{server.panel_url}</code>\n"
        f"👁🗨 <b>وضعیت:</b> {status_text}\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👥 <b>شرکای دارای دسترسی:</b>\n{shared_names}\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"📊 در اینجا اطلاعات منابع از طریق API لود خواهد شد...\n"
    )

    server_owner_tg_id = server.vendor.telegram_id if server.vendor else None
    can_manage = bool(
        is_owner
        or (
            server_owner_tg_id is not None
            and server_owner_tg_id == callback.from_user.id
        )
    )

    kb: list[list[InlineKeyboardButton]] = []
    if can_manage:
        kb.append(
            [
                InlineKeyboardButton(
                    text="تغییر وضعیت 🟢/🔴", callback_data=f"srv_tog_{server.id}"
                )
            ]
        )
        kb.append(
            [
                InlineKeyboardButton(
                    text="🗑 حذف سرور", callback_data=f"srv_del_{server.id}"
                )
            ]
        )

    kb.append(
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به لیست سرورها", callback_data="admin_view_servers"
            )
        ]
    )

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("srv_tog_"))
async def toggle_server_status(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data:
        await callback.answer()
        return

    srv_id_str = callback.data.replace("srv_tog_", "")
    if not srv_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return

    srv_id = int(srv_id_str)
    server = (
        await db_session.execute(select(Server).where(Server.id == srv_id))
    ).scalar_one_or_none()
    if not server:
        await callback.answer("❌ سرور یافت نشد.", show_alert=True)
        return

    server.is_active = not server.is_active

    if not server.is_active:
        await db_session.execute(
            update(Plan).where(Plan.server_id == server.id).values(is_active=False)
        )

    await db_session.commit()

    new_callback = callback.model_copy(update={"data": f"srv_det_{server.id}"})
    await admin_server_details(new_callback, db_session)


@router.callback_query(F.data.startswith("srv_del_"))
async def delete_server(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data:
        await callback.answer()
        return

    srv_id_str = callback.data.replace("srv_del_", "")
    if not srv_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return

    srv_id = int(srv_id_str)
    server = (
        await db_session.execute(select(Server).where(Server.id == srv_id))
    ).scalar_one_or_none()
    if not server:
        await callback.answer("❌ سرور یافت نشد.", show_alert=True)
        return

    server.is_deleted = True
    server.is_active = False
    await db_session.execute(
        update(Plan)
        .where(Plan.server_id == server.id)
        .values(is_deleted=True, is_active=False)
    )
    await db_session.commit()

    await callback.answer("✅ سرور حذف شد.", show_alert=True)
    await admin_view_servers(callback, db_session)


# ==========================================
# 🌟 مدیریت پلن‌ها (بایند شده به سرور)
# ==========================================
@router.callback_query(F.data == "admin_manage_plans")
async def admin_manage_plans_menu(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ افزودن پلن جدید", callback_data="admin_add_plan_sel_srv"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 مشاهده پلن‌ها (بر اساس سرور)",
                    callback_data="adm_view_srv_plans",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
                )
            ],
        ]
    )
    text = "🛍 <b>مدیریت پلن‌ها</b>\n\nعملیات مورد نظر خود را انتخاب کنید:"
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "admin_add_plan_sel_srv")
async def add_plan_select_server(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.from_user:
        await callback.answer()
        return

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    shared_subq = select(VendorServer.server_id).where(
        VendorServer.vendor_id == vendor.id
    )
    stmt = select(Server).where(
        Server.is_active == True,
        Server.is_deleted == False,
        or_(
            Server.vendor_id == vendor.id,
            Server.id.in_(shared_subq),
        ),
    )
    servers = (await db_session.execute(stmt)).scalars().all()

    if not servers:
        await callback.answer(
            "❌ هیچ سرور فعالی برای اتصال پلن وجود ندارد! ابتدا یک سرور اضافه کنید.",
            show_alert=True,
        )
        return

    kb: list[list[InlineKeyboardButton]] = []
    for srv in servers:
        ownership = "مالک" if srv.vendor_id == vendor.id else "اشتراکی"
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"🖥 {srv.name} ({ownership})", callback_data=f"addp_s_{srv.id}"
                )
            ]
        )

    kb.append([InlineKeyboardButton(text="❌ لغو", callback_data="admin_manage_plans")])

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "🔗 <b>مرحله ۱: انتخاب سرور</b>\n\nاین پلن قرار است روی کدام سرور ساخته شود؟",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("addp_s_"))
async def plan_title_start(callback: types.CallbackQuery, state: FSMContext):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    srv_id_str = callback.data.replace("addp_s_", "")
    if not srv_id_str.isdigit():
        await callback.answer("❌ شناسه سرور نامعتبر است.", show_alert=True)
        return

    srv_id = int(srv_id_str)
    await state.update_data(server_id=srv_id)

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "🏷 <b>مرحله ۲: نام پلن</b>\n\nنام پلن را وارد کنید (مثلاً: <code>۱ ماهه ۵۰ گیگ</code>):"
        )
    await state.set_state(AddPlanStates.waiting_for_title)
    await callback.answer()


@router.message(AddPlanStates.waiting_for_title)
async def plan_title(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(title=message.text.strip())
    await message.answer(
        "💽 <b>مرحله ۳: حجم (گیگابایت)</b>\n\nبرای نامحدود عدد <code>0</code> را بفرستید:"
    )
    await state.set_state(AddPlanStates.waiting_for_volume)


@router.message(AddPlanStates.waiting_for_volume)
async def plan_volume(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ عدد وارد کنید!")
        return
    await state.update_data(volume=float(message.text))
    await message.answer(
        "⏳ <b>مرحله ۴: اعتبار (روز)</b>\n\nبرای نامحدود عدد <code>0</code> را بفرستید:"
    )
    await state.set_state(AddPlanStates.waiting_for_days)


@router.message(AddPlanStates.waiting_for_days)
async def plan_days(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ عدد وارد کنید!")
        return
    await state.update_data(days=int(message.text))
    await message.answer(
        "👥 <b>مرحله ۵: محدودیت کاربر</b>\n\nبرای نامحدود عدد <code>0</code> را بفرستید:"
    )
    await state.set_state(AddPlanStates.waiting_for_user_limit)


@router.message(AddPlanStates.waiting_for_user_limit)
async def plan_user_limit(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ عدد وارد کنید!")
        return
    await state.update_data(user_limit=int(message.text))
    await message.answer("💵 <b>مرحله ۶: قیمت (تومان)</b>:")
    await state.set_state(AddPlanStates.waiting_for_price)


@router.message(AddPlanStates.waiting_for_price)
async def plan_price(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ عدد وارد کنید!")
        return
    await state.update_data(price=float(message.text))
    await message.answer(
        "📝 <b>مرحله ۷: توضیحات</b>\n\nبرای رد شدن <code>ندارد</code> را بفرستید:"
    )
    await state.set_state(AddPlanStates.waiting_for_description)


@router.message(AddPlanStates.waiting_for_description)
async def plan_description(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return
    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == message.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        return

    data = await state.get_data()
    desc = message.text.strip() if message.text.strip() != "ندارد" else None

    server_id_raw = data.get("server_id")
    title_val = data.get("title")
    volume_val = data.get("volume")
    days_val = data.get("days")
    user_limit_val = data.get("user_limit")
    price_val = data.get("price")

    if (
        server_id_raw is None
        or title_val is None
        or volume_val is None
        or days_val is None
        or user_limit_val is None
        or price_val is None
    ):
        await message.answer("❌ خطایی رخ داد. اطلاعات ناقص است.")
        await state.clear()
        return

    new_plan = Plan(
        vendor_id=vendor.id,
        server_id=int(server_id_raw),
        title=str(title_val),
        volume_gb=float(volume_val),
        days=int(days_val),
        user_limit=int(user_limit_val),
        price=float(price_val),
        description=desc,
    )
    db_session.add(new_plan)
    await db_session.commit()

    await message.answer(f"✅ <b>پلن شما ساخته شد!</b>\nنام: {title_val}")
    await state.clear()

    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if text:
        await message.answer(text, reply_markup=reply_markup)


async def _check_plan_access(
    db_session: AsyncSession,
    plan_id: int,
    admin_vendor_id: int,
) -> tuple[Optional[Plan], bool]:
    plan = (
        await db_session.execute(
            select(Plan).options(selectinload(Plan.server)).where(Plan.id == plan_id)
        )
    ).scalar_one_or_none()
    if not plan:
        return None, False

    shared_subq = select(VendorServer.server_id).where(
        VendorServer.vendor_id == admin_vendor_id
    )
    accessible_server = (
        await db_session.execute(
            select(Server.id).where(
                Server.id == plan.server_id,
                or_(
                    Server.vendor_id == admin_vendor_id,
                    Server.id.in_(shared_subq),
                ),
            )
        )
    ).scalar_one_or_none()

    return plan, accessible_server is not None


@router.callback_query(F.data == "adm_view_srv_plans")
async def admin_view_categories_by_server(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.from_user:
        return
    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        return

    shared_subq = select(VendorServer.server_id).where(
        VendorServer.vendor_id == vendor.id
    )
    stmt = (
        select(Server)
        .join(Plan, Plan.server_id == Server.id)
        .where(
            Plan.is_deleted == False,
            Server.is_deleted == False,
            or_(
                Server.vendor_id == vendor.id,
                Server.id.in_(shared_subq),
            ),
        )
        .distinct()
    )
    servers_with_plans = (await db_session.execute(stmt)).scalars().all()

    if not servers_with_plans:
        await callback.answer("❌ هیچ پلنی ثبت نکرده‌اید.", show_alert=True)
        return

    kb = []
    for srv in servers_with_plans:
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"🖥 {srv.name}", callback_data=f"adm_cat_{srv.id}"
                )
            ]
        )

    kb.append(
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_manage_plans")]
    )
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "📋 <b>سرورها</b>\n\nبرای دیدن پلن‌ها، یک سرور را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_cat_"))
async def admin_view_plans_in_server(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        return
    srv_id_str = callback.data.replace("adm_cat_", "")
    if not srv_id_str.isdigit():
        return
    srv_id = int(srv_id_str)

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        return

    shared_subq = select(VendorServer.server_id).where(
        VendorServer.vendor_id == vendor.id
    )
    stmt = (
        select(Plan)
        .join(Server, Server.id == Plan.server_id)
        .where(
            Plan.server_id == srv_id,
            Plan.is_deleted == False,
            or_(
                Server.vendor_id == vendor.id,
                Server.id.in_(shared_subq),
            ),
        )
    )
    plans = (await db_session.execute(stmt)).scalars().all()
    server = (
        await db_session.execute(select(Server).where(Server.id == srv_id))
    ).scalar_one_or_none()

    kb = []
    for p in plans:
        status_icon = "🟢" if p.is_active else "🔴"
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"{status_icon} {p.title}", callback_data=f"adm_p_{p.id}"
                )
            ]
        )

    kb.append(
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="adm_view_srv_plans")]
    )
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            f"📁 <b>سرویس‌های متصل به سرور: {server.name if server else 'نامشخص'}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_p_"))
async def admin_show_plan_details(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        return
    plan_id_str = callback.data.replace("adm_p_", "")
    if not plan_id_str.isdigit():
        return
    plan_id = int(plan_id_str)

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    plan, has_access = await _check_plan_access(db_session, plan_id, vendor.id)
    if not plan:
        await callback.answer("❌ پلن یافت نشد.", show_alert=True)
        return
    if not has_access:
        await callback.answer("❌ دسترسی غیرمجاز", show_alert=True)
        return

    status = "🟢 فعال" if plan.is_active else "🔴 غیرفعال"
    vol = "نامحدود" if plan.volume_gb == 0 else f"{plan.volume_gb}GB"
    days = "نامحدود" if plan.days == 0 else f"{plan.days} روز"
    ulimit = "نامحدود" if plan.user_limit == 0 else f"{plan.user_limit} کاربر"

    text = (
        f"⚙️ <b>مدیریت پلن</b>\n\n"
        f"🔹 <b>نام:</b> {plan.title}\n"
        f"🖥 <b>سرور متصل:</b> {plan.server.name}\n"
        f"💽 <b>حجم:</b> {vol} | ⏳ <b>اعتبار:</b> {days}\n"
        f"👥 <b>محدودیت:</b> {ulimit}\n"
        f"💵 <b>قیمت:</b> <code>{int(plan.price):,}</code> تومان\n"
        f"👁‍🗨 <b>وضعیت:</b> {status}\n"
    )

    kb = [
        [
            InlineKeyboardButton(
                text="تغییر وضعیت 🟢/🔴", callback_data=f"adm_tog_{plan.id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="✏️ ویرایش قیمت", callback_data=f"adm_edp_{plan.id}"
            ),
            InlineKeyboardButton(text="🗑 حذف پلن", callback_data=f"adm_del_{plan.id}"),
        ],
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به لیست", callback_data=f"adm_cat_{plan.server_id}"
            )
        ],
    ]

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_tog_"))
async def admin_toggle_plan(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    plan_id_str = callback.data.replace("adm_tog_", "")
    if not plan_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return

    plan_id = int(plan_id_str)

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    plan, has_access = await _check_plan_access(db_session, plan_id, vendor.id)
    if not plan:
        await callback.answer("❌ پلن یافت نشد.", show_alert=True)
        return
    if not has_access:
        await callback.answer("❌ دسترسی غیرمجاز", show_alert=True)
        return

    plan.is_active = not plan.is_active
    await db_session.commit()

    new_callback = callback.model_copy(update={"data": f"adm_p_{plan.id}"})
    await admin_show_plan_details(new_callback, db_session)


@router.callback_query(F.data.startswith("adm_del_"))
async def admin_delete_plan(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    plan_id_str = callback.data.replace("adm_del_", "")
    if not plan_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return

    plan_id = int(plan_id_str)

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    plan, has_access = await _check_plan_access(db_session, plan_id, vendor.id)
    if not plan:
        await callback.answer("❌ پلن یافت نشد.", show_alert=True)
        return
    if not has_access:
        await callback.answer("❌ دسترسی غیرمجاز", show_alert=True)
        return

    srv_id = plan.server_id
    plan.is_deleted = True
    plan.is_active = False
    await db_session.commit()

    await callback.answer("✅ حذف شد.", show_alert=True)

    new_callback = callback.model_copy(update={"data": f"adm_cat_{srv_id}"})
    await admin_view_plans_in_server(new_callback, db_session)


@router.callback_query(F.data.startswith("adm_edp_"))
async def admin_edit_price(
    callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        return
    plan_id = int(callback.data.replace("adm_edp_", ""))

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    plan, has_access = await _check_plan_access(db_session, plan_id, vendor.id)
    if not plan:
        await callback.answer("❌ پلن یافت نشد.", show_alert=True)
        return
    if not has_access:
        await callback.answer("❌ دسترسی غیرمجاز", show_alert=True)
        return

    await state.update_data(edit_plan_id=plan_id)
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"adm_p_{plan_id}")]
        ]
    )
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "💵 مبلغ جدید (تومان):", reply_markup=cancel_kb
        )
    await state.set_state(EditPlanStates.waiting_for_new_price)
    await callback.answer()


@router.message(EditPlanStates.waiting_for_new_price)
async def admin_process_new_price(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user or not message.text.isdigit():
        await message.answer("❌ فقط عدد وارد کنید.")
        return
    data = await state.get_data()
    plan_id_raw = data.get("edit_plan_id")

    if plan_id_raw is not None:
        plan_id = int(plan_id_raw)
        vendor = (
            await db_session.execute(
                select(Vendor).where(Vendor.telegram_id == message.from_user.id)
            )
        ).scalar_one_or_none()
        if vendor:
            plan, has_access = await _check_plan_access(db_session, plan_id, vendor.id)
            if plan and has_access:
                plan.price = float(message.text)
                await db_session.commit()
                await message.answer("✅ قیمت بروزرسانی شد.")
            else:
                await message.answer("❌ دسترسی غیرمجاز")
    await state.clear()

    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if text:
        await message.answer(text, reply_markup=reply_markup)


# ==========================================
# 🌟 داشبورد تیکت‌های پشتیبانی
# ==========================================
@router.callback_query(F.data == "admin_support_tickets")
async def admin_support_tickets_list(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.from_user:
        await callback.answer()
        return

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    stmt = (
        select(Ticket)
        .options(selectinload(Ticket.user))
        .where(Ticket.vendor_id == vendor.id, Ticket.status == "pending")
        .order_by(Ticket.created_at.desc())
    )
    tickets = (await db_session.execute(stmt)).scalars().all()

    if not tickets:
        empty_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
                    )
                ]
            ]
        )
        if isinstance(callback.message, types.Message):
            await callback.message.edit_text(
                "📨 <b>پیامهای پشتیبانی</b>\n\nهیچ پیام جدیدی وجود ندارد.",
                reply_markup=empty_kb,
            )
        await callback.answer()
        return

    kb: list[list[InlineKeyboardButton]] = []
    for t in tickets:
        user_display = t.user.telegram_id if t.user else "نامشخص"
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"📨 تیکت از کاربر {user_display}",
                    callback_data=f"adm_tk_{t.id}",
                )
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
            )
        ]
    )

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "📨 <b>پیامهای پشتیبانی</b>\n\nلیست پیامهای در انتظار پاسخ:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_tk_"))
async def admin_show_ticket(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    ticket_id_str = callback.data.replace("adm_tk_", "")
    if not ticket_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    ticket_id = int(ticket_id_str)

    stmt = (
        select(Ticket).options(selectinload(Ticket.user)).where(Ticket.id == ticket_id)
    )
    ticket = (await db_session.execute(stmt)).scalar_one_or_none()
    if not ticket:
        await callback.answer("❌ تیکت یافت نشد.", show_alert=True)
        return

    user_display = ticket.user.telegram_id if ticket.user else "نامشخص"
    status_emoji = (
        "🟡"
        if ticket.status == "pending"
        else ("🟢" if ticket.status == "answered" else "🔴")
    )

    text = (
        f"📨 <b>تیکت پشتیبانی #{ticket.id}</b>\n\n"
        f"👤 <b>کاربر:</b> <code>{user_display}</code>\n"
        f"{status_emoji} <b>وضعیت:</b> {ticket.status}\n"
        f"🕒 <b>زمان:</b> {ticket.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"📝 <b>متن پیام:</b>\n{ticket.message_text}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✍️ پاسخ (Reply)", callback_data=f"adm_tkr_{ticket.id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ رد/بستن (Close)", callback_data=f"adm_tkc_{ticket.id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت به لیست", callback_data="admin_support_tickets"
                )
            ],
        ]
    )

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_tkr_"))
async def admin_reply_ticket_start(
    callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    ticket_id_str = callback.data.replace("adm_tkr_", "")
    if not ticket_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    ticket_id = int(ticket_id_str)

    ticket = (
        await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if not ticket:
        await callback.answer("❌ تیکت یافت نشد.", show_alert=True)
        return

    await state.update_data(reply_ticket_id=ticket_id)

    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"adm_tk_{ticket_id}")]
        ]
    )
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "✍️ <b>پاسخ به تیکت</b>\n\nلطفاً متن پاسخ خود را ارسال کنید:",
            reply_markup=cancel_kb,
        )
    await state.set_state(AdminTicketStates.waiting_for_ticket_reply)
    await callback.answer()


@router.message(AdminTicketStates.waiting_for_ticket_reply)
async def admin_reply_ticket_send(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return

    data = await state.get_data()
    ticket_id_raw = data.get("reply_ticket_id")
    if not ticket_id_raw:
        await message.answer("❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.")
        await state.clear()
        return

    stmt = (
        select(Ticket)
        .options(selectinload(Ticket.user))
        .where(Ticket.id == int(ticket_id_raw))
    )
    ticket = (await db_session.execute(stmt)).scalar_one_or_none()
    if not ticket or not ticket.user:
        await message.answer("❌ تیکت یا کاربر یافت نشد.")
        await state.clear()
        return

    reply_text = message.text.strip()
    if not reply_text:
        await message.answer("❌ متن پاسخ نمیتواند خالی باشد.")
        return

    ticket.status = "answered"
    await db_session.commit()

    msg_to_user = (
        f"💬 <b>پاسخ پشتیبانی به تیکت #{ticket.id}</b>\n\n"
        f"📝 <b>پیام اصلی شما:</b>\n{ticket.message_text}\n\n"
        f"✍️ <b>پاسخ پشتیبانی:</b>\n{reply_text}"
    )
    try:
        if message.bot:
            await message.bot.send_message(
                chat_id=ticket.user.telegram_id, text=msg_to_user
            )
        else:
            logger.error(
                "Bot instance is not available on message; cannot send ticket reply."
            )
            await message.answer("⚠️ پاسخ ثبت شد اما ارسال پیام به کاربر ناموفق بود.")
    except Exception as e:
        logger.error(
            f"Failed to send ticket reply to user {ticket.user.telegram_id}: {e}"
        )
        await message.answer("⚠️ پاسخ ثبت شد اما ارسال پیام به کاربر با خطا مواجه شد.")

    await state.clear()
    await message.answer("✅ پاسخ شما برای کاربر ارسال شد.")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📨 بازگشت به لیست تیکت‌ها",
                    callback_data="admin_support_tickets",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
                )
            ],
        ]
    )
    await message.answer(
        "📨 <b>پیامهای پشتیبانی</b>\n\nدر حال بازگشت به لیست...", reply_markup=kb
    )


@router.callback_query(F.data.startswith("adm_tkc_"))
async def admin_close_ticket(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    ticket_id_str = callback.data.replace("adm_tkc_", "")
    if not ticket_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    ticket_id = int(ticket_id_str)

    ticket = (
        await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if not ticket:
        await callback.answer("❌ تیکت یافت نشد.", show_alert=True)
        return

    ticket.status = "closed"
    await db_session.commit()

    await callback.answer("✅ تیکت بسته شد.", show_alert=True)

    new_callback = callback.model_copy(update={"data": "admin_support_tickets"})
    await admin_support_tickets_list(new_callback, db_session)


# ==========================================
# 🌟 تنظیمات عضویت اجباری (Force-Join)
# ==========================================
@router.callback_query(F.data == "admin_force_join")
async def admin_force_join_menu(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.from_user:
        await callback.answer()
        return

    stmt = select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
    vendor = (await db_session.execute(stmt)).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    channels = (
        (
            await db_session.execute(
                select(ForceJoinChannel)
                .where(ForceJoinChannel.vendor_id == vendor.id)
                .order_by(ForceJoinChannel.id)
            )
        )
        .scalars()
        .all()
    )

    kb_rows: list[list[InlineKeyboardButton]] = []
    for ch in channels:
        kb_rows.append(
            [
                InlineKeyboardButton(text=f"📢 {ch.title}", url=ch.url),
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"fjc_del_{ch.id}"),
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="➕ افزودن کانال", callback_data="fjc_add")]
    )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
            )
        ]
    )

    if channels:
        text = (
            "🔒 <b>تنظیمات عضویت اجباری</b>\n\n"
            "کانالهای فعال زیر را کاربران باید عضو شوند تا تست رایگان دریافت کنند:"
        )
    else:
        text = "🔒 <b>تنظیمات عضویت اجباری</b>\n\nهیچ کانالی ثبت نشده است."

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.callback_query(F.data == "fjc_add")
async def admin_force_join_add(callback: types.CallbackQuery, state: FSMContext):
    if not callback.from_user:
        await callback.answer()
        return
    await state.set_state(AdminForceJoinStates.waiting_for_channel_data)
    text = (
        "➕ <b>افزودن کانال جدید</b>\n\n"
        "لطفاً اطلاعات کانال را با این فرمت ارسال کنید:\n\n"
        "<code>chat_id - https://t.me/... - عنوان کانال</code>\n\n"
        "💡 <b>chat_id</b> میتواند یکی از دو فرمت زیر باشد:\n"
        "• کانال عمومی: <code>@mychannel</code>\n"
        "• کانال خصوصی: <code>-1001234567890</code>\n\n"
        "⚠️ توجه: ربات باید مدیر (Admin) در آن کانال باشد تا بتواند عضویت کاربران را بررسی کند.\n"
        "برای دریافت chat_id کانال خصوصی، میتوانید از رباتهای @username_to_id_api_bot استفاده کنید."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="admin_force_join")]
        ]
    )
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.message(AdminForceJoinStates.waiting_for_channel_data)
async def process_force_join_channel_data(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return

    parts = message.text.split(" - ")
    if len(parts) != 3:
        await message.answer(
            "❌ فرمت نامعتبر. مثال صحیح:\n<code>-1001234567890 - https://t.me/... - عنوان</code>\n"
            "یا\n<code>@channel - https://t.me/... - عنوان</code>"
        )
        return

    chat_id = parts[0].strip()
    url = parts[1].strip()
    title = parts[2].strip()

    is_public_handle = chat_id.startswith("@") and len(chat_id) > 1
    is_private_id = (
        chat_id.startswith("-100")
        and chat_id.lstrip("-").isdigit()
        and len(chat_id) > 4
    )
    if not is_public_handle and not is_private_id:
        await message.answer(
            "❌ آیدی کانال نامعتبر است. باید با @ (کانال عمومی) یا -100 (کانال خصوصی) شروع شود.\n"
            "مثال: <code>-1001234567890</code> یا <code>@mychannel</code>"
        )
        return

    if not url.startswith("http"):
        await message.answer(
            "❌ لینک کانال نامعتبر است. باید با http شروع شود.\nمثال: <code>https://t.me/mychannel</code>"
        )
        return

    if not title:
        await message.answer("❌ عنوان کانال نمیتواند خالی باشد.")
        return

    bot = message.bot
    if bot is None:
        await message.answer("❌ خطای داخلی رخ داد. لطفاً مجدداً تلاش کنید.")
        return

    try:
        await bot.get_chat(chat_id=chat_id)
    except TelegramBadRequest:
        await message.answer(
            "❌ ربات نتوانست به کانال متصل شود. بررسی کنید آیدی صحیح است (مثل -100...) و ربات در کانال ادمین است."
        )
        return
    except Exception:
        await message.answer(
            "❌ ربات نتوانست به کانال متصل شود. بررسی کنید آیدی صحیح است (مثل -100...) و ربات در کانال ادمین است."
        )
        return

    stmt = select(Vendor).where(Vendor.telegram_id == message.from_user.id)
    vendor = (await db_session.execute(stmt)).scalar_one_or_none()
    if not vendor:
        await message.answer("❌ فروشنده یافت نشد.")
        await state.clear()
        return

    channel = ForceJoinChannel(
        vendor_id=vendor.id, chat_id=chat_id, url=url, title=title
    )
    db_session.add(channel)
    await db_session.commit()

    await message.answer(f"✅ کانال «{title}» با موفقیت اضافه شد.")
    await state.clear()

    channels = (
        (
            await db_session.execute(
                select(ForceJoinChannel)
                .where(ForceJoinChannel.vendor_id == vendor.id)
                .order_by(ForceJoinChannel.id)
            )
        )
        .scalars()
        .all()
    )

    kb_rows: list[list[InlineKeyboardButton]] = []
    for ch in channels:
        kb_rows.append(
            [
                InlineKeyboardButton(text=f"📢 {ch.title}", url=ch.url),
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"fjc_del_{ch.id}"),
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="➕ افزودن کانال", callback_data="fjc_add")]
    )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
            )
        ]
    )

    if channels:
        menu_text = (
            "🔒 <b>تنظیمات عضویت اجباری</b>\n\n"
            "کانالهای فعال زیر را کاربران باید عضو شوند تا تست رایگان دریافت کنند:"
        )
    else:
        menu_text = "🔒 <b>تنظیمات عضویت اجباری</b>\n\nهیچ کانالی ثبت نشده است."

    await message.answer(
        menu_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )


@router.callback_query(F.data.startswith("fjc_del_"))
async def admin_force_join_delete(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    channel_id_str = callback.data.replace("fjc_del_", "")
    if not channel_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    channel_id = int(channel_id_str)

    channel = (
        await db_session.execute(
            select(ForceJoinChannel).where(ForceJoinChannel.id == channel_id)
        )
    ).scalar_one_or_none()
    if not channel:
        await callback.answer("❌ کانال یافت نشد.", show_alert=True)
        return

    await db_session.delete(channel)
    await db_session.commit()

    await callback.answer("✅ حذف شد.", show_alert=True)

    new_callback = callback.model_copy(update={"data": "admin_force_join"})
    await admin_force_join_menu(new_callback, db_session)


# ==========================================
# 🌟 مدیریت مشتریان (Customer Management)
# ==========================================
@router.callback_query(F.data == "admin_my_customers")
async def admin_my_customers(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    stmt = select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
    vendor = (await db_session.execute(stmt)).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 لیست مشتریان", callback_data="adm_cust_list_1"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 پیام همگانی", callback_data="adm_cust_broadcast"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔍 جستجوی مشتری", callback_data="adm_cust_search"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
                )
            ],
        ]
    )
    text = "👥 <b>مدیریت مشتریان</b>\n\nلطفاً یک گزینه را انتخاب کنید:"
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_cust_list_"))
async def admin_customers_list(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    page_str = callback.data.replace("adm_cust_list_", "")
    page = int(page_str) if page_str.isdigit() else 1
    if page < 1:
        page = 1

    stmt = select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
    vendor = (await db_session.execute(stmt)).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    users = (
        (
            await db_session.execute(
                select(User)
                .where(User.vendor_id == vendor.id)
                .order_by(User.created_at.desc())
                .limit(10)
                .offset((page - 1) * 10)
            )
        )
        .scalars()
        .all()
    )

    total = await db_session.scalar(
        select(func.count(User.id)).where(User.vendor_id == vendor.id)
    )
    total = int(total or 0)

    kb_rows: list[list[InlineKeyboardButton]] = []
    if not users:
        text = "👥 <b>لیست مشتریان</b>\n\nهیچ مشتریای یافت نشد."
        kb_rows.append(
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_my_customers")]
        )
    else:
        text = f"👥 <b>لیست مشتریان</b>\n\nصفحه {page} — برای مشاهده جزئیات روی یک کاربر کلیک کنید:"
        for u in users:
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"👤 {u.telegram_id}", callback_data=f"adm_cust_det_{u.id}"
                    )
                ]
            )

        nav_row: list[InlineKeyboardButton] = []
        if page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text="⬅️ قبلی", callback_data=f"adm_cust_list_{page - 1}"
                )
            )
        if total > page * 10:
            nav_row.append(
                InlineKeyboardButton(
                    text="➡️ بعدی", callback_data=f"adm_cust_list_{page + 1}"
                )
            )
        if nav_row:
            kb_rows.append(nav_row)

        kb_rows.append(
            [InlineKeyboardButton(text="🔍 جستجو", callback_data="adm_cust_search")]
        )
        kb_rows.append(
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_my_customers")]
        )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.callback_query(F.data == "adm_cust_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if not callback.from_user:
        await callback.answer()
        return
    await state.set_state(AdminCustomerStates.waiting_for_broadcast_message)
    text = "📢 <b>پیام همگانی</b>\n\nلطفاً متن پیام خود را ارسال کنید تا برای تمام مشتریان شما ارسال شود:"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="admin_my_customers")]
        ]
    )
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.message(AdminCustomerStates.waiting_for_broadcast_message)
async def process_broadcast_message(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return

    stmt = select(Vendor).where(Vendor.telegram_id == message.from_user.id)
    vendor = (await db_session.execute(stmt)).scalar_one_or_none()
    if not vendor:
        await message.answer("❌ فروشنده یافت نشد.")
        await state.clear()
        return

    users = (
        (await db_session.execute(select(User).where(User.vendor_id == vendor.id)))
        .scalars()
        .all()
    )

    await message.answer("⏳ در حال ارسال...")

    sent = 0
    failed = 0
    bot = message.bot
    if bot is None:
        await state.clear()
        await message.answer("❌ ربات در دسترس نیست.")
        return
    for u in users:
        try:
            await bot.send_message(chat_id=u.telegram_id, text=message.text)
            sent += 1
        except TelegramBadRequest:
            failed += 1
        await asyncio.sleep(0.05)

    await state.clear()
    await message.answer(
        f"✅ ارسال همگانی کامل شد.\n\n📤 موفق: {sent}\n❌ ناموفق: {failed}"
    )

    menu_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 لیست مشتریان", callback_data="adm_cust_list_1"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 پیام همگانی", callback_data="adm_cust_broadcast"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔍 جستجوی مشتری", callback_data="adm_cust_search"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
                )
            ],
        ]
    )
    await message.answer(
        "👥 <b>مدیریت مشتریان</b>\n\nلطفاً یک گزینه را انتخاب کنید:",
        reply_markup=menu_kb,
    )


@router.callback_query(F.data == "adm_cust_search")
async def admin_search_start(callback: types.CallbackQuery, state: FSMContext):
    if not callback.from_user:
        await callback.answer()
        return
    await state.set_state(AdminCustomerStates.waiting_for_customer_search)
    text = (
        "🔍 <b>جستجوی مشتری</b>\n\nلطفاً شناسه تلگرام (عددی) یا نام کاربر را ارسال کنید:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="admin_my_customers")]
        ]
    )
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.message(AdminCustomerStates.waiting_for_customer_search)
async def process_customer_search(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return

    stmt = select(Vendor).where(Vendor.telegram_id == message.from_user.id)
    vendor = (await db_session.execute(stmt)).scalar_one_or_none()
    if not vendor:
        await message.answer("❌ فروشنده یافت نشد.")
        await state.clear()
        return

    query_text = message.text.strip()
    if not query_text.isdigit():
        await message.answer("❌ برای جستجو شناسه تلگرام عددی ارسال کنید.")
        return

    telegram_id = int(query_text)
    users = (
        (
            await db_session.execute(
                select(User)
                .where(User.vendor_id == vendor.id, User.telegram_id == telegram_id)
                .limit(10)
            )
        )
        .scalars()
        .all()
    )

    await state.clear()

    kb_rows: list[list[InlineKeyboardButton]] = []
    if not users:
        text = "🔍 <b>نتایج جستجو</b>\n\nهیچ کاربری با این شناسه یافت نشد."
    else:
        text = "🔍 <b>نتایج جستجو</b>\n\nبرای مشاهده جزئیات روی کاربر کلیک کنید:"
        for u in users:
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"👤 {u.telegram_id}", callback_data=f"adm_cust_det_{u.id}"
                    )
                ]
            )
    kb_rows.append(
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_my_customers")]
    )

    await message.answer(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )


@router.callback_query(F.data.startswith("adm_cust_det_"))
async def admin_customer_detail(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    user_id_str = callback.data.replace("adm_cust_det_", "")
    if not user_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    user_id = int(user_id_str)

    admin_vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not admin_vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    if user.vendor_id != admin_vendor.id:
        await callback.answer("❌ دسترسی غیرمجاز", show_alert=True)
        return

    text = (
        "👤 <b>جزئیات مشتری</b>\n\n"
        f"🆔 شناسه تلگرام: <code>{user.telegram_id}</code>\n"
        f"📅 تاریخ عضویت: {user.created_at.strftime('%Y-%m-%d')}\n"
        f"💰 موجودی کیف پول: <code>{int(user.wallet_balance):,}</code> تومان"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💰 شارژ دستی کیف پول", callback_data=f"adm_cust_chg_{user.id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗂 سرویسهای مشتری", callback_data=f"adm_cust_srv_{user.id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت به لیست", callback_data="adm_cust_list_1"
                )
            ],
        ]
    )
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_cust_chg_"))
async def admin_wallet_charge_start(
    callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    user_id_str = callback.data.replace("adm_cust_chg_", "")
    if not user_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    user_id = int(user_id_str)

    admin_vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not admin_vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    target_user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not target_user:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    if target_user.vendor_id != admin_vendor.id:
        await callback.answer("❌ دسترسی غیرمجاز", show_alert=True)
        return

    await state.update_data(target_user_id=user_id)
    await state.set_state(AdminCustomerStates.waiting_for_wallet_charge_amount)

    text = (
        "💰 <b>شارژ دستی کیف پول</b>\n\n"
        "لطفاً مبلغ را به تومان ارسال کنید (مثبت برای افزایش، منفی برای کسر):\n"
        "مثال: <code>50000</code> یا <code>-20000</code>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="admin_my_customers")]
        ]
    )
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.message(AdminCustomerStates.waiting_for_wallet_charge_amount)
async def process_wallet_charge_amount(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return

    try:
        amount = float(message.text.strip())
    except (ValueError, TypeError):
        await message.answer("❌ مبلغ نامعتبر.")
        return

    data = await state.get_data()
    target_user_id_raw = data.get("target_user_id")
    if not target_user_id_raw:
        await state.clear()
        return

    target_user_id = int(target_user_id_raw)

    admin_vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == message.from_user.id)
        )
    ).scalar_one_or_none()
    if not admin_vendor:
        await state.clear()
        await message.answer("❌ فروشنده یافت نشد.")
        return

    user = (
        await db_session.execute(select(User).where(User.id == target_user_id))
    ).scalar_one_or_none()
    if not user:
        await state.clear()
        await message.answer("❌ کاربر یافت نشد.")
        return

    if user.vendor_id != admin_vendor.id:
        await state.clear()
        await message.answer("❌ دسترسی غیرمجاز")
        return

    user.wallet_balance += amount
    await db_session.commit()
    await state.clear()

    await message.answer(
        f"✅ موجودی کاربر {user.telegram_id} با موفقیت تغییر کرد.\n"
        f"💰 موجودی جدید: <code>{int(user.wallet_balance):,}</code> تومان"
    )
    try:
        bot = message.bot
        if bot is not None:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=(
                    f"💰 موجودی کیف پول شما تغییر کرد.\n"
                    f"مبلغ: <code>{int(amount):,}</code> تومان\n"
                    f"موجودی جدید: <code>{int(user.wallet_balance):,}</code> تومان"
                ),
            )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("adm_cust_srv_"))
async def admin_customer_services(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    user_id_str = callback.data.replace("adm_cust_srv_", "")
    if not user_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    user_id = int(user_id_str)

    admin_vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not admin_vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    user = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    if user.vendor_id != admin_vendor.id:
        await callback.answer("❌ دسترسی غیرمجاز", show_alert=True)
        return

    transactions = (
        (
            await db_session.execute(
                select(Transaction)
                .options(selectinload(Transaction.plan))
                .where(
                    Transaction.user_id == user.id,
                    Transaction.plan_id.is_not(None),
                    Transaction.status == "approved",
                )
                .order_by(Transaction.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    kb_rows: list[list[InlineKeyboardButton]] = []
    if not transactions:
        text = "🗂 این کاربر هنوز سرویسی خریداری نکرده است."
    else:
        text = (
            "🗂 <b>سرویسهای مشتری</b>\n\nبرای مشاهده جزئیات زنده روی یک سرویس کلیک کنید:"
        )
        for tx in transactions:
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📦 {tx.plan.title if tx.plan else 'نامشخص'}",
                        callback_data=f"adm_usr_srv_{tx.id}",
                    )
                ]
            )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔙 بازگشت", callback_data=f"adm_cust_det_{user.id}"
            )
        ]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_usr_srv_"))
async def admin_customer_service_monitor(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return
    tx_id_str = callback.data.replace("adm_usr_srv_", "")
    if not tx_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    tx_id = int(tx_id_str)

    admin_vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not admin_vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

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

    if tx.user.vendor_id != admin_vendor.id:
        await callback.answer("❌ دسترسی غیرمجاز", show_alert=True)
        return

    server: Server = tx.plan.server
    username = f"U_{tx.user.telegram_id}_{tx.id}_{int(tx.created_at.timestamp())}"

    client = MarzbanClient(
        base_url=server.panel_url, username=server.username, password=server.password
    )
    api_data: Dict[str, Any] | None = None
    try:
        api_data = await client.get_user(username=username)
    except Exception as e:
        logger.error(f"Admin live monitoring failed for tx {tx_id}: {e}")
        api_data = None
    finally:
        await client.close()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 بروزرسانی", callback_data=f"adm_usr_srv_{tx.id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت", callback_data=f"adm_cust_srv_{tx.user.id}"
                )
            ],
        ]
    )

    if api_data is None:
        text = (
            "📡 <b>مانیتورینگ سرویس</b>\n\n"
            f"🛍 <b>سرویس:</b> {tx.plan.title if tx.plan else 'نامشخص'}\n"
            f"🖥 <b>سرور:</b> {server.name}\n\n"
            "❌ <b>سرور در حال حاضر قابل دسترسی نیست.</b>\n"
            "لطفاً بعداً مجدداً تلاش کنید."
        )
    else:
        data_limit = api_data.get("data_limit") or 0
        used_traffic = api_data.get("used_traffic") or 0
        expire_val = api_data.get("expire")
        status = api_data.get("status") or "unknown"

        total_gb = data_limit / (1024**3) if data_limit else 0.0
        used_gb = used_traffic / (1024**3) if used_traffic else 0.0
        remaining_gb = max(total_gb - used_gb, 0.0) if data_limit else 0.0

        remaining_days = 0
        has_expire = False
        if expire_val:
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
# 🌟 مدیریت کدهای تخفیف
# ==========================================
@router.callback_query(F.data == "admin_discounts")
async def admin_discounts_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    stmt = (
        select(DiscountCode)
        .where(DiscountCode.vendor_id == vendor.id)
        .order_by(DiscountCode.id.desc())
    )
    codes = (await db_session.execute(stmt)).scalars().all()

    kb_rows: list[list[InlineKeyboardButton]] = []
    for dc in codes:
        status = "فعال" if dc.is_active else "غیرفعال"
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"🎁 {dc.code} ({dc.discount_percent}% - {status})",
                    callback_data=f"dc_view_{dc.id}",
                ),
                InlineKeyboardButton(
                    text="🗑 حذف",
                    callback_data=f"delete_dc_{dc.id}",
                ),
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="➕ افزودن کد تخفیف", callback_data="dc_add")]
    )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
            )
        ]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    text = "🎁 <b>مدیریت کدهای تخفیف</b>\n\n" + (
        "کدهای تخفیف شما:" if codes else "هیچ کد تخفیفی ثبت نشده است."
    )

    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text=text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.callback_query(F.data == "dc_add")
async def admin_discount_add_start(callback: types.CallbackQuery, state: FSMContext):
    if not callback.from_user:
        await callback.answer()
        return
    text = (
        "➕ <b>افزودن کد تخفیف</b>\n\n"
        "لطفاً کد تخفیف را ارسال کنید (مثال: SUMMER50):\n"
        "⚠️ فقط حروف انگلیسی و اعداد، بین ۳ تا ۵۰ کاراکتر."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="admin_discounts")]
        ]
    )
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text=text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await state.set_state(AdminDiscountStates.waiting_for_discount_code)
    await callback.answer()


@router.message(AdminDiscountStates.waiting_for_discount_code)
async def process_discount_code_input(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return

    code = message.text.strip()
    if not (code.isalnum() and 3 <= len(code) <= 50):
        await message.answer(
            "❌ کد نامعتبر است. فقط حروف انگلیسی و اعداد، بین ۳ تا ۵۰ کاراکتر."
        )
        return

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == message.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await message.answer("❌ فروشنده یافت نشد.")
        await state.clear()
        return

    exists = (
        await db_session.execute(
            select(DiscountCode).where(
                DiscountCode.vendor_id == vendor.id, DiscountCode.code == code
            )
        )
    ).scalar_one_or_none()
    if exists:
        await message.answer("❌ این کد قبلاً ثبت شده است. لطفاً کد دیگری وارد کنید.")
        return

    await state.update_data(discount_code=code, discount_vendor_id=vendor.id)
    await state.set_state(AdminDiscountStates.waiting_for_discount_percent)
    await message.answer("🎯 حالا درصد تخفیف را ارسال کنید (عدد ۱ تا ۱۰۰):")


@router.message(AdminDiscountStates.waiting_for_discount_percent)
async def process_discount_percent(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return

    percent_str = message.text.strip()
    if not percent_str.isdigit():
        await message.answer("❌ لطفاً فقط عدد ارسال کنید.")
        return
    percent = int(percent_str)
    if percent < 1 or percent > 100:
        await message.answer("❌ درصد باید بین ۱ و ۱۰۰ باشد.")
        return

    data = await state.get_data()
    code = data.get("discount_code")
    vendor_id_raw = data.get("discount_vendor_id")
    if not code or not vendor_id_raw:
        await message.answer("❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.")
        await state.clear()
        return

    new_dc = DiscountCode(
        vendor_id=int(vendor_id_raw),
        code=str(code),
        discount_percent=percent,
        is_active=True,
    )
    db_session.add(new_dc)
    await db_session.commit()
    await state.clear()
    await message.answer(f"✅ کد تخفیف «{code}» با {percent}% تخفیف ایجاد شد.")

    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if text:
        await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data.startswith("delete_dc_"))
async def admin_discount_delete(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return
    dc_id_str = callback.data.replace("delete_dc_", "")
    if not dc_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    dc_id = int(dc_id_str)

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    dc = (
        await db_session.execute(
            select(DiscountCode).where(
                DiscountCode.id == dc_id,
                DiscountCode.vendor_id == vendor.id,
            )
        )
    ).scalar_one_or_none()
    if not dc:
        await callback.answer("❌ کد یافت نشد یا متعلق به شما نیست.", show_alert=True)
        return

    await db_session.delete(dc)
    await db_session.commit()
    await callback.answer("✅ کد تخفیف حذف شد", show_alert=True)

    new_callback = callback.model_copy(update={"data": "admin_discounts"})
    await admin_discounts_menu(new_callback, db_session)


# ==========================================
# 🌟 تنظیمات ریدایرکت فروشنده
# ==========================================
@router.callback_query(F.data == "admin_redirect")
async def admin_redirect_menu(
    callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession
):
    if not callback.from_user:
        await callback.answer()
        return

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    target_name = "ندارد"
    if vendor.redirect_target_id:
        target = (
            await db_session.execute(
                select(Vendor).where(Vendor.id == vendor.redirect_target_id)
            )
        ).scalar_one_or_none()
        if target:
            target_name = f"{target.name} (ID: {target.id})"

    text = (
        "🔄 <b>تنظیمات ریدایرکت</b>\n\n"
        "با فعال‌سازی ریدایرکت، تمام تراکنش‌ها و تیکت‌های کاربران شما به فروشنده هدف منتقل می‌شود.\n\n"
        f"🎯 <b>فروشنده هدف فعلی:</b> {target_name}\n\n"
        "برای تنظیم فروشنده هدف، شناسه عددی آن فروشنده را ارسال کنید.\n"
        "برای حذف ریدایرکت، عدد 0 را ارسال کنید."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="back_to_admin_panel")]
        ]
    )
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text=text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await state.set_state(AdminRedirectStates.waiting_for_redirect_target_id)
    await callback.answer()


@router.message(AdminRedirectStates.waiting_for_redirect_target_id)
async def process_redirect_target(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return

    target_str = message.text.strip()
    if not target_str.isdigit():
        await message.answer("❌ لطفاً فقط شناسه عددی ارسال کنید.")
        return
    target_id = int(target_str)

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == message.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await message.answer("❌ فروشنده یافت نشد.")
        await state.clear()
        return

    if target_id == 0:
        vendor.redirect_target_id = None
        await db_session.commit()
        await state.clear()
        await message.answer("✅ ریدایرکت با موفقیت حذف شد.")
    else:
        if target_id == vendor.id:
            await message.answer("❌ نمی‌توانید ریدایرکت را به خودتان تنظیم کنید.")
            return
        target_vendor = (
            await db_session.execute(select(Vendor).where(Vendor.id == target_id))
        ).scalar_one_or_none()
        if not target_vendor:
            await message.answer("❌ فروشنده‌ای با این شناسه یافت نشد.")
            return
        vendor.redirect_target_id = target_id
        await db_session.commit()
        await state.clear()
        await message.answer(f"✅ ریدایرکت به «{target_vendor.name}» تنظیم شد.")

    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if text:
        await message.answer(text, reply_markup=reply_markup)


# ==========================================
# 🌟 گزارش مالی پیشرفته
# ==========================================
@router.callback_query(F.data == "admin_reports")
async def admin_reports(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    vendor = (
        await db_session.execute(
            select(Vendor).where(Vendor.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    direct_sales = await db_session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.vendor_id == vendor.id,
            Transaction.status == "approved",
            or_(
                Transaction.origin_vendor_id.is_(None),
                Transaction.origin_vendor_id == vendor.id,
            ),
        )
    )
    direct_sales = int(direct_sales or 0)

    routed_to_others = await db_session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.origin_vendor_id == vendor.id,
            Transaction.vendor_id != vendor.id,
            Transaction.status == "approved",
        )
    )
    routed_to_others = int(routed_to_others or 0)

    received_from_others = await db_session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.vendor_id == vendor.id,
            Transaction.origin_vendor_id.is_not(None),
            Transaction.origin_vendor_id != vendor.id,
            Transaction.status == "approved",
        )
    )
    received_from_others = int(received_from_others or 0)

    total_deposits = direct_sales + received_from_others

    server_stats = (
        await db_session.execute(
            select(
                Server.name.label("server_name"),
                func.coalesce(func.sum(Plan.volume_gb), 0).label("total_gb"),
                func.count(Plan.id).label("plans_sold"),
            )
            .join(Plan, Plan.server_id == Server.id)
            .join(Transaction, Transaction.plan_id == Plan.id)
            .where(
                Transaction.vendor_id == vendor.id,
                Transaction.status == "approved",
            )
            .group_by(Server.id, Server.name)
            .order_by(Server.name)
        )
    ).all()

    report_lines: list[str] = [
        "📊 <b>گزارش مالی</b>\n",
        f"👤 <b>فروشگاه:</b> {vendor.name}\n",
        "━━━━━━━━━━━━━\n",
        "💼 <b>گزارش فروش تفکیک‌شده</b>",
        f"🟢 فروش مستقیم خودتان: <code>{direct_sales:,}</code> تومان",
        f"📤 ارجاع داده شده به همکاران: <code>{routed_to_others:,}</code> تومان",
        f"📥 دریافت شده از همکاران: <code>{received_from_others:,}</code> تومان",
        "",
        f"💰 <b>کل واریزی به حساب شما:</b> <code>{total_deposits:,}</code> تومان\n",
        "━━━━━━━━━━━━━\n",
        "🖥 <b>آمار سرورها</b>\n",
    ]

    if server_stats:
        for row in server_stats:
            report_lines.extend(
                [
                    f"🖥 سرور: {row.server_name}",
                    f" ├ 📦 تعداد پلن فروخته شده: {int(row.plans_sold)}",
                    f" └ 💽 مجموع حجم فروش: {int(row.total_gb)} گیگابایت\n",
                ]
            )
    else:
        report_lines.append("هیچ فروشی ثبت نشده است.")

    owner_id_str = os.getenv("OWNER_ID")
    is_owner = bool(owner_id_str and callback.from_user.id == int(owner_id_str))
    if is_owner:
        global_sales = await db_session.scalar(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.status == "approved",
            )
        )
        global_sales = int(global_sales or 0)
        global_volume = await db_session.scalar(
            select(func.coalesce(func.sum(Plan.volume_gb), 0))
            .join(Transaction, Transaction.plan_id == Plan.id)
            .where(Transaction.status == "approved")
        )
        global_volume = int(global_volume or 0)

        report_lines.extend(
            [
                "\n━━━━━━━━━━━━━\n",
                "👑 <b>آمار جهانی بات (مالک)</b>",
                f"💰 مجموع فروش کل بات: <code>{global_sales:,}</code> تومان",
                f"💽 مجموع حجم فروخته‌شده: <code>{global_volume}</code> GB",
            ]
        )

    text = "\n".join(report_lines)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
                )
            ]
        ]
    )
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text=text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


# ==========================================
# 🌟 مدیریت شرکا (Owner Only)
# ==========================================
@router.callback_query(F.data == "owner_manage_vendors")
async def admin_manage_vendors_menu(
    callback: types.CallbackQuery, db_session: AsyncSession
):
    if not callback.from_user:
        await callback.answer()
        return

    owner_id_str = os.getenv("OWNER_ID")
    if not (owner_id_str and callback.from_user.id == int(owner_id_str)):
        await callback.answer(
            "❌ این بخش فقط برای مالک بات قابل دسترسی است.", show_alert=True
        )
        return

    vendors = (
        (await db_session.execute(select(Vendor).order_by(Vendor.id.asc())))
        .scalars()
        .all()
    )

    kb_rows: list[list[InlineKeyboardButton]] = []
    for v in vendors:
        status = "🟢" if v.is_active else "🔴"
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {v.name} (ID: {v.id})",
                    callback_data=f"adm_ven_{v.id}",
                )
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="➕ افزودن شریک جدید", callback_data="adm_ven_add")]
    )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel"
            )
        ]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    text = (
        "👑 <b>مدیریت شرکا</b>\n\n"
        f"تعداد کل شرکا: <code>{len(vendors)}</code>\n\n"
        "برای افزودن شریک جدید، روی دکمه زیر کلیک کنید."
    )

    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text=text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.callback_query(F.data == "adm_ven_add")
async def admin_add_vendor_start(callback: types.CallbackQuery, state: FSMContext):
    if not callback.from_user:
        await callback.answer()
        return

    owner_id_str = os.getenv("OWNER_ID")
    if not (owner_id_str and callback.from_user.id == int(owner_id_str)):
        await callback.answer("❌ دسترسی غیرمجاز.", show_alert=True)
        return

    text = (
        "➕ <b>افزودن شریک جدید</b>\n\n"
        "لطفاً شناسه تلگرام (Telegram ID) عددی شریک جدید را ارسال کنید:\n"
        "مثال: <code>123456789</code>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="owner_manage_vendors")]
        ]
    )
    if isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(text=text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await state.set_state(AdminPartnerStates.waiting_for_partner_telegram_id)
    await callback.answer()


@router.message(AdminPartnerStates.waiting_for_partner_telegram_id)
async def process_partner_telegram_id(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return

    tg_id_str = message.text.strip()
    if not tg_id_str.isdigit():
        await message.answer("❌ لطفاً فقط شناسه عددی ارسال کنید.")
        return
    tg_id = int(tg_id_str)

    existing = (
        await db_session.execute(select(Vendor).where(Vendor.telegram_id == tg_id))
    ).scalar_one_or_none()
    if existing:
        await message.answer(
            f"❌ این شناسه قبلاً به عنوان شریک ثبت شده است: {existing.name}"
        )
        return

    await state.update_data(new_vendor_tg_id=tg_id)
    await state.set_state(AdminPartnerStates.waiting_for_partner_name)
    await message.answer("✅ شناسه ثبت شد.\n🏷 حالا نام فروشگاه این شریک را ارسال کنید:")


@router.message(AdminPartnerStates.waiting_for_partner_name)
async def process_partner_name(
    message: types.Message, state: FSMContext, db_session: AsyncSession
):
    if not message.text or not message.from_user:
        return

    name = message.text.strip()
    if not (2 <= len(name) <= 50):
        await message.answer("❌ نام باید بین ۲ و ۵۰ کاراکتر باشد.")
        return

    data = await state.get_data()
    tg_id_raw = data.get("new_vendor_tg_id")
    if not tg_id_raw:
        await message.answer("❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.")
        await state.clear()
        return

    new_vendor = Vendor(
        telegram_id=int(tg_id_raw),
        name=name,
        is_active=True,
    )
    db_session.add(new_vendor)
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ شریک جدید با موفقیت ایجاد شد!\n\n"
        f"🏷 نام: {name}\n"
        f"🆔 تلگرام: <code>{int(tg_id_raw)}</code>\n\n"
        "این کاربر با ارسال /admin می‌تواند وارد پنل مدیریت شود."
    )

    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if text:
        await message.answer(text, reply_markup=reply_markup)
