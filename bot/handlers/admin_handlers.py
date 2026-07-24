import os
import io
import time
import logging
from typing import Sequence

import qrcode
from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, update, func
from sqlalchemy.orm import selectinload
from aiogram.fsm.context import FSMContext

from bot.states import AdminStates, AddPlanStates, EditPlanStates, AddServerStates, AdminTicketStates
from core.database.models import Plan, Vendor, Transaction, Server, VendorServer, Ticket
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

    # 🌟 [جدید] شمارش فیشها و تیکتهای در انتظار برای نمایش Badge
    # نکته: فقط تراکنشهایی که واقعاً عکس فیش آپلود شده دارند شمرده میشوند
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

    # تبدیل به int (در صورت None بودن نتیجه scalar)
    pending_tx_count = int(pending_tx_count or 0)
    pending_ticket_count = int(pending_ticket_count or 0)

    # 🌟 ساخت متن دکمهها با Badge
    receipts_label = f"💳 بررسی فیشها ({pending_tx_count})"
    tickets_label = f"📨 پیامهای پشتیبانی ({pending_ticket_count})"

    kb = [
        [
            InlineKeyboardButton(text="👥 مشتریان من", callback_data="admin_my_users"),
            InlineKeyboardButton(text=receipts_label, callback_data="admin_receipts")
        ],
        [
            InlineKeyboardButton(text="🔄 تنظیمات ریدایرکت", callback_data="admin_redirect"),
            InlineKeyboardButton(text="⚙️ اطلاعات پرداخت من", callback_data="admin_payment_info")
        ],
        [
            InlineKeyboardButton(text="📊 گزارش مالی", callback_data="admin_reports"),
            InlineKeyboardButton(text="🖥 مدیریت سرورها", callback_data="admin_view_servers")
        ],
        [
            InlineKeyboardButton(text="🛍 مدیریت پلنها", callback_data="admin_manage_plans"),
            InlineKeyboardButton(text=tickets_label, callback_data="admin_support_tickets")
        ],
    ]

    if is_owner:
        kb.append([
            InlineKeyboardButton(text="👑 مدیریت کل شرکا", callback_data="owner_manage_vendors")
        ])

    kb.append([InlineKeyboardButton(text="🔙 بستن پنل", callback_data="close_admin_panel")])
    reply_markup = InlineKeyboardMarkup(inline_keyboard=kb)

    status_text = "🟢 فعال" if vendor.is_active else "🔴 غیرفعال"
    redirect_text = "ندارد" if not vendor.redirect_to_id else "روشن 🔄"

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
    if not message.from_user: return
    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if not text:
        await message.answer("❌ شما دسترسی به پنل مدیریت ندارید.")
        return
    await message.answer(text, reply_markup=reply_markup)

@router.callback_query(F.data == "back_to_admin_panel")
async def callback_back_to_admin_panel(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    await state.clear()
    if not callback.from_user: return
    text, reply_markup = await get_admin_panel_content(callback.from_user.id, db_session)
    if text and isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=reply_markup)
    await callback.answer()

@router.callback_query(F.data == "close_admin_panel")
async def close_panel(callback: types.CallbackQuery):
    if isinstance(callback.message, types.Message):
        await callback.message.delete()
    await callback.answer()


# ==========================================
# 🌟 [جدید] لیست فیشهای در انتظار (Pending Receipts)
# ==========================================
@router.callback_query(F.data == "admin_receipts")
async def admin_receipts_list(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    vendor = (
        await db_session.execute(select(Vendor).where(Vendor.telegram_id == callback.from_user.id))
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    # واکشی تمام تراکنشهای pending این فروشنده بههمراه کاربر و پلن
    # نکته: فقط تراکنشهایی که عکس فیش دارند (سبدهای رهاشده مستثنی میشوند)
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
        empty_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel")]
        ])
        list_text = "💳 <b>فیشهای در انتظار</b>\n\nهیچ فیش جدیدی وجود ندارد."
        if isinstance(callback.message, types.Message):
            if callback.message.photo:
                # بازگشت از صفحه فیش (عکس): کیبورد عکس پاک شود و لیست متن جدید ارسال گردد
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
        kb.append([
            InlineKeyboardButton(
                text=f"🧾 فیش کاربر {user_display} - {int(tx.amount):,} ت",
                callback_data=f"adm_rcp_{tx.id}",
            )
        ])
    kb.append([InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel")])

    list_text = "💳 <b>فیشهای در انتظار</b>\n\nلطفاً یک فیش را برای بررسی انتخاب کنید:"
    list_kb = InlineKeyboardMarkup(inline_keyboard=kb)

    if isinstance(callback.message, types.Message):
        if callback.message.photo:
            # 🌟 بازگشت از صفحه فیش: عکس در چت باقی بماند، فقط کیبوردش پاک شود و لیست متن جدید ارسال گردد
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            await callback.message.answer(text=list_text, reply_markup=list_kb)
        else:
            # پیمایش عادی از منوهای متنی
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
        await callback.answer("❌ این فیش قبلاً بررسی شده یا وجود ندارد.", show_alert=True)
        return

    bot = callback.bot
    if not bot:
        await callback.answer("❌ خطای سیستمی: بات در دسترس نیست.", show_alert=True)
        return

    user = tx.user
    if not user:
        await callback.answer("❌ کاربر مرتبط یافت نشد.", show_alert=True)
        return

    # ساخت متن اطلاعات فیش
    plan_text = "شارژ عادی کیف پول"
    if tx.plan:
        plan_text = f"{tx.plan.title}"

    caption = (
        f"🧾 <b>بررسی فیش</b>\n\n"
        f"👤 <b>کاربر:</b> <code>{user.telegram_id}</code>\n"
        f"💰 <b>مبلغ:</b> <code>{int(tx.amount):,}</code> تومان\n"
        f"📌 <b>بابت:</b> {plan_text}\n"
        f"🕒 <b>زمان:</b> {tx.created_at.strftime('%Y-%m-%d %H:%M')}\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ تایید فیش", callback_data=f"admin_approve_tx_{tx.id}"),
            InlineKeyboardButton(text="❌ رد فیش", callback_data=f"admin_reject_tx_{tx.id}"),
        ],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="admin_receipts")],
    ])

    # حذف کیبورد پیام فعلی
    if isinstance(callback.message, types.Message):
        await callback.message.edit_reply_markup(reply_markup=None)

    # ارسال عکس فیش به همراه دکمههای تایید/رد
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
            # fallback: ارسال فقط متن
            await bot.send_message(chat_id=callback.from_user.id, text=caption, reply_markup=kb)
    else:
        # اگر عکس نداشت، فقط متن بفرست
        await bot.send_message(chat_id=callback.from_user.id, text=caption, reply_markup=kb)

    await callback.answer()

