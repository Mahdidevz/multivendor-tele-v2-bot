import os
import time
import random
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload
from aiogram.fsm.context import FSMContext

from bot.states import AdminStates, AddPlanStates, EditPlanStates, AddServerStates
from core.database.models import Plan, Vendor, Transaction, Server

# 🌟 وقتی کلاینتت آماده بود این کامنت را باز کن:
# from core.services.panel_client import MarzbanClient

router = Router()

# ==========================================
# 🌟 توابع کمکی و منوی اصلی
# ==========================================
async def get_admin_panel_content(user_id: int, db_session: AsyncSession):
    stmt = select(Vendor).where(Vendor.telegram_id == user_id)
    vendor = (await db_session.execute(stmt)).scalar_one_or_none()
    if not vendor: return None, None

    owner_id_str = os.getenv("OWNER_ID")
    is_owner = bool(owner_id_str and user_id == int(owner_id_str))

    kb = [
        [
            InlineKeyboardButton(text="👥 مشتریان من", callback_data="admin_my_users"),
            InlineKeyboardButton(text="💳 بررسی فیش‌ها", callback_data="admin_receipts")
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
            InlineKeyboardButton(text="🛍 مدیریت پلن‌ها", callback_data="admin_manage_plans")
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
    if isinstance(callback.message, types.Message): await callback.message.delete()
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
    if not callback.from_user: return
    vendor = (await db_session.execute(select(Vendor).where(Vendor.telegram_id == callback.from_user.id))).scalar_one_or_none()
    if not vendor: return

    owner_id_str = os.getenv("OWNER_ID")
    is_owner = bool(owner_id_str and callback.from_user.id == int(owner_id_str))

    stmt = select(Server).where(or_(Server.vendor_id == vendor.id, Server.is_shared == True))
    servers = (await db_session.execute(stmt)).scalars().all()

    kb = []
    if is_owner: kb.append([InlineKeyboardButton(text="➕ افزودن سرور اشتراکی (عمومی)", callback_data="add_srv_shared")])
    kb.append([InlineKeyboardButton(text="➕ افزودن سرور اختصاصی", callback_data="add_srv_private")])

    for srv in servers:
        icon = "🟢" if srv.is_active else "🔴"
        type_str = "اشتراکی" if srv.is_shared else "اختصاصی"
        kb.append([InlineKeyboardButton(text=f"{icon} {srv.name} ({type_str})", callback_data=f"srv_det_{srv.id}")])

    kb.append([InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="back_to_admin_panel")])
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text("🖥 <b>مدیریت سرورها</b>\n\nسرورهای متصل در سیستم:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()


@router.callback_query(F.data.in_(["add_srv_shared", "add_srv_private"]))
async def start_add_server(callback: types.CallbackQuery, state: FSMContext):
    is_shared = (callback.data == "add_srv_shared")
    await state.update_data(is_shared=is_shared)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data="admin_view_servers")]])
    if isinstance(callback.message, types.Message):
        await callback.message.edit_text("🖥 <b>افزودن سرور</b>\n\nنام سرور را وارد کنید (مثال: سرور آلمان ۱):", reply_markup=cancel_kb)
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
    if not message.text or not message.from_user: return
    data = await state.get_data()
    password = message.text.strip()

    vendor = (await db_session.execute(select(Vendor).where(Vendor.telegram_id == message.from_user.id))).scalar_one_or_none()
    if not vendor: return

    # --- جای لاگین و تست API با MarzbanClient ---

    new_srv = Server(
        vendor_id=None if data['is_shared'] else vendor.id,
        is_shared=data['is_shared'],
        name=data['srv_name'],
        panel_url=data['srv_url'],
        username=data['srv_user'],
        password=password,
        is_active=True
    )
    db_session.add(new_srv)
    await db_session.commit()

    await message.answer(f"✅ <b>سرور {data['srv_name']} ثبت شد.</b>")
    await state.clear()

    text, reply_markup = await get_admin_panel_content(message.from_user.id, db_session)
    if text: await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data.startswith("srv_det_"))
