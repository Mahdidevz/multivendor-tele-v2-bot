from datetime import datetime
from sqlalchemy import String, Boolean, ForeignKey, DateTime, BigInteger, Float, Integer, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

# 🌟 [جدید] یادآوری: پس از تغییرات این فایل، حتماً ماگریشن Alembic بسازید:
#   alembic revision --autogenerate -m "add_discount_redirect_partner_fields"
#   alembic upgrade head

class Base(DeclarativeBase):
    pass

class Vendor(Base):
    __tablename__ = 'vendors'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(50))
    card_number: Mapped[str] = mapped_column(String(20), nullable=True)
    wallet_address: Mapped[str] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    redirect_to_id: Mapped[int] = mapped_column(ForeignKey('vendors.id', ondelete='SET NULL'), nullable=True)
    # 🌟 [جدید] ریدایرکت جامع فروش/پشتیبانی: تراکنشها و تیکتها به این فروشنده منتقل میشوند
    redirect_target_id: Mapped[int | None] = mapped_column(ForeignKey('vendors.id', ondelete='SET NULL'), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # روابط
    users = relationship("User", back_populates="vendor")
    vendor_servers = relationship("VendorServer", back_populates="vendor")
    redirect_to = relationship("Vendor", remote_side=[id], foreign_keys=[redirect_to_id], backref="redirects_received")
    plans = relationship("Plan", back_populates="vendor", cascade="all, delete-orphan")

    # 🌟 [جدید] رابطه: هر فروشنده میتواند سرورهای اختصاصی خودش را داشته باشد
    servers = relationship("Server", back_populates="vendor")

    # 🌟 [جدید] رابطه: تیکتهای پشتیبانی دریافتشده توسط این فروشنده
    tickets = relationship("Ticket", back_populates="vendor", foreign_keys="Ticket.vendor_id")

    # 🌟 [جدید] رابطه: کانالهای عضویت اجباری این فروشنده
    force_join_channels = relationship("ForceJoinChannel", back_populates="vendor", cascade="all, delete-orphan")

    # 🌟 [جدید] رابطه: کدهای تخفیف این فروشنده
    discount_codes = relationship("DiscountCode", back_populates="vendor", cascade="all, delete-orphan")

    # 🌟 [جدید] رابطه: تراکنشهایی که از این فروشنده سرچشمه گرفتهاند (ریدایرکت)
    transactions = relationship(
        "Transaction",
        back_populates="vendor",
        foreign_keys="[Transaction.vendor_id]",
    )
    origin_transactions = relationship(
        "Transaction",
        back_populates="origin_vendor",
        foreign_keys="[Transaction.origin_vendor_id]",
    )


class Server(Base):
    __tablename__ = 'servers'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 🌟 [جدید] مالکیت سرور: اگر نال باشد و is_shared ترو باشد، یعنی سرور کل سیستم است (اشتراکی)
    vendor_id: Mapped[int] = mapped_column(ForeignKey('vendors.id', ondelete='CASCADE'), nullable=True)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)

    name: Mapped[str] = mapped_column(String(50))
    panel_url: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(50))
    password: Mapped[str] = mapped_column(String(255))
    total_capacity: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    vendor_servers = relationship("VendorServer", back_populates="server")
    transactions = relationship("Transaction", back_populates="server")
    vendor = relationship("Vendor", back_populates="servers")

    # 🌟 [جدید] رابطه سرور با پلن‌ها
    plans = relationship("Plan", back_populates="server", cascade="all, delete-orphan")


class Plan(Base):
    __tablename__ = 'plans'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey('vendors.id', ondelete='CASCADE'))

    # 🌟 [آپدیت] فیلد category حذف شد و plan به یک سرور خاص متصل (Bind) می‌شود
    server_id: Mapped[int] = mapped_column(ForeignKey('servers.id', ondelete='CASCADE'))

    title: Mapped[str] = mapped_column(String(100))
    volume_gb: Mapped[float] = mapped_column(Float)
    days: Mapped[int] = mapped_column(Integer)
    user_limit: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[float] = mapped_column(Float)
    description: Mapped[str] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    vendor = relationship("Vendor", back_populates="plans")
    server = relationship("Server", back_populates="plans") # 🌟 اتصال به آبجکت سرور
    transactions = relationship("Transaction", back_populates="plan")