# ==========================================
# 🌟 اطلاعات پرداخت ادمین
# ==========================================
@router.callback_query(F.data == "admin_payment_info")
async def ask_vendor_card(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    if not callback.from_user: return
    vendor = (await db_session.execute(select(Vendor).where(Vendor.telegram_id == callback.from_user.id))).scalar_one_or_none()
    if not vendor: return

    current_card = vendor.card_number or "ثبت نشده"
    text = f"💳 <b>اطلاعات پرداخت شما</b>\n\nشماره کارت فعلی: <code>{current_card}</code>\n\nلطفاً شماره کارت جدید خود را ارسال کنید:"
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="back_to_admin_panel")]])
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=cancel_kb)
    await state.set_state(AdminStates.waiting_for_card_number)
    await callback.answer()

@router.message(AdminStates.waiting_for_card_number)
async def process_vendor_card(message: types.Message, state: FSMContext, db_session: AsyncSession):
    if not message.text or not message.from_user: return
    new_card = message.text.strip()
    vendor = (await db_session.execute(select(Vendor).where(Vendor.telegram_id == message.from_user.id))).scalar_one_or_none()
    if vendor:
        vendor.card_number = new_card
        await db_session.commit()
        await message.answer(f"✅ شماره کارت با موفقیت به <code>{new_card}</code> تغییر یافت.")
    await state.clear()

    # بازگشت خودکار
    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if text: await message.answer(text, reply_markup=reply_markup)


# ==========================================
# 🌟 مدیریت سرورها
# ==========================================
@router.callback_query(F.data == "admin_view_servers")
async def admin_view_servers(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    vendor = (await db_session.execute(select(Vendor).where(Vendor.telegram_id == callback.from_user.id))).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ دسترسی یافت نشد.", show_alert=True)
        return

    owner_id_str = os.getenv("OWNER_ID")
    is_owner = bool(owner_id_str and callback.from_user.id == int(owner_id_str))

    # 🌟 سرورهای قابل دسترس برای این فروشنده:
    #   a) سرورهایی که خودش مالک آن است (Server.vendor_id == vendor.id)
    #   b) سرورهایی که از طریق جدول VendorServer با او اشتراک داده شده است
    shared_subq = select(VendorServer.server_id).where(VendorServer.vendor_id == vendor.id)
    stmt = select(Server).where(
        or_(
            Server.vendor_id == vendor.id,
            Server.id.in_(shared_subq)
        )
    )
    servers = (await db_session.execute(stmt)).scalars().all()

    kb: list[list[InlineKeyboardButton]] = []
    if is_owner:
        kb.append([InlineKeyboardButton(text="➕ افزودن سرور (اشتراک انتخابی)", callback_data="add_srv_selective")])
    kb.append([InlineKeyboardButton(text="➕ افزودن سرور اختصاصی", callback_data="add_srv_private")])

    for srv in servers:
        icon = "🟢" if srv.is_active else "🔴"
        # 🌟 نمایش «مالک/اشتراکی» بر اساس مالکیت واقعی
        ownership = "مالک" if srv.vendor_id == vendor.id else "اشتراکی"
        kb.append([InlineKeyboardButton(text=f"{icon} {srv.name} ({ownership})", callback_data=f"srv_det_{srv.id}")])

    kb.append([InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel")])
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "🖥 <b>مدیریت سرورها</b>\n\nسرورهای قابل دسترس در سیستم:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
        )
    await callback.answer()


@router.callback_query(F.data.in_(["add_srv_selective", "add_srv_private"]))
async def start_add_server(callback: types.CallbackQuery, state: FSMContext):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    # 🌟 «selective» یعنی فلو مالک با اشتراک‌گذاری انتخابی؛ «private» یعنی سرور اختصاصی بدون اشتراک
    is_selective = (callback.data == "add_srv_selective")
    await state.update_data(is_selective=is_selective, is_private=(not is_selective))

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data="admin_view_servers")]])
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "🖥 <b>افزودن سرور</b>\n\nنام سرور را وارد کنید (مثال: سرور آلمان ۱):",
            reply_markup=cancel_kb
        )
    await state.set_state(AddServerStates.waiting_for_name)
    await callback.answer()

