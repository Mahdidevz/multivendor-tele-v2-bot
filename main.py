import asyncio
import logging
import os
import traceback

import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession

# ۱. ایمپورت ابزارهای ساخت انجین و سشن از SQLAlchemy
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from bot.handlers.user_handlers import router as user_router
from bot.handlers.admin_handlers import router as admin_router
from bot.middlewares.database import DatabaseMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.utils.backup import perform_database_backup

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def notify_owner_of_crash(error: BaseException) -> None:
    """
    🌟 اطلاع‌رسانی مستقیم به ادمین در صورت کرش کامل ربات (نه فقط بکاپ).
    عمداً از آبجکت Bot استفاده نمی‌کنه، چون ممکنه اصلاً ساخته نشده باشه
    یا سشنش قبل از رسیدن به این نقطه بسته شده باشه؛ به‌جاش مستقیماً
    با یک درخواست خام به API تلگرام وصل می‌شه.
    """
    token = os.getenv("BOT_TOKEN")
    owner_id = os.getenv("OWNER_ID")
    if not token or not owner_id:
        # اگه BOT_TOKEN خودش موجود نباشه، اصلاً امکان ارسال پیام تلگرام نیست
        return

    tb_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    message_text = f"🔴 ربات متوقف شد (کرش کامل)!\n\n{tb_text[-3500:]}"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": owner_id, "text": message_text}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"ارسال پیام کرش به ادمین ناموفق بود: HTTP {resp.status} - {body}")
    except Exception as notify_error:
        logger.error(f"⚠️ حتی اطلاع‌رسانی کرش هم ناموفق بود: {notify_error}")


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN is not set in .env file!")
        return

    # --- بخش دیتابیس ---
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not set in .env file!")
        return

    # 🌟 [رفع ارور psycopg2]: اصلاح خودکار پیش‌وند برای سازگاری با asyncpg در Railway
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    # ساخت انجین اتصال به PostgreSQL (echo=False یعنی لاگ کوئری‌های SQL رو تو ترمینال چاپ نکن که شلوغ نشه)
    engine = create_async_engine(database_url, echo=False)
    # ساخت کارخانه تولید سشن (Session Pool)
    session_pool = async_sessionmaker(engine, expire_on_commit=False)
    # -------------------

    proxy_url = os.getenv("PROXY_URL")
    if proxy_url:
        session = AiohttpSession(proxy=proxy_url)
        logger.info(f"Using proxy: {proxy_url}")
    else:
        session = AiohttpSession()
        logger.info("Connecting directly (No Proxy)")

    bot = Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    # 🌟 راه‌اندازی سیستم زمان‌بندی (Scheduler)
    scheduler = AsyncIOScheduler()

    # ⏱️ فعلاً روی هر ۵ دقیقه تنظیمه تا مطمئن بشید روش جدید بکاپ‌گیری (بدون pg_dump) درست کار می‌کنه.
    # بعد از اینکه یکی دو بار بکاپ رو با موفقیت توی پی‌وی گرفتید، این خط رو کامنت کنید
    # و خط hours=24 پایینی رو از کامنت در بیارید تا بکاپ روزی یک بار گرفته بشه:
    scheduler.add_job(perform_database_backup, 'interval', minutes=1, args=[bot])
    # scheduler.add_job(perform_database_backup, 'interval', hours=24, args=[bot])

    scheduler.start()

    # ۳. تزریق میدل‌ور دیتابیس به تمام رویدادهای ربات
    dp.update.outer_middleware(DatabaseMiddleware(session_pool=session_pool))
    dp.include_router(admin_router)
    dp.include_router(user_router)

    logger.info("Starting bot polling...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        # ۴. بستن اصولی کانکشن‌های دیتابیس و تلگرام هنگام خاموش شدن ربات
        await bot.session.close()
        await engine.dispose()
        logger.info("Database connections closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped manually.")
    except Exception as e:
        logger.critical(f"Bot crashed with an unhandled exception: {e}", exc_info=True)
        try:
            asyncio.run(notify_owner_of_crash(e))
        except Exception:
            pass
        # 🌟 دوباره خطا رو raise می‌کنیم تا Railway بفهمه پروسه با شکست خارج شده
        # و طبق تنظیمات Restart Policy خودش، دوباره اجراش کنه
        raise
