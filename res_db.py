import asyncio
import asyncpg

async def reset_database():
    print("Connecting to PostgreSQL server...")
    # به دیتابیس سیستمی postgres وصل می‌شویم تا بتوانیم دیتابیس خودمان را ریست کنیم
    conn = await asyncpg.connect('postgresql://bot_admin:secretpassword@localhost:5433/postgres')
    try:
        print("Dropping existing database...")
        await conn.execute('DROP DATABASE IF EXISTS multivendor_db WITH (FORCE);')

        print("Creating fresh empty database...")
        await conn.execute('CREATE DATABASE multivendor_db;')

        print("✅ دیتابیس multivendor_db با موفقیت پاک و از نو ساخته شد!")
    except Exception as e:
        print(f"❌ خطا: {e}")
    finally:
        await conn.close()

asyncio.run(reset_database())
