import asyncio
import asyncpg

async def reset_migration_history():
    conn = await asyncpg.connect('postgresql://bot_admin:secretpassword@localhost:5433/multivendor_db')
    try:
        # حذف جدول تاریخچه مایگریشن‌ها
        await conn.execute('DROP TABLE IF EXISTS alembic_version CASCADE;')
        print("✅ تاریخچه مایگریشن در دیتابیس لوکال با موفقیت پاک شد!")
    finally:
        await conn.close()

asyncio.run(reset_migration_history())
