import logging
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


class MarzbanClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token: Optional[str] = None
        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """ساخت یا بازگردانی سشن aiohttp"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                trust_env=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:152.0) V2RayBot/1.0",
                    "Accept": "application/json",
                },
            )
        return self.session

    async def login(self) -> bool:
        """دریافت توکن از سرور"""
        session = await self._get_session()
        url = f"{self.base_url}/api/admin/token"

        data = {
            "grant_type": "password",
            "username": self.username,
            "password": self.password,
        }

        try:
            async with session.post(url, data=data) as response:
                if response.status == 200:
                    result = await response.json()
                    self.token = result.get("access_token")
                    return True
                else:
                    error_text = await response.text()
                    logger.error(f"Login failed: HTTP {response.status} - {error_text}")
                    return False
        except Exception as e:
            logger.error(f"Connection error during login: {str(e)}")
            return False

    async def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """متد واسط برای هندل کردن ریکوئست‌ها و تزریق اتوماتیک توکن"""
        if not self.token:
            await self.login()
            if not self.token:
                raise Exception("Authentication failed. Cannot proceed with request.")

        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"

        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        kwargs["headers"] = headers

        async with session.request(method, url, **kwargs) as response:
            if response.status == 401:
                logger.info("Token expired, attempting to relogin...")
                await self.login()
                headers["Authorization"] = f"Bearer {self.token}"
                async with session.request(method, url, **kwargs) as retry_response:
                    retry_response.raise_for_status()
                    return await retry_response.json()

            response.raise_for_status()
            return await response.json()

    async def get_users(self) -> Dict[str, Any]:
        return await self._request("GET", "/api/system/users")

    async def get_resources(self) -> Dict[str, Any]:
        return await self._request("GET", "/api/system/resources")

    async def create_user(
        self, username: str, data_limit_bytes: int, expire_iso: str, hwid_limit: int = 0
    ) -> Dict[str, Any]:
        """ساخت کاربر جدید"""
        payload = {
            "username": username,
            "status": "active",
            "data_limit": data_limit_bytes,
            "hwid_limit": hwid_limit if hwid_limit > 0 else None,
            "expire": expire_iso,
            "note": "",
            "group_ids": [3],
            "proxy_settings": {
                "vmess": {},
                "vless": {},
                "trojan": {},
                "shadowsocks": {"method": "aes-256-gcm"},
            },
            "data_limit_reset_strategy": "no_reset",
        }
        return await self._request("POST", "/api/user", json=payload)

    async def get_user(self, username: str) -> Dict[str, Any]:
        """گرفتن اطلاعات و لینک اتصال یک کاربر خاص"""
        return await self._request("GET", f"/api/user/{username}")

    async def update_user(
        self, user_id: int, username: str, data_limit_bytes: int, expire_iso: str
    ) -> Dict[str, Any]:
        """ویرایش و تمدید کاربر"""
        payload = {
            "username": username,
            "data_limit": data_limit_bytes,
            "expire": expire_iso,
            "note": "",
            "data_limit_reset_strategy": "no_reset",
            "group_ids": [3],
            "proxy_settings": {
                "vmess": {},
                "vless": {},
                "trojan": {},
                "shadowsocks": {"method": "aes-256-gcm"},
            },
        }
        return await self._request("PUT", f"/api/user/by-id/{user_id}", json=payload)

    async def delete_user(self, username: str) -> bool:
        """حذف کاربر از پنل"""
        try:
            await self._request("DELETE", f"/api/user/{username}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete user {username}: {str(e)}")
            return False

    async def revoke_sub(self, username: str) -> Dict[str, Any]:
        """
        باطل کردن لینک فعلی کاربر و تولید لینک جدید
        این کار تمام پسوردها و UUID های متصل به این کاربر را تغییر می‌دهد
        """
        return await self._request("POST", f"/api/user/{username}/revoke_sub")

    async def reset_usage(self, username: str) -> Dict[str, Any]:
        """
        صفر کردن حجم مصرفی (Used Traffic) کاربر
        """
        return await self._request("POST", f"/api/user/{username}/reset")

    async def close(self):
        """بستن سشن در پایان کار"""
        if self.session and not self.session.closed:
            await self.session.close()
