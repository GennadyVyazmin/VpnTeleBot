[file name]: callback_handlers.py
[file content begin]
import os
import telebot
import logging
import subprocess
from telebot import types
from database import db
from vpn_manager import vpn_manager
from utils import format_traffic_stats, get_backup_info_text
from config import Config

logger = logging.getLogger(__name__)


def setup_callback_handlers(bot):
    """Настройка обработчиков callback запросов"""

    # ========== ОБРАБОТЧИКИ ДЛЯ START КНОПОК ==========

    @bot.callback_query_handler(func=lambda call: call.data.startswith('start_'))
    def handle_start_buttons(call):
        user_id = call.from_user.id

        if not db.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        action = call.data.replace('start_', '')

        if action == 'adduser':
            from handlers.user_handlers import user_states

            # Сохраняем состояние пользователя
            user_states[user_id] = {'waiting_for_username': True}

            bot.send_message(
                call.message.chat.id,
                'Введите имя пользователя (только латиница, цифры, _ и -):'
            )
            bot.answer_callback_query(call.id, "⚡ Введите имя пользователя")

        elif action == 'listusers':
            from handlers.user_handlers import list_users
            class FakeMessage:
                def __init__(self):
                    self.chat = type('obj', (object,), {'id': call.message.chat.id})()
                    self.from_user = call.from_user

            fake_msg = FakeMessage()
            list_users(fake_msg)
            bot.answer_callback_query(call.id, "⚡ Список пользователей")

        elif action == 'stats':
            from handlers.user_handlers import show_stats
            class FakeMessage:
                def __init__(self):
                    self.chat = type('obj', (object,), {'id': call.message.chat.id})()
                    self.from_user = call.from_user

            fake_msg = FakeMessage()
            show_stats(fake_msg)
            bot.answer_callback_query(call.id, "⚡ Статистика сервера")

        elif action == 'userstats':
            from handlers.user_handlers import user_stats
            class FakeMessage:
                def __init__(self):
                    self.chat = type('obj', (object,), {'id': call.message.chat.id})()
                    self.from_user = call.from_user

            fake_msg = FakeMessage()
            user_stats(fake_msg)
            bot.answer_callback_query(call.id, "⚡ Статистика пользователей")

        elif action == 'activestats':
            from handlers.user_handlers import show_active_stats
            class FakeMessage:
                def __init__(self):
                    self.chat = type('obj', (object,), {'id': call.message.chat.id})()
                    self.from_user = call.from_user

            fake_msg = FakeMessage()
            show_active_stats(fake_msg)
            bot.answer_callback_query(call.id, "⚡ Активные подключения")

        elif action == 'admin':
            from handlers.admin_handlers import admin_panel
            class FakeMessage:
                def __init__(self):
                    self.chat = type('obj', (object,), {'id': call.message.chat.id})()
                    self.from_user = call.from_user

            fake_msg = FakeMessage()
            admin_panel(fake_msg)
            bot.answer_callback_query(call.id, "⚡ Панель администратора")

        elif action == 'manage_admins':
            from handlers.admin_handlers import manage_admins
            class FakeMessage:
                def __init__(self):
                    self.chat = type('obj', (object,), {'id': call.message.chat.id})()
                    self.from_user = call.from_user

            fake_msg = FakeMessage()
            manage_admins(fake_msg)
            bot.answer_callback_query(call.id, "⚡ Управление админами")

        elif action == 'deleteuser':
            from handlers.admin_handlers import delete_user
            class FakeMessage:
                def __init__(self):
                    self.chat = type('obj', (object,), {'id': call.message.chat.id})()
                    self.from_user = call.from_user

            fake_msg = FakeMessage()
            delete_user(fake_msg)
            bot.answer_callback_query(call.id, "⚡ Удаление пользователя")

    # ... остальной код остается без изменений (оставил только то, что нужно) ...

    @bot.callback_query_handler(func=lambda call: call.data.startswith('add_'))
    def handle_add_methods(call):
        user_id = call.from_user.id

        if not db.is_super_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        method = call.data

        if method == 'add_manual':
            from handlers.user_handlers import user_states
            user_states[user_id] = {'waiting_for_admin_id': True}

            bot.send_message(call.message.chat.id, "Введите ID пользователя для добавления в администраторы:")
            bot.answer_callback_query(call.id, "📝 Ввод ID")

        elif method == 'add_forward':
            msg = bot.send_message(
                call.message.chat.id,
                "Перешлите любое сообщение от пользователя, которого хотите добавить в администраторы."
            )
            bot.answer_callback_query(call.id, "🔗 Перешлите сообщение")

        elif method == 'add_contact':
            # Запрашиваем контакт через кнопку "Поделиться контактом"
            keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            contact_button = types.KeyboardButton("📇 Поделиться контактом", request_contact=True)
            cancel_button = types.KeyboardButton("❌ Отмена")
            keyboard.add(contact_button, cancel_button)

            msg = bot.send_message(
                call.message.chat.id,
                "Нажмите кнопку ниже, чтобы поделиться контактом из вашего списка контактов Telegram:",
                reply_markup=keyboard
            )
            bot.answer_callback_query(call.id, "📇 Запрос контакта")

        elif method == 'add_from_users':
            # Показываем список пользователей бота
            show_users_list_for_admin(bot, call.message.chat.id, call.id)

        elif method == 'add_cancel':
            bot.send_message(call.message.chat.id, "❌ Добавление админа отменено")
            bot.answer_callback_query(call.id, "❌ Отменено")


# ... остальные функции остаются без изменений ...
[file content end]