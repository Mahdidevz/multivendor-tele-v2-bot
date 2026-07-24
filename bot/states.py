from aiogram.fsm.state import State, StatesGroup

class WalletChargeStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_receipt = State()

class AdminStates(StatesGroup):
    waiting_for_card_number = State()


class UserStates(StatesGroup):
    """🌟 [جدید] وضعیت‌های مربوط به کاربر."""
    waiting_for_support_message = State()


class AdminTicketStates(StatesGroup):
    """🌟 [جدید] وضعیتهای مربوط به پاسخ به تیکت توسط ادمین."""
    waiting_for_ticket_reply = State()


# 🌟 [جدید] وضعیتهای مدیریت مشتریان توسط ادمین
class AdminCustomerStates(StatesGroup):
    waiting_for_broadcast_message = State()
    waiting_for_customer_search = State()
    waiting_for_wallet_charge_amount = State()


# 🌟 [جدید] وضعیتهای تنظیمات عضویت اجباری (Force Join)
class AdminForceJoinStates(StatesGroup):
    waiting_for_channel_data = State()

class AddServerStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_url = State()
    waiting_for_username = State()
    waiting_for_password = State()
    # 🌟 [جدید] مرحله اشتراکگذاری انتخابی سرور (Owner Only)
    waiting_for_vendor_selection = State()

class AddPlanStates(StatesGroup):
    # مرحله انتخاب دسته حذف و انتخاب سرور جایگزین شد
    waiting_for_title = State()
    waiting_for_volume = State()
    waiting_for_days = State()
    waiting_for_user_limit = State()
    waiting_for_price = State()
    waiting_for_description = State()

class EditPlanStates(StatesGroup):
    waiting_for_new_price = State()


# 🌟 [جدید] وضعیتهای مربوط به تخفیف و خرید کاربر
class PurchaseStates(StatesGroup):
    waiting_for_discount_code = State()


# 🌟 [جدید] وضعیتهای مدیریت کدهای تخفیف توسط ادمین
class AdminDiscountStates(StatesGroup):
    waiting_for_discount_code = State()
    waiting_for_discount_percent = State()


# 🌟 [جدید] وضعیتهای تنظیمات ریدایرکت فروشنده و مدیریت شرکا
class AdminRedirectStates(StatesGroup):
    waiting_for_redirect_target_id = State()


class AdminPartnerStates(StatesGroup):
    waiting_for_partner_telegram_id = State()
    waiting_for_partner_name = State()
