# 1. استفاده از پایتون نسخه 3.11 (سبک و سریع)
FROM python:3.11-slim

# 2. تنظیم پوشه کاری داخل سرور
WORKDIR /app

# 3. 🌟 نصب قطعی ابزار pg_dump از مخازن اصلی لینوکس 🌟
RUN apt-get update && \
    apt-get install -y postgresql-client && \
    rm -rf /var/lib/apt/lists/*

# 4. کپی کردن فایل نیازمندی‌ها
COPY requirements.txt .

# 5. نصب کتابخانه‌های پایتون (از جمله aiogram و sqlalchemy)
RUN pip install --no-cache-dir -r requirements.txt

# 6. کپی کردن کل کدهای ربات به داخل سرور
COPY . .

# 7. دستور اجرای ربات
CMD ["python", "main.py"]
