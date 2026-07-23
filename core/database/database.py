import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from dotenv import load_dotenv

# لود کردن متغیرهای فایل .env
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("متغیر DATABASE_URL در فایل .env یافت نشد!")

# ساخت موتور اتصال به صورت Async
engine = create_async_engine(DATABASE_URL, echo=False)

# ساخت Session Factory برای ارتباط با دیتابیس در طول پروژه
async_session = async_sessionmaker(engine, expire_on_commit=False)