async def admin_server_details(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data or not callback.from_user: return
    srv_id_str = callback.data.replace("srv_det_", "")
    if not srv_id_str.isdigit(): return

    server = (await db_session.execute(select(Server).where(Server.id == int(srv_id_str)))).scalar_one_or_none()
    if not server: return

    owner_id_str = os.getenv("OWNER_ID")
    is_owner = bool(owner_id_str and callback.from_user.id == int(owner_id_str))

    status_text = "🟢 در حال کار" if server.is_active else "🔴 غیرفعال"
    type_text = "اشتراکی (عمومی)" if server.is_shared else "اختصاصی"

    text = (
        f"🖥 <b>سرور: {server.name}</b> ({type_text})\n\n"
        f"🔗 <b>آدرس:</b> <code>{server.panel_url}</code>\n"
        f"👁‍🗨 <b>وضعیت:</b> {status_text}\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"📊 در اینجا اطلاعات منابع از طریق API لود خواهد شد...\n"
    )

    kb = []
    if is_owner or (server.vendor_id and server.vendor.telegram_id == callback.from_user.id):
        kb.append([InlineKeyboardButton(text="تغییر وضعیت 🟢/🔴", callback_data=f"srv_tog_{server.id}")])
        kb.append([InlineKeyboardButton(text="🗑 حذف سرور", callback_data=f"srv_del_{server.id}")])

    kb.append([InlineKeyboardButton(text="🔙 بازگشت به لیست سرورها", callback_data="admin_view_servers")])

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@router.callback_query(F.data.startswith("srv_tog_"))
async def toggle_server_status(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data: return
    srv_id = int(callback.data.replace("srv_tog_", ""))
    server = (await db_session.execute(select(Server).where(Server.id == srv_id))).scalar_one_or_none()
    if server:
        server.is_active = not server.is_active
        await db_session.commit()
        callback.data = f"srv_det_{server.id}"
        await admin_server_details(callback, db_session)

@router.callback_query(F.data.startswith("srv_del_"))
async def delete_server(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data: return
    srv_id = int(callback.data.replace("srv_del_", ""))
    server = (await db_session.execute(select(Server).where(Server.id == srv_id))).scalar_one_or_none()
    if server:
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
    if not callback.from_user: return
    vendor = (await db_session.execute(select(Vendor).where(Vendor.telegram_id == callback.from_user.id))).scalar_one_or_none()
    if not vendor: return

    stmt = select(Server).where(Server.is_active == True, or_(Server.vendor_id == vendor.id, Server.is_shared == True))
    servers = (await db_session.execute(stmt)).scalars().all()

    if not servers:
        await callback.answer("❌ هیچ سرور فعالی برای اتصال پلن وجود ندارد! ابتدا یک سرور اضافه کنید.", show_alert=True)
        return

    kb = []
    for srv in servers:
        type_str = "اشتراکی" if srv.is_shared else "اختصاصی"
        kb.append([InlineKeyboardButton(text=f"🖥 {srv.name} ({type_str})", callback_data=f"addp_s_{srv.id}")])

    kb.append([InlineKeyboardButton(text="❌ لغو", callback_data="admin_manage_plans")])

    if isinstance(callback.message, types.Message):
        await callback.message.edit_text("🔗 <b>مرحله ۱: انتخاب سرور</b>\n\nاین پلن قرار است روی کدام سرور ساخته شود؟", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()


@router.callback_query(F.data.startswith("addp_s_"))
async def plan_title_start(callback: types.CallbackQuery, state: FSMContext):
    if not callback.data: return
    srv_id = int(callback.data.replace("addp_s_", ""))
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
    if not callback.data: return
    plan_id = int(callback.data.replace("adm_tog_", ""))
    plan = (await db_session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if plan:
        plan.is_active = not plan.is_active
        await db_session.commit()
        callback.data = f"adm_p_{plan.id}"
        await admin_show_plan_details(callback, db_session)

@router.callback_query(F.data.startswith("adm_del_"))
async def admin_delete_plan(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data: return
    plan_id = int(callback.data.replace("adm_del_", ""))
    plan = (await db_session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if plan:
        srv_id = plan.server_id
        await db_session.delete(plan)
        await db_session.commit()
        await callback.answer("✅ حذف شد.", show_alert=True)
        callback.data = f"adm_cat_{srv_id}"
        await admin_view_plans_in_server(callback, db_session)

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
    if not message.text or not message.text.isdigit():
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
    if text: await message.answer(text, reply_markup=reply_markup)


# ==========================================
# 🌟 تایید و رد فیش (با API کامنت شده)
# ==========================================
@router.callback_query(F.data.startswith("admin_approve_tx_"))
async def admin_approve_transaction(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data: return
    tx_id_str = callback.data.replace("admin_approve_tx_", "")
    if not tx_id_str.isdigit(): return
    tx_id = int(tx_id_str)

    stmt = select(Transaction).options(
        selectinload(Transaction.user),
        selectinload(Transaction.plan).selectinload(Plan.server)
    ).where(Transaction.id == tx_id)

    tx = (await db_session.execute(stmt)).scalar_one_or_none()

    if not tx or tx.status != "pending":
        await callback.answer("❌ این فیش قبلاً بررسی شده یا وجود ندارد.", show_alert=True)
        return

    user = tx.user
    tx.status = "approved"

    if not tx.plan_id:
        user.wallet_balance += tx.amount
        await db_session.commit()

        # 🌟 استفاده از bot.send_message برای رفع ارور InaccessibleMessage
        if isinstance(callback.message, types.Message):
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.reply(f"✅ فیش تایید شد.")

        await callback.bot.send_message(
            user.telegram_id,
            f"✅ <b>واریزی شما تایید شد!</b>\n\nمبلغ <code>{tx.amount:,}</code> تومان به کیف پول شما اضافه گردید.\nموجودی فعلی: {int(user.wallet_balance):,} تومان"
        )
    else:
        plan = tx.plan
        if not plan: return

        user.wallet_balance += tx.amount
        user.wallet_balance -= plan.price
        await db_session.commit()

        # ---------------------------------------------------------
        # 🌐 درخواست به API
        # ---------------------------------------------------------
        """
        # client = MarzbanClient(plan.server.panel_url, plan.server.username, plan.server.password)
        # try:
        #     await client.login()
        #     expire_timestamp = str(int(time.time()) + (plan.days * 86400)) if plan.days > 0 else "0"
        #     data_limit_bytes = int(plan.volume_gb * 1073741824) if plan.volume_gb > 0 else 0
        #
        #     api_result = await client.create_user(
        #         username=f"U_{user.telegram_id}_{tx.id}",
        #         data_limit_bytes=data_limit_bytes,
        #         expire_iso=expire_timestamp,
        #         hwid_limit=plan.user_limit
        #     )
        #     sub_url = api_result.get("subscription_url", "خطا در لینک")
        # except Exception as e:
        #     sub_url = f"Error: {e}"
        # finally:
        #     await client.close()
        """

        mock_sub_link = f"https://vpn-sub.com/sub/{user.telegram_id}/{random.randint(1000, 9999)}"

        if isinstance(callback.message, types.Message):
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.reply(f"✅ فیش تایید شد و سرویس کاربر صادر گردید.")

        msg_to_user = (
            f"✅ <b>پرداخت شما تایید شد!</b>\n\n"
            f"🛍 <b>سرویس:</b> {plan.title}\n"
            f"🔗 <b>لینک اشتراک شما:</b>\n<code>{mock_sub_link}</code>\n\n"
            f"💡 آموزش اتصال: ابتدا لینک بالا را کپی کرده و در نرم‌افزار خود وارد کنید."
        )

        # 🌟 استفاده امن از bot
        await callback.bot.send_message(user.telegram_id, msg_to_user)

    await callback.answer()

@router.callback_query(F.data.startswith("admin_reject_tx_"))
async def admin_reject_transaction(callback: types.CallbackQuery, db_session: AsyncSession):
    if not callback.data: return
    tx_id_str = callback.data.replace("admin_reject_tx_", "")
    if not tx_id_str.isdigit(): return
    tx_id = int(tx_id_str)

    tx = (await db_session.execute(select(Transaction).options(selectinload(Transaction.user)).where(Transaction.id == tx_id))).scalar_one_or_none()
    if not tx or tx.status != "pending":
        await callback.answer("❌ فیش قبلاً بررسی شده.", show_alert=True)
        return

    tx.status = "rejected"
    await db_session.commit()

    if isinstance(callback.message, types.Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("❌ فیش رد شد و به کاربر اطلاع داده شد.")

    await callback.bot.send_message(
        tx.user.telegram_id,
        f"❌ <b>پرداخت شما تایید نشد.</b>\n\nفیش ارسالی برای مبلغ <code>{tx.amount:,}</code> تومان توسط پشتیبانی رد شد."
    )
    await callback.answer()
