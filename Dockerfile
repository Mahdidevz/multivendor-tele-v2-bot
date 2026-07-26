# 1. استفاده از پایتون
FROM python:3.11-slim

# 2. ارسال لحظه‌ای لاگ‌ها به Railway (برای رفع باگ‌های احتمالی)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 3. نصب قطعی ابزار pg_dump
RUN apt-get update && \
    apt-get install -y postgresql-client && \
    rm -rf /var/lib/apt/lists/*

# 4. کپی و نصب نیازمندی‌ها
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. کپی کل پروژه
COPY . .

# 6. 🌟 اجرای مایگریشن دیتابیس و سپس روشن کردن ربات 🌟
# دستور اجرای موقت برای گرفتن ارور دقیق پایتون
CMD ["sh", "-c", "alembic upgrade head && python -u main.py"]
