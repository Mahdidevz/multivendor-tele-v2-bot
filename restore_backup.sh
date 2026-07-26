#!/bin/bash
# ---------------------------------------------------------------------------
# اسکریپت بازگردانی بکاپ دیتابیس از فایل ZIP تولید شده توسط ربات.
# این اسکریپت رو روی سیستم خودتون اجرا کنید (نه روی Railway) و به psql نیاز داره.
#
# پیش‌نیاز: ساختار جدول‌ها باید از قبل با «alembic upgrade head» ساخته شده باشه.
#
# استفاده:
#   bash restore_backup.sh backup_20260726_120000.zip "postgresql://user:pass@host:port/dbname"
# ---------------------------------------------------------------------------

set -e

BACKUP_ZIP="$1"
DB_URL="$2"

if [ -z "$BACKUP_ZIP" ] || [ -z "$DB_URL" ]; then
    echo "استفاده صحیح:"
    echo "  bash restore_backup.sh <فایل بکاپ.zip> <DATABASE_URL>"
    exit 1
fi

if ! command -v psql &> /dev/null; then
    echo "❌ ابزار psql روی این سیستم نصب نیست. اول postgresql-client رو نصب کنید."
    exit 1
fi

RESTORE_DIR="restore_tmp_$$"
mkdir -p "$RESTORE_DIR"

echo "📦 در حال استخراج فایل بکاپ..."
unzip -o "$BACKUP_ZIP" -d "$RESTORE_DIR" > /dev/null

echo ""
echo "⚠️  توجه:"
echo "   - اگه جدول‌ها از قبل داده دارن، باید اول خالی‌شون کنید (TRUNCATE)، وگرنه به خطای"
echo "     duplicate key برمی‌خورید."
echo "   - اگه بین جدول‌ها foreign key هست، ترتیب بازگردانی مهمه (اول جدول مرجع، بعد وابسته)."
echo ""
read -p "آیا مطمئنید می‌خواهید ادامه بدید؟ (y/n) " confirm
if [ "$confirm" != "y" ]; then
    echo "لغو شد."
    rm -rf "$RESTORE_DIR"
    exit 0
fi

for csv_file in "$RESTORE_DIR"/*.csv; do
    table_name=$(basename "$csv_file" .csv)
    echo "⏳ بازگردانی جدول: $table_name"
    psql "$DB_URL" -c "\copy \"$table_name\" FROM '$csv_file' WITH (FORMAT csv, HEADER true)"
done

rm -rf "$RESTORE_DIR"

echo ""
echo "✅ بازگردانی کامل شد."
echo ""
echo "نکته: شمارنده‌ی ستون‌های SERIAL/IDENTITY به‌صورت خودکار به‌روز نمی‌شه."
echo "برای هر جدولی که id خودکار داره، این کوئری رو توی دیتابیس اجرا کنید:"
echo "  SELECT setval(pg_get_serial_sequence('table_name', 'id'), (SELECT MAX(id) FROM table_name));"
