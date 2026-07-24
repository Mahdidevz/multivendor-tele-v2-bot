import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from dotenv import load_dotenv

# لود کردن متغیرهای فایل .env (برای اجرای لوکال)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("متغیر DATABASE_URL یافت نشد!")

# 🌟 [جادوی Railway]: اصلاح پیش‌وند دیتابیس برای پشتیبانی از asyncpg
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# ساخت موتور اتصال به صورت Async
engine = create_async_engine(DATABASE_URL, echo=False)

# ساخت Session Factory برای ارتباط با دیتابیس در طول پروژه
async_session = async_sessionmaker(engine, expire_on_commit=False)
