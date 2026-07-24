import os
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from core.database.models import User, Vendor

async def _get_next_vendor_round_robin(session: AsyncSession) -> int | None:
    """انتخاب نوبتی وندور (Round Robin) با استفاده از کوئریهای دیتابیس"""

    # ۱. پیدا کردن وندور آخرین کاربری که در سیستم ثبتنام کرده
    stmt_last_user = select(User.vendor_id).order_by(desc(User.id)).limit(1)
    last_vendor_id = (await session.execute(stmt_last_user)).scalar_one_or_none()

    if last_vendor_id is not None:
        # ۲. پیدا کردن وندور فعال بعدی (آیدی بزرگتر از آخرین وندور تخصیص یافته)
        stmt_next_vendor = select(Vendor.id).where(
            Vendor.is_active == True,
            Vendor.id > last_vendor_id
        ).order_by(Vendor.id.asc()).limit(1)
        next_vendor_id = (await session.execute(stmt_next_vendor)).scalar_one_or_none()

        if next_vendor_id is not None:
            return next_vendor_id

    # ۳. حلقه (Loop): اگر کاربری نبود، یا به انتهای لیست وندورها رسیدیم، برگرد به اولین وندور فعال
    stmt_first_vendor = select(Vendor.id).where(
        Vendor.is_active == True
    ).order_by(Vendor.id.asc()).limit(1)

    return (await session.execute(stmt_first_vendor)).scalar_one_or_none()


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    deep_link_vendor_id: int | None = None,
) -> User | None:
    """گرفتن یا ساخت کاربر.

    قوانین پیوند فروشنده:
    - کاربر جدید: vendor_id از deep_link (اگر معتبر) وگرنه Round Robin.
    - کاربر موجود: هرگز vendor_id تغییر نمیکند (پیوند دائمی به اولین فروشنده).
    """
    owner_id_str = os.getenv("OWNER_ID")
    owner_id = int(owner_id_str) if owner_id_str else 0

    # ۱. احراز هویت مالک: اگر مالک ربات را استارت کرد، او را به عنوان وندور ثبت کن
    if telegram_id == owner_id:
        stmt_vendor = select(Vendor).where(Vendor.telegram_id == owner_id)
        vendor = (await session.execute(stmt_vendor)).scalar_one_or_none()
        if not vendor:
            new_vendor = Vendor(
                telegram_id=owner_id,
                name="Owner",
                is_active=True
            )
            session.add(new_vendor)
            # flush میکنیم تا وندور در همین سشن ثبت بشه و بتونیم به عنوان یوزر هم ثبتش کنیم
            await session.flush()

    # ۲. بررسی اینکه آیا کاربر از قبل وجود دارد؟
    stmt_user = select(User).where(User.telegram_id == telegram_id)
    user = (await session.execute(stmt_user)).scalar_one_or_none()

    # 🌟 [Bug 1 Fix] کاربر موجود: vendor_id هرگز تغییر نمیکند. پیوند دائمی است.
    if user is not None:
        return user

    # ۳. کاربر جدید است: تعیین vendor_id نهایی
    # اولویت اول: deep_link_vendor_id (اگر عدد مثبت معتبر باشد)
    assigned_vendor_id: int | None = None
    if deep_link_vendor_id is not None and deep_link_vendor_id > 0:
        # اعتبارسنجی وجود فروشنده هدف
        stmt_dl_vendor = select(Vendor.id).where(
            Vendor.id == deep_link_vendor_id,
            Vendor.is_active == True,
        )
        assigned_vendor_id = (await session.execute(stmt_dl_vendor)).scalar_one_or_none()

    # اولویت دوم: Round Robin اگر deep-link معتبر نبود
    if not assigned_vendor_id:
        assigned_vendor_id = await _get_next_vendor_round_robin(session)

    # گارد امنیتی: اگر هیچ وندوری (حتی مالک) هنوز در دیتابیس فعال نباشد
    if not assigned_vendor_id:
        return None

    # ۴. ساخت کاربر جدید و اتصال به وندورِ نهایی
    new_user = User(
        telegram_id=telegram_id,
        vendor_id=assigned_vendor_id,
        wallet_balance=0.0
    )
    session.add(new_user)
    await session.commit()
    await session.refresh(new_user)

    return new_user
