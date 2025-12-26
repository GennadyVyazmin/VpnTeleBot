import telebot
import logging
import shutil
import os
from telebot import types
from datetime import datetime
from database import db
from vpn_manager import vpn_manager
from utils import get_backup_info_text, format_database_info, format_bytes
from config import Config

logger = logging.getLogger(__name__)


def setup_admin_handlers(bot):
    """Настройка обработчиков админ команд"""

    @bot.message_handler(commands=['admin'])
    def admin_panel(message):
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Доступ запрещен")
            return

        logger.info(f"Открытие админ-панели администратором {user_id}")

        if db.is_super_admin(user_id):
            buttons = [
                [types.InlineKeyboardButton("📊 Статистика", callback_data='admin_stats')],
                [types.InlineKeyboardButton("🔄 Перезапустить VPN", callback_data='admin_restart')],
                [types.InlineKeyboardButton("💾 Создать бэкап", callback_data='admin_backup')],
                [types.InlineKeyboardButton("📋 Список бэкапов", callback_data='admin_backup_list')],
                [types.InlineKeyboardButton("🧹 Очистить БД", callback_data='admin_clear_db')],
                [types.InlineKeyboardButton("👑 Управление админами", callback_data='admin_manage')]
            ]
        else:
            buttons = [
                [types.InlineKeyboardButton("📊 Статистика", callback_data='admin_stats')],
                [types.InlineKeyboardButton("🔄 Перезапустить VPN", callback_data='admin_restart')],
                [types.InlineKeyboardButton("💾 Создать бэкап", callback_data='admin_backup')],
                [types.InlineKeyboardButton("📋 Список бэкапов", callback_data='admin_backup_list')]
            ]

        markup = types.InlineKeyboardMarkup(buttons)
        bot.send_message(message.chat.id, "👨‍💻 Панель администратора", reply_markup=markup)

    @bot.message_handler(commands=['manage_admins'])
    def manage_admins(message):
        user_id = message.from_user.id

        if not db.is_super_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Только для супер-администратора")
            return

        logger.info(f"Команда /manage_admins от супер-администратора {user_id}")

        buttons = [
            [types.InlineKeyboardButton("👥 Список админов", callback_data='admin_list')],
            [types.InlineKeyboardButton("➕ Добавить админа", callback_data='admin_add')],
            [types.InlineKeyboardButton("➖ Удалить админа", callback_data='admin_remove')]
        ]

        markup = types.InlineKeyboardMarkup(buttons)
        bot.send_message(message.chat.id, "👑 Управление администраторами", reply_markup=markup)

    @bot.message_handler(commands=['deleteuser'])
    def delete_user(message):
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Доступ запрещен")
            return

        logger.info(f"Команда /deleteuser от администратора {user_id}")

        # Если супер-админ - показывает всех пользователей
        # Если обычный админ - показывает только своих пользователей
        if db.is_super_admin(user_id):
            users = db.get_all_users()
        else:
            # Получаем только своих пользователей
            cursor = db.execute("SELECT * FROM users WHERE created_by = ? ORDER BY created_at DESC", (user_id,))
            users = cursor.fetchall()

        if not users:
            if db.is_super_admin(user_id):
                bot.send_message(message.chat.id, "❌ В базе данных нет пользователей для удаления")
            else:
                bot.send_message(message.chat.id, "❌ У вас нет созданных пользователей для удаления")
            return

        buttons = []
        for user in users:
            if len(user) >= 2:
                username = user[1]

                # Для супер-админа показываем кто создал пользователя
                if db.is_super_admin(user_id) and len(user) >= 4:
                    created_by_username = user[3]
                    button_text = f"🗑️ {username} (создал: {created_by_username})"
                else:
                    button_text = f"🗑️ {username}"

                buttons.append([types.InlineKeyboardButton(
                    button_text,
                    callback_data=f'delete_{username}'
                )])

        markup = types.InlineKeyboardMarkup(buttons)

        if db.is_super_admin(user_id):
            bot.send_message(message.chat.id, "Выберите пользователя для удаления:", reply_markup=markup)
        else:
            bot.send_message(message.chat.id, "Выберите пользователя для удаления (только ваши пользователи):",
                             reply_markup=markup)

    @bot.message_handler(commands=['clear'])
    def clear_database(message):
        """Очистка всей базы данных"""
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Доступ запрещен")
            return

        logger.info(f"Команда /clear от администратора {user_id}")

        buttons = [
            [types.InlineKeyboardButton("✅ Создать бэкап и очистить", callback_data='confirm_clear_with_backup')],
            [types.InlineKeyboardButton("⚠️ Очистить без бэкапа", callback_data='confirm_clear_no_backup')],
            [types.InlineKeyboardButton("❌ Отмена", callback_data='cancel_clear')]
        ]

        markup = types.InlineKeyboardMarkup(buttons)
        bot.send_message(
            message.chat.id,
            "⚠️ Вы собираетесь очистить всю базу данных!\n\n"
            "Это действие удалит:\n"
            "• Всех пользователей\n"
            "• Всю статистику трафика\n"
            "• Все сессии\n\n"
            "Выберите действие:",
            reply_markup=markup
        )