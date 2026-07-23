import asyncio
import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine
from core.database.models import Base

load_dotenv()

async def reset_database():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("no database url")
        return
    engine = create_async_engine(database_url, echo=True)

    async with engine.begin() as conn:
        print("🔴 Dropping all tables...")
        await conn.run_sync(Base.metadata.drop_all)

        print("🟢 Creating new tables based on updated models...")
        await conn.run_sync(Base.metadata.create_all)

    await engine.dispose()
    print("✅ Database reset successfully!")

if __name__ == "__main__":
    asyncio.run(reset_database())