class VendorServer(Base):
    __tablename__ = 'vendor_servers'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey('vendors.id', ondelete='CASCADE'))
    server_id: Mapped[int] = mapped_column(ForeignKey('servers.id', ondelete='CASCADE'))
    vendor = relationship("Vendor", back_populates="vendor_servers")
    server = relationship("Server", back_populates="vendor_servers")


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey('vendors.id'))
    wallet_balance: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 🌟 [جدید] آیا کاربر قبلاً کانفیگ تست رایگان دریافت کرده است؟ (هر کاربر فقط یک بار)
    has_received_test: Mapped[bool] = mapped_column(Boolean, default=False)
    vendor = relationship("Vendor", back_populates="users")
    transactions = relationship("Transaction", back_populates="user")

    # 🌟 [جدید] رابطه: تیکتهای پشتیبانی ارسالشده توسط این کاربر
    tickets = relationship("Ticket", back_populates="user")


# 🌟 [جدید] مدل کانالهای عضویت اجباری هر فروشنده (Force Join)
class ForceJoinChannel(Base):
    """کانالهایی که کاربران باید پیش از دریافت تست رایگان در آنها عضو شوند."""
    __tablename__ = 'force_join_channels'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey('vendors.id', ondelete='CASCADE'))
    chat_id: Mapped[str] = mapped_column(String(100))   # مثال: "@mychannel" یا "-1001234567890"
    title: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(255))        # لینک دعوت

    vendor = relationship("Vendor", back_populates="force_join_channels")


class Transaction(Base):
    __tablename__ = 'transactions'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    vendor_id: Mapped[int] = mapped_column(ForeignKey('vendors.id', ondelete='CASCADE'))
    server_id: Mapped[int] = mapped_column(ForeignKey('servers.id', ondelete='SET NULL'), nullable=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey('plans.id', ondelete='SET NULL'), nullable=True)
    amount: Mapped[float] = mapped_column(Float)
    # 🌟 [جدید] فیلدهای تخفیف و ریدایرکت برای حسابداری دقیق
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    original_amount: Mapped[int] = mapped_column(Integer, default=0)
    # 🌟 [جدید] ارجاع به کد تخفیف استفادهشده (SET NULL تا سابقه مالی پس از حذف کد حفظ شود)
    discount_code_id: Mapped[int | None] = mapped_column(ForeignKey('discount_codes.id', ondelete="SET NULL"), nullable=True)
    origin_vendor_id: Mapped[int | None] = mapped_column(ForeignKey('vendors.id', ondelete='SET NULL'), nullable=True)
    receipt_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    destination_card: Mapped[str] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    user = relationship("User", back_populates="transactions")
    vendor = relationship("Vendor", back_populates="transactions", foreign_keys=[vendor_id])
    # 🌟 [جدید] فروشندهای که کاربر در اصل متعلق به او بوده (قبل از ریدایرکت)
    origin_vendor = relationship("Vendor", back_populates="origin_transactions", foreign_keys=[origin_vendor_id])
    # 🌟 [جدید] کد تخفیف مرتبط (در صورت حذف کد، این فیلد NULL میشود اما سابقه مالی حفظ میگردد)
    discount_code = relationship("DiscountCode")
    server = relationship("Server", back_populates="transactions")
    plan = relationship("Plan", back_populates="transactions")


# 🌟 [جدید] مدل کدهای تخفیف هر فروشنده
class DiscountCode(Base):
    """کد تخفیف قابل استفاده توسط کاربران هنگام خرید پلن."""
    __tablename__ = 'discount_codes'
    __table_args__ = (UniqueConstraint('vendor_id', 'code', name='uq_discount_vendor_code'),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey('vendors.id', ondelete='CASCADE'))
    code: Mapped[str] = mapped_column(String(50))
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)  # ۱ تا ۱۰۰
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    vendor = relationship("Vendor", back_populates="discount_codes")


class Ticket(Base):
    """🌟 [جدید] مدل تیکت‌های پشتیبانی کاربران."""
    __tablename__ = 'tickets'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    vendor_id: Mapped[int] = mapped_column(ForeignKey('vendors.id', ondelete='CASCADE'))
    message_text: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | answered | closed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="tickets")
    vendor = relationship("Vendor", back_populates="tickets", foreign_keys=[vendor_id])
