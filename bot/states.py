from aiogram.fsm.state import State, StatesGroup

class WalletChargeStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_receipt = State()

class AdminStates(StatesGroup):
    waiting_for_card_number = State()

class AddServerStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_url = State()
    waiting_for_username = State()
    waiting_for_password = State()

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
