import os
import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)

async def perform_database_backup(bot: Bot):
    """
    این تابع به صورت غیرهمزمان از دیتابیس بکاپ می‌گیرد و به پی‌وی ادمین می‌فرستد.
    """
    backup_filename = None
    try:
        db_url = os.getenv("DATABASE_URL")
        owner_id = os.getenv("OWNER_ID")

        if not db_url or not owner_id:
            logger.error("⚠️ DATABASE_URL یا OWNER_ID برای بکاپ‌گیری تنظیم نشده است.")
            return

        # ابزار pg_dump با پیشوند asyncpg کار نمی‌کند، باید آن را به فرمت استاندارد برگردانیم
        if db_url.startswith("postgresql+asyncpg://"):
            db_url = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)

        # ساخت نام فایل با تاریخ و ساعت دقیق
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"backup_{timestamp}.sql"

        logger.info(f"🔄 در حال گرفتن بکاپ دیتابیس: {backup_filename}")

        # 🌟 اجرای دستور pg_dump در پس‌زمینه (بدون متوقف کردن ربات)
        process = await asyncio.create_subprocess_shell(
            f"pg_dump '{db_url}' -f {backup_filename}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        # بررسی خطای احتمالی در فرآیند بکاپ
        if process.returncode != 0:
            error_msg = stderr.decode('utf-8')
            logger.error(f"❌ خطا در بکاپ‌گیری: {error_msg}")

            # ارسال پیام خطا به ادمین (در صورت شکست)
            await bot.send_message(
                chat_id=owner_id,
                text=f"❌ <b>خطا در بکاپ‌گیری خودکار دیتابیس!</b>\n\n<pre>{error_msg[:800]}</pre>"
            )
            return

        # 🌟 ارسال فایل بکاپ به تلگرام ادمین
        file = FSInputFile(backup_filename)
        await bot.send_document(
            chat_id=owner_id,
            document=file,
            caption=(
                f"📦 <b>بکاپ خودکار دیتابیس</b>\n"
                f"📅 تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"✅ بکاپ با موفقیت انجام شد."
            )
        )
        logger.info("✅ بکاپ با موفقیت برای ادمین ارسال شد.")

    except Exception as e:
        # 🌟 گارد امنیتی: هر خطایی رخ دهد اینجا مهار می‌شود و ربات کرش نمی‌کند
        logger.error(f"❌ خطای بحرانی در سیستم بکاپ: {e}")
        try:
            owner_id = os.getenv("OWNER_ID")
            if owner_id:
                await bot.send_message(
                    chat_id=owner_id,
                    text=f"❌ <b>خطای غیرمنتظره در اسکریپت بکاپ:</b>\n<pre>{str(e)[:800]}</pre>"
                )
        except:
            pass

    finally:
        # 🧹 پاکسازی فایل از روی سرور تا هارد سرور پر نشود
        if backup_filename and os.path.exists(backup_filename):
            try:
                os.remove(backup_filename)
                logger.info(f"🗑 فایل {backup_filename} از روی سرور پاک شد.")
            except Exception as e:
                logger.error(f"⚠️ خطا در حذف فایل بکاپ: {e}")
