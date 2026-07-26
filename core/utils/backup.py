"""
core/utils/backup.py

بکاپ‌گیری کامل دیتابیس بدون هیچ وابستگی به باینری خارجی (pg_dump).
هر جدول از اسکیمای public با استفاده از پروتکل COPY خودِ PostgreSQL
(همون مکانیزم داخلی‌ای که pg_dump هم ازش استفاده می‌کنه) به CSV تبدیل می‌شه،
همه‌ی فایل‌های CSV در یک ZIP فشرده می‌شن و به پی‌وی ادمین ارسال می‌شن.

نکته: این بکاپ فقط داده‌ها رو نگه می‌داره، نه ساختار جدول‌ها (DDL).
چون پروژه از Alembic استفاده می‌کنه، ساختار همیشه با `alembic upgrade head`
قابل بازسازیه؛ نیازی نیست بکاپ خودش هم مسئول این کار باشه.
"""

import os
import zipfile
import logging
from io import BytesIO
from datetime import datetime

import asyncpg
from aiogram import Bot
from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)

# سقف امن برای آپلود فایل توسط ربات‌های تلگرام
# (سقف واقعی تلگرام ۵۰ مگابایته، برای اطمینان کمی پایین‌تر می‌گیریم)
TELEGRAM_MAX_FILE_SIZE = 49 * 1024 * 1024


async def _get_public_tables(conn: asyncpg.Connection) -> list[str]:
    """لیست تمام جدول‌های واقعی (بدون view) داخل اسکیمای public رو برمی‌گردونه"""
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;"
    )
    return [row["tablename"] for row in rows]


async def _table_to_csv_bytes(conn: asyncpg.Connection, table_name: str) -> bytes:
    """
    خروجی گرفتن از یک جدول به فرمت CSV با دستور COPY خودِ PostgreSQL.
    ایمنه چون asyncpg خودش اسم جدول رو درست quote می‌کنه (بدون ریسک SQL injection).
    """
    buffer = BytesIO()
    await conn.copy_from_table(table_name, output=buffer, format="csv", header=True)
    return buffer.getvalue()


async def _notify_owner(bot: Bot, owner_id: str, text: str) -> None:
    """ارسال پیام به ادمین، طوری که اگه خودِ ارسال هم خطا بده برنامه کرش نکنه"""
    try:
        await bot.send_message(chat_id=owner_id, text=text)
    except Exception as notify_error:
        logger.error(f"⚠️ حتی ارسال پیام خطا هم ناموفق بود: {notify_error}")


async def perform_database_backup(bot: Bot) -> None:
    """
    این تابع به صورت غیرهمزمان از تمام جدول‌های دیتابیس بکاپ می‌گیره
    (به فرمت CSV داخل یک ZIP) و به پی‌وی ادمین می‌فرسته.
    هر خطایی رخ بده مهار می‌شه و به ادمین اطلاع داده می‌شه؛ ربات هرگز کرش نمی‌کنه.
    """
    zip_filename = None
    owner_id = os.getenv("OWNER_ID")
    table_names: list[str] = []

    try:
        db_url = os.getenv("DATABASE_URL")
        if not db_url or not owner_id:
            logger.error("⚠️ DATABASE_URL یا OWNER_ID برای بکاپ‌گیری تنظیم نشده است.")
            return

        # asyncpg با پیشوند "+asyncpg" که SQLAlchemy اضافه می‌کنه آشنا نیست
        raw_dsn = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_filename = f"backup_{timestamp}.zip"

        logger.info(f"🔄 در حال گرفتن بکاپ دیتابیس: {zip_filename}")

        # 🌟 اتصال مستقل به دیتابیس، جدا از پول اصلی ربات
        # (تا با کوئری‌های خودِ ربات در حال اجرا تداخلی نداشته باشه)
        conn = await asyncpg.connect(raw_dsn, timeout=15)
        try:
            table_names = await _get_public_tables(conn)

            if not table_names:
                logger.warning("⚠️ هیچ جدولی در اسکیمای public پیدا نشد.")
                await _notify_owner(
                    bot, owner_id,
                    "⚠️ بکاپ‌گیری انجام نشد: هیچ جدولی در دیتابیس پیدا نشد."
                )
                return

            with zipfile.ZipFile(zip_filename, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                for table in table_names:
                    csv_bytes = await _table_to_csv_bytes(conn, table)
                    zf.writestr(f"{table}.csv", csv_bytes)
                    logger.info(f"  ✔️ جدول '{table}' به بکاپ اضافه شد ({len(csv_bytes)} بایت)")
        finally:
            await conn.close()

        file_size = os.path.getsize(zip_filename)

        # اگه حجم بکاپ از سقف مجاز تلگرام بیشتر شد، به‌جای تلاش ناموفق، شفاف اطلاع می‌دیم
        if file_size > TELEGRAM_MAX_FILE_SIZE:
            size_mb = file_size / (1024 * 1024)
            logger.error(f"❌ حجم بکاپ ({size_mb:.1f}MB) از سقف مجاز تلگرام بیشتره.")
            await _notify_owner(
                bot, owner_id,
                f"⚠️ <b>بکاپ گرفته شد اما قابل ارسال نبود!</b>\n\n"
                f"حجم فایل: <b>{size_mb:.1f} MB</b> — بیشتر از سقف ۵۰ مگابایتی ارسال فایل توسط ربات‌های تلگرام.\n"
                f"برای دیتابیس‌های بزرگ باید بکاپ مستقیم روی یک فضای ابری (مثل S3) آپلود بشه."
            )
            return

        await bot.send_document(
            chat_id=owner_id,
            document=FSInputFile(zip_filename),
            caption=(
                f"📦 <b>بکاپ خودکار دیتابیس</b>\n"
                f"📅 تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"🗂 تعداد جدول‌ها: {len(table_names)}\n"
                f"💾 حجم فایل: {file_size / 1024:.1f} KB\n"
                f"✅ بکاپ با موفقیت انجام شد."
            )
        )
        logger.info("✅ بکاپ با موفقیت برای ادمین ارسال شد.")

    except asyncpg.PostgresError as e:
        logger.error(f"❌ خطای دیتابیس در بکاپ‌گیری: {e}")
        if owner_id:
            await _notify_owner(
                bot, owner_id,
                f"❌ <b>خطای دیتابیس در بکاپ‌گیری:</b>\n<pre>{str(e)[:800]}</pre>"
            )

    except Exception as e:
        # 🌟 گارد امنیتی: هر خطایی رخ بده اینجا مهار می‌شه و ربات کرش نمی‌کنه
        logger.error(f"❌ خطای بحرانی در سیستم بکاپ: {e}", exc_info=True)
        if owner_id:
            await _notify_owner(
                bot, owner_id,
                f"❌ <b>خطای غیرمنتظره در اسکریپت بکاپ:</b>\n<pre>{str(e)[:800]}</pre>"
            )

    finally:
        # 🧹 پاکسازی فایل از روی سرور تا هارد سرور پر نشه
        if zip_filename and os.path.exists(zip_filename):
            try:
                os.remove(zip_filename)
                logger.info(f"🗑 فایل {zip_filename} از روی سرور پاک شد.")
            except Exception as e:
                logger.error(f"⚠️ خطا در حذف فایل بکاپ: {e}")
