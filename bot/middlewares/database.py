from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import async_sessionmaker

class DatabaseMiddleware(BaseMiddleware):
    def __init__(self, session_pool: async_sessionmaker):
        super().__init__()
        self.session_pool = session_pool

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # ۱. باز کردن یک سشن جدید برای این پیام
        async with self.session_pool() as session:

            # ۲. تزریق سشن به دیتای در حال گردش تا هندلرها بتونن ازش استفاده کنن
            data["db_session"] = session

            # ۳. پاس دادن کنترل به هندلر (مثلاً هندلر دستور استارت یا کلیک دکمه)
            result = await handler(event, data)

            # ۴. برگرداندن نتیجه (سشن با خروج از بلاک with به طور خودکار بسته می‌شود)
            return result