@router.message(AddServerStates.waiting_for_name)
async def srv_name(message: types.Message, state: FSMContext):
    if not message.text: return
    await state.update_data(srv_name=message.text.strip())
    await message.answer("🌐 <b>آدرس پنل</b> را وارد کنید (مثال: https://sub.domain.com:8000):")
    await state.set_state(AddServerStates.waiting_for_url)

@router.message(AddServerStates.waiting_for_url)
async def srv_url(message: types.Message, state: FSMContext):
    if not message.text: return
    await state.update_data(srv_url=message.text.strip())
    await message.answer("👤 <b>نام کاربری (Username)</b> ادمین پنل:")
    await state.set_state(AddServerStates.waiting_for_username)

@router.message(AddServerStates.waiting_for_username)
async def srv_user(message: types.Message, state: FSMContext):
    if not message.text: return
    await state.update_data(srv_user=message.text.strip())
    await message.answer("🔑 <b>رمز عبور (Password)</b>:")
    await state.set_state(AddServerStates.waiting_for_password)

@router.message(AddServerStates.waiting_for_password)
async def srv_pass(message: types.Message, state: FSMContext, db_session: AsyncSession):
    if not message.text or not message.from_user:
        return

    data = await state.get_data()
    password = message.text.strip()

    vendor = (await db_session.execute(select(Vendor).where(Vendor.telegram_id == message.from_user.id))).scalar_one_or_none()
    if not vendor:
        await message.answer("❌ فروشنده یافت نشد.")
        await state.clear()
        return

    # 🌟 تست واقعی اتصال به API قبل از ثبت سرور
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
        text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
        if text:
            await message.answer(text, reply_markup=reply_markup)
        return

    is_selective = bool(data.get("is_selective", False))

    # 🌟 برای فلو Owner (selective): پس از موفقیت API، به مرحله انتخاب شرکا منتقل میشویم.
    if is_selective:
        # استخراج لیست شرکای فعال (به جز خود مالک)
        stmt_vendors = select(Vendor).where(
            Vendor.is_active == True,
            Vendor.id != vendor.id
        ).order_by(Vendor.id.asc())
        all_vendors = (await db_session.execute(stmt_vendors)).scalars().all()

        if not all_vendors:
            # شرکایی وجود ندارند؛ سرور را بدون اشتراک ثبت میکنیم
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
                "ℹ️ هیچ شرک فعالی برای اشتراکگذاری وجود نداشت، بنابراین سرور اختصاصی شما باقی ماند."
            )
            await state.clear()
            text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
            if text:
                await message.answer(text, reply_markup=reply_markup)
            return

        # ذخیره رمز عبور و شناسه مالک برای مرحله نهایی
        await state.update_data(
            srv_password=password,
            owner_vendor_id=vendor.id,
            selected_vendor_ids=[],  # مجموعه خالی از انتخابها
        )

        kb = _build_vendor_selection_keyboard(all_vendors, selected_ids=[])
        if isinstance(message, types.Message):
            await message.answer(
                "👥 <b>مرحله اشتراکگذاری انتخابی</b>\n\n"
                "سرور با موفقیت به API متصل شد.\n"
                "اکنون انتخاب کنید کدام شرکا به این سرور دسترسی داشته باشند:\n"
                "✅ = انتخاب شده | ⬜ = انتخاب نشده",
                reply_markup=kb
            )
        await state.set_state(AddServerStates.waiting_for_vendor_selection)
        return

    # 🌟 برای فلو private (یا غیر-owner): ثبت مستقیم سرور اختصاصی
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
    """
    تست اتصال به پنل Marzban قبل از ثبت سرور.
    در صورت موفقیت True و در غیر این صورت False برمیگرداند.
    """
    # گارد: بررسی مقادیر ورودی خالی
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
    """ساخت کیبورد انتخاب شرکا با حالت toggle."""
    kb: list[list[InlineKeyboardButton]] = []
    for v in vendors:
        mark = "✅" if v.id in selected_ids else "⬜"
        kb.append([
            InlineKeyboardButton(
                text=f"{mark} {v.name}",
                callback_data=f"srv_togv_{v.id}",
            )
        ])
    kb.append([InlineKeyboardButton(text="💾 ثبت نهایی سرور", callback_data="srv_save_vendor_sharing")])
    kb.append([InlineKeyboardButton(text="❌ لغو", callback_data="admin_view_servers")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(AddServerStates.waiting_for_vendor_selection, F.data.startswith("srv_togv_"))
async def toggle_vendor_in_sharing(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
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

    # بازسازی کیبورد با انتخاب جدید
    stmt_vendors = select(Vendor).where(
        Vendor.is_active == True,
        Vendor.id != data.get("owner_vendor_id")
    ).order_by(Vendor.id.asc())
    all_vendors = (await db_session.execute(stmt_vendors)).scalars().all()

    kb = _build_vendor_selection_keyboard(all_vendors, selected_ids=selected_ids)
    if isinstance(callback.message, types.Message):
        await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


@router.callback_query(AddServerStates.waiting_for_vendor_selection, F.data == "srv_save_vendor_sharing")
async def save_server_with_vendor_sharing(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    data = await state.get_data()

    # گارد: بررسی وجود تمام کلیدهای لازم در FSM
    required_keys = ("srv_name", "srv_url", "srv_user", "srv_password", "owner_vendor_id")
    if not all(k in data for k in required_keys):
        await callback.answer("❌ اطلاعات سرور ناقص است. لطفاً دوباره تلاش کنید.", show_alert=True)
        await state.clear()
        text, reply_markup = await get_admin_panel_content(callback.from_user.id, db_session)
        if text and isinstance(callback.message, types.Message):
            await callback.message.edit_text(text, reply_markup=reply_markup)
        return

    owner_vendor_id: int = int(data["owner_vendor_id"])
    selected_vendor_ids: list[int] = list(data.get("selected_vendor_ids", []))

    # ساخت و ذخیره سرور مالک
    new_srv = Server(
        vendor_id=owner_vendor_id,
        is_shared=False,  # دیگر سرور عمومی نیست؛ اشتراک انتخابی است
        name=data["srv_name"],
        panel_url=data["srv_url"],
        username=data["srv_user"],
        password=data["srv_password"],
        is_active=True,
    )
    db_session.add(new_srv)
    await db_session.flush()  # گرفتن new_srv.id بدون بستن تراکنش

    # ساخت روابط اشتراک‌گذاری در جدول VendorServer
    for vid in selected_vendor_ids:
        db_session.add(VendorServer(vendor_id=vid, server_id=new_srv.id))

    await db_session.commit()
    await state.clear()

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            f"✅ <b>سرور {new_srv.name} با موفقیت ثبت شد.</b>\n"
            f"👥 تعداد شرکای دسترسی‌یافته: <b>{len(selected_vendor_ids)}</b>"
        )

    # 🌟 بازگشت خودکار به پنل ادمین
    text, reply_markup = await get_admin_panel_content(callback.from_user.id, db_session)
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

    # 🌟 بارگذاری سرور بههمراه روابط vendor و vendor_servers.vendor برای جلوگیری از خطای Lazy Load در Async
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

    # 🌟 فهرست شرکایی که این سرور با آنها اشتراک داده شده است
    shared_vendors: list[Vendor] = [vs.vendor for vs in server.vendor_servers if vs.vendor is not None]
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

    # 🌟 بررسی مالکیت: فقط مالک سرور یا Owner رسمی میتواند ویرایش کند
    server_owner_tg_id = server.vendor.telegram_id if server.vendor else None
    can_manage = bool(is_owner or (server_owner_tg_id is not None and server_owner_tg_id == callback.from_user.id))

    kb: list[list[InlineKeyboardButton]] = []
    if can_manage:
        kb.append([InlineKeyboardButton(text="تغییر وضعیت 🟢/🔴", callback_data=f"srv_tog_{server.id}")])
        kb.append([InlineKeyboardButton(text="🗑 حذف سرور", callback_data=f"srv_del_{server.id}")])

    kb.append([InlineKeyboardButton(text="🔙 بازگشت به لیست سرورها", callback_data="admin_view_servers")])

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
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
    server = (await db_session.execute(select(Server).where(Server.id == srv_id))).scalar_one_or_none()
    if not server:
        await callback.answer("❌ سرور یافت نشد.", show_alert=True)
        return

    server.is_active = not server.is_active

    # 🌟 [جدید] Cascading Plan Deactivation:
    # اگر سرور غیرفعال شد، تمام پلنهای متصل به آن نیز غیرفعال شوند.
    # ولی اگر سرور مجدداً فعال شد، پلنها بهصورت خودکار فعال نمیشوند.
    if not server.is_active:
        await db_session.execute(
            update(Plan).where(Plan.server_id == server.id).values(is_active=False)
        )

    await db_session.commit()

    # 🌟 استفاده از model_copy به جای تغییر مستقیم callback.data (Pydantic Frozen Instance)
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
    server = (await db_session.execute(select(Server).where(Server.id == srv_id))).scalar_one_or_none()
    if not server:
        await callback.answer("❌ سرور یافت نشد.", show_alert=True)
        return

    # 🌟 روابط VendorServer بهصورت Cascade روی DB حذف میشوند؛ nonetheless صریحًا پاک میکنیم تا سشن sync بماند
    vs_stmt = select(VendorServer).where(VendorServer.server_id == srv_id)
    vs_rows = (await db_session.execute(vs_stmt)).scalars().all()
    for vs in vs_rows:
        await db_session.delete(vs)

    await db_session.delete(server)
    await db_session.commit()

    await callback.answer("✅ سرور حذف شد.", show_alert=True)
    await admin_view_servers(callback, db_session)


# ==========================================
# 🌟 مدیریت پلن‌ها (بایند شده به سرور)
# ==========================================
@router.callback_query(F.data == "admin_manage_plans")
async def admin_manage_plans_menu(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن پلن جدید", callback_data="admin_add_plan_sel_srv")],
        [InlineKeyboardButton(text="📋 مشاهده پلن‌ها (بر اساس سرور)", callback_data="adm_view_srv_plans")],
        [InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel")]
    ])
    text = "🛍 <b>مدیریت پلن‌ها</b>\n\nعملیات مورد نظر خود را انتخاب کنید:"
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "admin_add_plan_sel_srv")
async def add_plan_select_server(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    vendor = (await db_session.execute(select(Vendor).where(Vendor.telegram_id == callback.from_user.id))).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    # 🌟 سرورهای قابل دسترس برای این فروشنده (فعال):
    #   a) مستقیماً متعلق به فروشنده است (Server.vendor_id == vendor.id)
    #   b) از طریق جدول VendorServer با او اشتراک داده شده است
    shared_subq = select(VendorServer.server_id).where(VendorServer.vendor_id == vendor.id)
    stmt = select(Server).where(
        Server.is_active == True,
        or_(
            Server.vendor_id == vendor.id,
            Server.id.in_(shared_subq),
        ),
    )
    servers = (await db_session.execute(stmt)).scalars().all()

    if not servers:
        await callback.answer("❌ هیچ سرور فعالی برای اتصال پلن وجود ندارد! ابتدا یک سرور اضافه کنید.", show_alert=True)
        return

    kb: list[list[InlineKeyboardButton]] = []
    for srv in servers:
        # 🌟 نوع دسترسی را بر اساس مالکیت نمایش میدهیم
        ownership = "مالک" if srv.vendor_id == vendor.id else "اشتراکی"
        kb.append([InlineKeyboardButton(text=f"🖥 {srv.name} ({ownership})", callback_data=f"addp_s_{srv.id}")])

    kb.append([InlineKeyboardButton(text="❌ لغو", callback_data="admin_manage_plans")])

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "🔗 <b>مرحله ۱: انتخاب سرور</b>\n\nاین پلن قرار است روی کدام سرور ساخته شود؟",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
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
        await callback.message.edit_text("🏷 <b>مرحله ۲: نام پلن</b>\n\nنام پلن را وارد کنید (مثلاً: <code>۱ ماهه ۵۰ گیگ</code>):")
    await state.set_state(AddPlanStates.waiting_for_title)
    await callback.answer()

@router.message(AddPlanStates.waiting_for_title)
async def plan_title(message: types.Message, state: FSMContext):
    if not message.text: return
    await state.update_data(title=message.text.strip())
    await message.answer("💽 <b>مرحله ۳: حجم (گیگابایت)</b>\n\nبرای نامحدود عدد <code>0</code> را بفرستید:")
    await state.set_state(AddPlanStates.waiting_for_volume)

@router.message(AddPlanStates.waiting_for_volume)
async def plan_volume(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ عدد وارد کنید!")
        return
    await state.update_data(volume=float(message.text))
    await message.answer("⏳ <b>مرحله ۴: اعتبار (روز)</b>\n\nبرای نامحدود عدد <code>0</code> را بفرستید:")
    await state.set_state(AddPlanStates.waiting_for_days)

@router.message(AddPlanStates.waiting_for_days)
async def plan_days(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ عدد وارد کنید!")
        return
    await state.update_data(days=int(message.text))
    await message.answer("👥 <b>مرحله ۵: محدودیت کاربر</b>\n\nبرای نامحدود عدد <code>0</code> را بفرستید:")
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
    await message.answer("📝 <b>مرحله ۷: توضیحات</b>\n\nبرای رد شدن <code>ندارد</code> را بفرستید:")
    await state.set_state(AddPlanStates.waiting_for_description)

@router.message(AddPlanStates.waiting_for_description)
async def plan_description(message: types.Message, state: FSMContext, db_session: AsyncSession):
    if not message.text or not message.from_user: return
    vendor = (await db_session.execute(select(Vendor).where(Vendor.telegram_id == message.from_user.id))).scalar_one_or_none()
    if not vendor: return

    data = await state.get_data()
    desc = message.text.strip() if message.text.strip() != "ندارد" else None

    new_plan = Plan(
        vendor_id=vendor.id,
        server_id=data['server_id'],
        title=data['title'],
        volume_gb=data['volume'],
        days=data['days'],
        user_limit=data['user_limit'],
        price=data['price'],
        description=desc
    )
    db_session.add(new_plan)
    await db_session.commit()

    await message.answer(f"✅ <b>پلن شما ساخته شد!</b>\nنام: {data['title']}")
    await state.clear()

    # 🌟 بازگشت خودکار به پنل ادمین
    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if text: await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "adm_view_srv_plans")
async def admin_view_categories_by_server(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user: return
    vendor = (await db_session.execute(select(Vendor).where(Vendor.telegram_id == callback.from_user.id))).scalar_one_or_none()
    if not vendor: return

    stmt = select(Server).join(Plan).where(Plan.vendor_id == vendor.id).distinct()
    servers_with_plans = (await db_session.execute(stmt)).scalars().all()

    if not servers_with_plans:
        await callback.answer("❌ هیچ پلنی ثبت نکرده‌اید.", show_alert=True)
        return

    kb = []
    for srv in servers_with_plans:
        kb.append([InlineKeyboardButton(text=f"🖥 {srv.name}", callback_data=f"adm_cat_{srv.id}")])

    kb.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_manage_plans")])
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text("📋 <b>سرورها</b>\n\nبرای دیدن پلن‌ها، یک سرور را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@router.callback_query(F.data.startswith("adm_cat_"))
async def admin_view_plans_in_server(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user: return
    srv_id_str = callback.data.replace("adm_cat_", "")
    if not srv_id_str.isdigit(): return
    srv_id = int(srv_id_str)

    vendor = (await db_session.execute(select(Vendor).where(Vendor.telegram_id == callback.from_user.id))).scalar_one_or_none()
    if not vendor: return

    stmt = select(Plan).where(Plan.vendor_id == vendor.id, Plan.server_id == srv_id)
    plans = (await db_session.execute(stmt)).scalars().all()
    server = (await db_session.execute(select(Server).where(Server.id == srv_id))).scalar_one_or_none()

    kb = []
    for p in plans:
        status_icon = "🟢" if p.is_active else "🔴"
        kb.append([InlineKeyboardButton(text=f"{status_icon} {p.title}", callback_data=f"adm_p_{p.id}")])

    kb.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="adm_view_srv_plans")])
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(f"📁 <b>سرویس‌های متصل به سرور: {server.name if server else 'نامشخص'}</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@router.callback_query(F.data.startswith("adm_p_"))
async def admin_show_plan_details(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data: return
    plan_id_str = callback.data.replace("adm_p_", "")
    if not plan_id_str.isdigit(): return
    plan_id = int(plan_id_str)

    plan = (await db_session.execute(select(Plan).options(selectinload(Plan.server)).where(Plan.id == plan_id))).scalar_one_or_none()
    if not plan: return

    status = "🟢 فعال" if plan.is_active else "🔴 غیرفعال"
    vol = "نامحدود" if plan.volume_gb == 0 else f"{plan.volume_gb}GB"
    days = "نامحدود" if plan.days == 0 else f"{plan.days} روز"
    ulimit = "نامحدود" if plan.user_limit == 0 else f"{plan.user_limit} کاربر"

    text = (
        f"⚙️ <b>مدیریت پلن</b>\n\n"
        f"🔹 <b>نام:</b> {plan.title}\n"
        f"🖥 <b>سرور מתصل:</b> {plan.server.name}\n"
        f"💽 <b>حجم:</b> {vol} | ⏳ <b>اعتبار:</b> {days}\n"
        f"👥 <b>محدودیت:</b> {ulimit}\n"
        f"💵 <b>قیمت:</b> <code>{int(plan.price):,}</code> تومان\n"
        f"👁‍🗨 <b>وضعیت:</b> {status}\n"
    )

    kb = [
        [InlineKeyboardButton(text="تغییر وضعیت 🟢/🔴", callback_data=f"adm_tog_{plan.id}")],
        [
            InlineKeyboardButton(text="✏️ ویرایش قیمت", callback_data=f"adm_edp_{plan.id}"),
            InlineKeyboardButton(text="🗑 حذف پلن", callback_data=f"adm_del_{plan.id}")
        ],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data=f"adm_cat_{plan.server_id}")]
    ]

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@router.callback_query(F.data.startswith("adm_tog_"))
async def admin_toggle_plan(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data:
        await callback.answer()
        return

    plan_id_str = callback.data.replace("adm_tog_", "")
    if not plan_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return

    plan_id = int(plan_id_str)
    plan = (await db_session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if not plan:
        await callback.answer("❌ پلن یافت نشد.", show_alert=True)
        return

    plan.is_active = not plan.is_active
    await db_session.commit()

    # 🌟 استفاده از model_copy به جای تغییر مستقیم callback.data (Pydantic Frozen Instance)
    new_callback = callback.model_copy(update={"data": f"adm_p_{plan.id}"})
    await admin_show_plan_details(new_callback, db_session)


@router.callback_query(F.data.startswith("adm_del_"))
async def admin_delete_plan(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data:
        await callback.answer()
        return

    plan_id_str = callback.data.replace("adm_del_", "")
    if not plan_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return

    plan_id = int(plan_id_str)
    plan = (await db_session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if not plan:
        await callback.answer("❌ پلن یافت نشد.", show_alert=True)
        return

    srv_id = plan.server_id
    await db_session.delete(plan)
    await db_session.commit()

    await callback.answer("✅ حذف شد.", show_alert=True)

    # 🌟 استفاده از model_copy به جای تغییر مستقیم callback.data (Pydantic Frozen Instance)
    new_callback = callback.model_copy(update={"data": f"adm_cat_{srv_id}"})
    await admin_view_plans_in_server(new_callback, db_session)

@router.callback_query(F.data.startswith("adm_edp_"))
async def admin_edit_price(callback: types.CallbackQuery, state: FSMContext):
    if not callback.data: return
    plan_id = int(callback.data.replace("adm_edp_", ""))
    await state.update_data(edit_plan_id=plan_id)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data=f"adm_p_{plan_id}")]])
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text("💵 مبلغ جدید (تومان):", reply_markup=cancel_kb)
    await state.set_state(EditPlanStates.waiting_for_new_price)
    await callback.answer()

@router.message(EditPlanStates.waiting_for_new_price)
async def admin_process_new_price(message: types.Message, state: FSMContext, db_session: AsyncSession):
    if not message.text or not message.from_user or not message.text.isdigit():
        await message.answer("❌ فقط عدد وارد کنید.")
        return
    data = await state.get_data()
    plan_id = data.get("edit_plan_id")
    if plan_id:
        plan = (await db_session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
        if plan:
            plan.price = float(message.text)
            await db_session.commit()
            await message.answer("✅ قیمت بروزرسانی شد.")
    await state.clear()

    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if text:
        await message.answer(text, reply_markup=reply_markup)


# ==========================================
# 🌟 تایید و رد فیش (با API واقعی)
# ==========================================
@router.callback_query(F.data.startswith("admin_approve_tx_"))
async def admin_approve_transaction(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    tx_id_str = callback.data.replace("admin_approve_tx_", "")
    if not tx_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    tx_id = int(tx_id_str)

    stmt = select(Transaction).options(
        selectinload(Transaction.user),
        selectinload(Transaction.plan).selectinload(Plan.server)
    ).where(Transaction.id == tx_id)

    tx = (await db_session.execute(stmt)).scalar_one_or_none()

    if not tx or tx.status != "pending":
        await callback.answer("❌ این فیش قبلاً بررسی شده یا وجود ندارد.", show_alert=True)
        return

    # 🌟 گارد امنیتی: کاربر حتماً باید بارگذاری شده باشد
    user = tx.user
    if not user:
        await callback.answer("❌ کاربر مرتبط با این فیش یافت نشد.", show_alert=True)
        return

    bot = callback.bot
    if not bot:
        await callback.answer("❌ خطای سیستمی: بات در دسترس نیست.", show_alert=True)
        return

    tx.status = "approved"

    if not tx.plan_id:
        # 🌟 شارژ عادی کیف پول (بدون پلن)
        user.wallet_balance += tx.amount
        await db_session.commit()

        if isinstance(callback.message, types.Message):
            # 🌟 حذف پیام عکس فیش از چت ادمین برای جلوگیری از شلوغی
            try:
                await callback.message.delete()
            except TelegramBadRequest:
                pass

        await bot.send_message(
            chat_id=user.telegram_id,
            text=(
                f"✅ <b>واریزی شما تایید شد!</b>\n\n"
                f"مبلغ <code>{tx.amount:,}</code> تومان به کیف پول شما اضافه گردید.\n"
                f"موجودی فعلی: <code>{int(user.wallet_balance):,}</code> تومان"
            ),
        )

        # 🌟 پاک کردن مرجع عکس فیش (حفظ سابقه مالی، حذف فقط برای صرفهجویی در فضا)
        tx.receipt_file_id = None
        await db_session.commit()
    else:
        # 🌟 خرید پلن: کسر مبلغ از کیف پول + ساخت کاربر واقعی در پنل
        plan = tx.plan
        if not plan or not plan.server:
            await callback.answer("❌ پلن یا سرور مرتبط یافت نشد.", show_alert=True)
            return

        server = plan.server

        user.wallet_balance += tx.amount
        user.wallet_balance -= plan.price
        await db_session.commit()

        # ---------------------------------------------------------
        # 🌐 ساخت کاربر واقعی در پنل Marzban
        # ---------------------------------------------------------
        username = f"U_{user.telegram_id}_{tx.id}"
        expire_timestamp = str(int(time.time()) + (plan.days * 86400)) if plan.days > 0 else "0"
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
            # 🌟 حذف پیام عکس فیش از چت ادمین برای جلوگیری از شلوغی
            try:
                await callback.message.delete()
            except TelegramBadRequest:
                pass

        msg_to_user = (
            f"✅ <b>پرداخت شما تایید شد!</b>\n\n"
            f"🛍 <b>سرویس:</b> {plan.title}\n"
            f"🔗 <b>لینک اشتراک شما:</b>\n<code>{sub_url}</code>\n\n"
            f"💡 آموزش اتصال: ابتدا لینک بالا را کپی کرده و در نرمافزار خود وارد کنید."
        )

        # 🌟 [جدید] تولید QR Code از لینک اشتراک و ارسال به عنوان عکس
        if isinstance(callback.message, types.Message):
            try:
                import qrcode.constants as _qr_constants  # type: ignore[attr-defined]
                qr = qrcode.QRCode(
                    version=1,
                    error_correction=_qr_constants.ERROR_CORRECT_M,
                    box_size=10,
                    border=4,
                )
                qr.add_data(sub_url)
                qr.make(fit=True)
                qr_image = qr.make_image(fill_color="black", back_color="white")
                stream = io.BytesIO()
                # استفاده از pil.save با فرمت PNG
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
                # fallback: ارسال فقط متن در صورت شکست QR
                await bot.send_message(chat_id=user.telegram_id, text=msg_to_user)

        # 🌟 پاک کردن مرجع عکس فیش (حفظ سابقه مالی، حذف فقط برای صرفهجویی در فضا)
        tx.receipt_file_id = None
        await db_session.commit()

    await callback.answer()


@router.callback_query(F.data.startswith("admin_reject_tx_"))
async def admin_reject_transaction(callback: types.CallbackQuery, db_session: AsyncSession):
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

    # 🌟 گارد امنیتی: کاربر حتماً باید بارگذاری شده باشد
    user = tx.user
    if not user:
        await callback.answer("❌ کاربر مرتبط با این فیش یافت نشد.", show_alert=True)
        return

    bot = callback.bot
    if not bot:
        await callback.answer("❌ خطای سیستمی: بات در دسترس نیست.", show_alert=True)
        return

    tx.status = "rejected"
    await db_session.commit()

    if isinstance(callback.message, types.Message):
        # 🌟 حذف پیام عکس فیش از چت ادمین برای جلوگیری از شلوغی
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass

    # 🌟 استفاده امن از bot
    if isinstance(callback.message, types.Message):
        await bot.send_message(
            chat_id=user.telegram_id,
            text=(
                f"❌ <b>پرداخت شما تایید نشد.</b>\n\n"
                f"فیش ارسالی برای مبلغ <code>{tx.amount:,}</code> تومان توسط پشتیبانی رد شد."
            ),
        )
    await callback.answer()


# ==========================================
# 🌟 [جدید] داشبورد تیکت‌های پشتیبانی
# ==========================================
@router.callback_query(F.data == "admin_support_tickets")
async def admin_support_tickets_list(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    vendor = (
        await db_session.execute(select(Vendor).where(Vendor.telegram_id == callback.from_user.id))
    ).scalar_one_or_none()
    if not vendor:
        await callback.answer("❌ فروشنده یافت نشد.", show_alert=True)
        return

    # واکشی تیکت‌های pending این فروشنده بههمراه کاربر
    stmt = (
        select(Ticket)
        .options(selectinload(Ticket.user))
        .where(Ticket.vendor_id == vendor.id, Ticket.status == "pending")
        .order_by(Ticket.created_at.desc())
    )
    tickets = (await db_session.execute(stmt)).scalars().all()

    if not tickets:
        empty_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel")]
        ])
        if isinstance(callback.message, types.Message):
            await callback.message.edit_text(
                "📨 <b>پیامهای پشتیبانی</b>\n\nهیچ پیام جدیدی وجود ندارد.",
                reply_markup=empty_kb
            )
        await callback.answer()
        return

    kb: list[list[InlineKeyboardButton]] = []
    for t in tickets:
        user_display = t.user.telegram_id if t.user else "نامشخص"
        kb.append([
            InlineKeyboardButton(
                text=f"📨 تیکت از کاربر {user_display}",
                callback_data=f"adm_tk_{t.id}",
            )
        ])
    kb.append([InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel")])

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "📨 <b>پیامهای پشتیبانی</b>\n\nلیست پیامهای در انتظار پاسخ:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
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
        select(Ticket)
        .options(selectinload(Ticket.user))
        .where(Ticket.id == ticket_id)
    )
    ticket = (await db_session.execute(stmt)).scalar_one_or_none()
    if not ticket:
        await callback.answer("❌ تیکت یافت نشد.", show_alert=True)
        return

    user_display = ticket.user.telegram_id if ticket.user else "نامشخص"
    status_emoji = "🟡" if ticket.status == "pending" else ("🟢" if ticket.status == "answered" else "🔴")

    text = (
        f"📨 <b>تیکت پشتیبانی #{ticket.id}</b>\n\n"
        f"👤 <b>کاربر:</b> <code>{user_display}</code>\n"
        f"{status_emoji} <b>وضعیت:</b> {ticket.status}\n"
        f"🕒 <b>زمان:</b> {ticket.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"📝 <b>متن پیام:</b>\n{ticket.message_text}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ پاسخ (Reply)", callback_data=f"adm_tkr_{ticket.id}")],
        [InlineKeyboardButton(text="❌ رد/بستن (Close)", callback_data=f"adm_tkc_{ticket.id}")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="admin_support_tickets")],
    ])

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_tkr_"))
async def admin_reply_ticket_start(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    if not callback.data or not callback.from_user:
        await callback.answer()
        return

    ticket_id_str = callback.data.replace("adm_tkr_", "")
    if not ticket_id_str.isdigit():
        await callback.answer("❌ شناسه نامعتبر.", show_alert=True)
        return
    ticket_id = int(ticket_id_str)

    # بررسی وجود تیکت
    ticket = (
        await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if not ticket:
        await callback.answer("❌ تیکت یافت نشد.", show_alert=True)
        return

    await state.update_data(reply_ticket_id=ticket_id)

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"adm_tk_{ticket_id}")]
    ])
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(
            "✍️ <b>پاسخ به تیکت</b>\n\nلطفاً متن پاسخ خود را ارسال کنید:",
            reply_markup=cancel_kb
        )
    await state.set_state(AdminTicketStates.waiting_for_ticket_reply)
    await callback.answer()


@router.message(AdminTicketStates.waiting_for_ticket_reply)
async def admin_reply_ticket_send(message: types.Message, state: FSMContext, db_session: AsyncSession):
    if not message.text or not message.from_user:
        return

    data = await state.get_data()
    ticket_id = data.get("reply_ticket_id")
    if not ticket_id:
        await message.answer("❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.")
        await state.clear()
        return

    # واکشی تیکت بههمراه کاربر
    stmt = (
        select(Ticket)
        .options(selectinload(Ticket.user))
        .where(Ticket.id == int(ticket_id))
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

    # بروزرسانی وضعیت تیکت
    ticket.status = "answered"
    await db_session.commit()

    # ارسال پاسخ به کاربر از طریق بات
    msg_to_user = (
        f"💬 <b>پاسخ پشتیبانی به تیکت #{ticket.id}</b>\n\n"
        f"📝 <b>پیام اصلی شما:</b>\n{ticket.message_text}\n\n"
        f"✍️ <b>پاسخ پشتیبانی:</b>\n{reply_text}"
    )
    try:
        if message.bot:
            await message.bot.send_message(chat_id=ticket.user.telegram_id, text=msg_to_user)
        else:
            logger.error("Bot instance is not available on message; cannot send ticket reply.")
            await message.answer("⚠️ پاسخ ثبت شد اما ارسال پیام به کاربر ناموفق بود.")
    except Exception as e:
        logger.error(f"Failed to send ticket reply to user {ticket.user.telegram_id}: {e}")
        await message.answer("⚠️ پاسخ ثبت شد اما ارسال پیام به کاربر با خطا مواجه شد.")

    await state.clear()
    await message.answer("✅ پاسخ شما برای کاربر ارسال شد.")

    # بازگشت به لیست تیکت‌ها
    # ساخت یک CallbackQuery مجازی برای فراخوانی مجدد لیست (با استفاده از message)
    text = "📨 <b>پیامهای پشتیبانی</b>\n\nدر حال بازگشت به لیست..."
    # بهجای فراخوانی مستقیم، کاربر را با دکمه هدایت میکنیم
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 بازگشت به لیست تیکت‌ها", callback_data="admin_support_tickets")],
        [InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel")],
    ])
    await message.answer(text, reply_markup=kb)


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

    # بازگشت به لیست تیکت‌ها با model_copy
    new_callback = callback.model_copy(update={"data": "admin_support_tickets"})
    await admin_support_tickets_list(new_callback, db_session)
