import telebot
import logging
import subprocess
import socket
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from telebot import types
from database import db
from vpn_manager import vpn_manager
from utils import format_traffic_stats, get_backup_info_text
from config import Config

logger = logging.getLogger(__name__)


def _extract_forwarded_user(message):
    """
    Возвращает (user_id, username, error_message) из пересланного сообщения.
    Поддерживает как старые поля forward_from, так и новые forward_origin.
    """
    forward_from = getattr(message, 'forward_from', None)
    if forward_from:
        username = f"@{forward_from.username}" if forward_from.username else f"{forward_from.first_name}"
        return forward_from.id, username, None

    # Для новых версий Telegram Bot API
    forward_origin = getattr(message, 'forward_origin', None)
    if forward_origin:
        sender_user = getattr(forward_origin, 'sender_user', None)
        if sender_user:
            username = f"@{sender_user.username}" if sender_user.username else f"{sender_user.first_name}"
            return sender_user.id, username, None

        # Если пользователь скрыл аккаунт при пересылке - ID недоступен
        sender_user_name = getattr(forward_origin, 'sender_user_name', None)
        if sender_user_name:
            return None, None, (
                "❌ У этого пересланного сообщения скрыт ID отправителя.\n\n"
                "Добавление невозможно через пересылку. Используйте:\n"
                "• 📝 Ввести ID вручную\n"
                "• 📇 Выбрать из контакта (если у контакта есть Telegram ID)"
            )

    return None, None, (
        "❌ Не удалось получить отправителя из пересланного сообщения.\n\n"
        "Проверьте, что это именно пересылка, а не скопированный текст."
    )


def _get_server_ip():
    """Пытается определить внешний IP сервера для инструкций."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    try:
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip and not host_ip.startswith("127."):
            return host_ip
    except Exception:
        pass

    return "YOUR_SERVER_IP"


def _message_as_caller(call):
    """Создает message-like объект с from_user = инициатор callback."""
    return SimpleNamespace(
        chat=call.message.chat,
        from_user=call.from_user,
        message_id=call.message.message_id
    )


def setup_callback_handlers(bot):
    """Настройка обработчиков callback запросов"""

    @bot.callback_query_handler(func=lambda call: call.data.startswith('start_'))
    def handle_start_buttons(call):
        user_id = call.from_user.id

        if not db.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        action = call.data.replace('start_', '')

        if action == 'adduser':
            from handlers.user_handlers import user_states
            user_states[user_id] = {'waiting_for_username': True}
            bot.send_message(
                call.message.chat.id,
                'Введите имя пользователя (только латиница, цифры, _ и -):'
            )
            bot.answer_callback_query(call.id, "⚡ Введите имя пользователя")

        elif action == 'listusers':
            from handlers.user_handlers import list_users
            list_users(_message_as_caller(call))
            bot.answer_callback_query(call.id, "⚡ Список пользователей")

        elif action == 'stats':
            from handlers.user_handlers import show_stats
            show_stats(_message_as_caller(call))
            bot.answer_callback_query(call.id, "⚡ Статистика сервера")

        elif action == 'userstats':
            from handlers.user_handlers import user_stats
            user_stats(_message_as_caller(call))
            bot.answer_callback_query(call.id, "⚡ Статистика пользователей")

        elif action == 'activestats':
            from handlers.user_handlers import show_active_stats
            show_active_stats(_message_as_caller(call))
            bot.answer_callback_query(call.id, "⚡ Активные подключения")

        elif action == 'admin':
            if db.is_super_admin(user_id):
                buttons = [
                    [types.InlineKeyboardButton("📊 Статистика", callback_data='admin_stats')],
                    [types.InlineKeyboardButton("🔄 Перезапустить VPN", callback_data='admin_restart')],
                    [types.InlineKeyboardButton("💾 Создать бэкап", callback_data='admin_backup')],
                    [types.InlineKeyboardButton("📋 Список бэкапов", callback_data='admin_backup_list')],
                    [types.InlineKeyboardButton("🛠️ Fix БД из VPN", callback_data='admin_fixdb')],
                    [types.InlineKeyboardButton("♻️ Восстановить БД из бэкапа", callback_data='admin_restore_db')],
                    [types.InlineKeyboardButton("🧹 Очистить БД", callback_data='admin_clear_db')],
                    [types.InlineKeyboardButton("👑 Управление админами", callback_data='admin_manage')]
                ]
            else:
                buttons = [
                    [types.InlineKeyboardButton("📊 Статистика", callback_data='admin_stats')],
                    [types.InlineKeyboardButton("📋 Список бэкапов", callback_data='admin_backup_list')]
                ]

            markup = types.InlineKeyboardMarkup(buttons)
            bot.send_message(call.message.chat.id, "👨‍💻 Панель администратора", reply_markup=markup)
            bot.answer_callback_query(call.id, "⚡ Панель администратора")

        elif action == 'manage_admins':
            if db.is_super_admin(user_id):
                buttons = [
                    [types.InlineKeyboardButton("👥 Список админов", callback_data='admin_list')],
                    [types.InlineKeyboardButton("➕ Добавить админа", callback_data='admin_add')],
                    [types.InlineKeyboardButton("➖ Удалить админа", callback_data='admin_remove')]
                ]
                markup = types.InlineKeyboardMarkup(buttons)
                bot.send_message(call.message.chat.id, "👑 Управление администраторами", reply_markup=markup)
                bot.answer_callback_query(call.id, "⚡ Управление админами")
            else:
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")

        elif action == 'deleteuser':
            from handlers.admin_handlers import show_delete_user_menu
            show_delete_user_menu(_message_as_caller(call), bot)
            bot.answer_callback_query(call.id, "⚡ Удаление пользователя")

        else:
            bot.answer_callback_query(call.id, "❌ Неизвестная кнопка")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('platform_'))
    def handle_platform_selection(call):
        try:
            user_id = call.from_user.id

            if not db.is_admin(user_id):
                bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
                return

            data_parts = call.data.split('_')
            if len(data_parts) < 3:
                bot.answer_callback_query(call.id, "❌ Ошибка формата данных")
                return

            platform = data_parts[1]
            username = '_'.join(data_parts[2:])

            logger.info(f"Выбор платформы {platform} для {username} администратором {user_id}")

            platform_handlers = {
                'ios': send_ios_profile,
                'sswan': send_sswan_profile,
                'android': send_android_profile,
                'macos': send_macos_profile,
                'win': send_windows_profile
            }

            handler = platform_handlers.get(platform)
            if handler:
                handler(bot, call, username)
                bot.answer_callback_query(call.id, f"📤 Отправляем конфиг для {platform}")
            else:
                bot.answer_callback_query(call.id, "❌ Неизвестная платформа")

        except Exception as e:
            logger.error(f"Ошибка обработки callback {call.data}: {str(e)}")
            bot.answer_callback_query(call.id, "❌ Ошибка обработки запроса")

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith('listusers_prev_') or
                          call.data.startswith('listusers_next_') or
                          call.data == 'listusers_refresh'
    )
    def handle_listusers_pagination(call):
        from handlers.user_handlers import list_users_pages, show_list_users_page

        chat_id = call.message.chat.id
        if chat_id not in list_users_pages:
            bot.answer_callback_query(call.id, "⚠️ Данные устарели, откройте список снова")
            return

        if call.data == 'listusers_refresh':
            list_users_pages[chat_id]['users'] = db.get_all_users()
            list_users_pages[chat_id]['page'] = 0
        elif call.data.startswith('listusers_prev_'):
            new_page = int(call.data.replace('listusers_prev_', ''))
            list_users_pages[chat_id]['page'] = max(0, new_page)
        elif call.data.startswith('listusers_next_'):
            new_page = int(call.data.replace('listusers_next_', ''))
            list_users_pages[chat_id]['page'] = max(0, new_page)

        show_list_users_page(
            bot,
            chat_id,
            edit_message_id=call.message.message_id,
            callback_query_id=call.id
        )

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith('userstats_page_') or call.data == 'userstats_refresh'
    )
    def handle_userstats_navigation(call):
        from handlers.user_handlers import user_stats
        user_stats(_message_as_caller(call))
        bot.answer_callback_query(call.id, "🔄 Список обновлен")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('userstats_'))
    def handle_user_stats(call):
        user_id = call.from_user.id

        if not db.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        if call.data.startswith('userstats_page_') or call.data == 'userstats_refresh':
            bot.answer_callback_query(call.id)
            return

        username = call.data.replace('userstats_', '')
        stats = db.get_user_statistics(username)

        if not stats:
            bot.send_message(call.message.chat.id, f"❌ Статистика для '{username}' не найдена")
            bot.answer_callback_query(call.id, "❌ Статистика не найдена")
            return

        stats_text = format_traffic_stats(stats)
        bot.send_message(call.message.chat.id, f"👤 Пользователь: {username}\n\n{stats_text}")
        bot.answer_callback_query(call.id, f"📊 Статистика {username}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('delete_'))
    def handle_user_deletion(call):
        user_id = call.from_user.id

        if not db.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        username = call.data.replace('delete_', '')

        # Проверяем права на удаление
        if not db.is_super_admin(user_id):
            # Обычный админ может удалять только своих пользователей
            user = db.get_user(username)
            if not user or user[2] != user_id:  # created_by
                bot.send_message(call.message.chat.id, f"❌ Вы можете удалять только своих пользователей")
                bot.answer_callback_query(call.id, "❌ Нет прав на удаление")
                return

        bot.answer_callback_query(call.id, "⏳ Начинаем удаление...")
        bot.send_message(call.message.chat.id, f"⏳ Удаляем пользователя '{username}'...")

        # Удаляем из VPN системы
        success, result_msg = vpn_manager.delete_user(username)

        if not success:
            bot.send_message(call.message.chat.id, f"❌ Ошибка удаления VPN пользователя: {result_msg}")
            return

        # Удаляем из БД (с автоматическим созданием бэкапа)
        if db.delete_user(username):
            bot.send_message(call.message.chat.id, f"✅ Пользователь '{username}' полностью удален из системы")
            logger.info(f"Пользователь {username} удален администратором {user_id}")
        else:
            bot.send_message(call.message.chat.id, f"⚠️ VPN пользователь удален, но ошибка удаления из БД")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('admin_'))
    def handle_admin_actions(call):
        user_id = call.from_user.id

        if not db.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        action = call.data

        if action == 'admin_stats':
            from handlers.user_handlers import show_stats
            show_stats(_message_as_caller(call))
            bot.answer_callback_query(call.id, "📊 Статистика обновлена")

        elif action == 'admin_restart':
            if not db.is_super_admin(user_id):
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")
                return

            # Сначала подтверждаем callback, затем планируем reboot,
            # чтобы Telegram не переотправлял тот же callback после рестарта.
            bot.answer_callback_query(call.id, "🔄 Перезагрузка запланирована")
            bot.send_message(call.message.chat.id, "🔄 Сервер будет перезагружен через 5 секунд...")
            try:
                subprocess.Popen(
                    ['sh', '-c', 'sleep 5 && reboot'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
            except Exception as e:
                bot.send_message(call.message.chat.id, f"❌ Неожиданная ошибка: {str(e)}")

        elif action == 'admin_backup':
            if not db.is_super_admin(user_id):
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")
                return

            bot.send_message(call.message.chat.id, "💾 Создание резервной копии...")
            backup_file = db.create_full_backup("manual_from_panel")

            if backup_file:
                try:
                    with open(backup_file, 'rb') as f:
                        bot.send_document(call.message.chat.id, f, caption="💾 Резервная копия БД")
                    bot.send_message(call.message.chat.id, "✅ Бэкап создан успешно")
                except Exception as e:
                    bot.send_message(call.message.chat.id, f"✅ Бэкап создан, но ошибка отправки: {str(e)}")
            else:
                bot.send_message(call.message.chat.id, "❌ Ошибка создания бэкапа")
            bot.answer_callback_query(call.id, "💾 Бэкап создан")

        elif action == 'admin_backup_list':
            backup_info = db.get_backup_info()
            backup_text = get_backup_info_text(backup_info)
            bot.send_message(call.message.chat.id, backup_text)
            bot.answer_callback_query(call.id, "📋 Список бэкапов")

        elif action == 'admin_fixdb':
            if not db.is_super_admin(user_id):
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")
                return

            markup = types.InlineKeyboardMarkup([
                [types.InlineKeyboardButton("✅ Запустить fix_database", callback_data='admin_fixdb_confirm')],
                [types.InlineKeyboardButton("❌ Отмена", callback_data='admin_fixdb_cancel')]
            ])
            bot.send_message(
                call.message.chat.id,
                "⚠️ Будет запущен скрипт восстановления пользователей из VPN-конфигов.\nПродолжить?",
                reply_markup=markup
            )
            bot.answer_callback_query(call.id, "🛠️ Подтвердите запуск")

        elif action == 'admin_fixdb_confirm':
            if not db.is_super_admin(user_id):
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")
                return

            bot.send_message(call.message.chat.id, "🛠️ Запускаю fix_database.py...")
            script_path = Path(__file__).resolve().parent.parent / "fix_database.py"
            try:
                result = subprocess.run(
                    [sys.executable, str(script_path)],
                    capture_output=True,
                    text=True,
                    timeout=180
                )
                if result.returncode == 0:
                    bot.send_message(call.message.chat.id, "✅ fix_database выполнен успешно")
                else:
                    error_text = (result.stderr or result.stdout or "unknown error")[-1500:]
                    bot.send_message(call.message.chat.id, f"❌ fix_database завершился с ошибкой:\n{error_text}")
            except Exception as e:
                bot.send_message(call.message.chat.id, f"❌ Ошибка запуска fix_database: {e}")
            bot.answer_callback_query(call.id, "🛠️ Выполнено")

        elif action == 'admin_fixdb_cancel':
            bot.send_message(call.message.chat.id, "❌ Запуск fix_database отменен")
            bot.answer_callback_query(call.id, "❌ Отменено")

        elif action == 'admin_restore_db':
            if not db.is_super_admin(user_id):
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")
                return

            backup_info = db.get_backup_info()
            db_backups = backup_info.get("db_backups", [])
            if not db_backups:
                db_backups = [b for b in backup_info.get("backups", []) if b.get("path", "").endswith(".db")]
            if not db_backups:
                bot.send_message(call.message.chat.id, "❌ Не найдено .db бэкапов для восстановления")
                bot.answer_callback_query(call.id, "❌ Нет бэкапов")
                return

            latest_backup = db_backups[0]
            latest_name = latest_backup.get("name", "unknown")
            latest_path = latest_backup.get("path", "")

            markup = types.InlineKeyboardMarkup([
                [types.InlineKeyboardButton("✅ Восстановить", callback_data='admin_restore_latest_confirm')],
                [types.InlineKeyboardButton("❌ Отмена", callback_data='admin_restore_latest_cancel')]
            ])
            bot.send_message(
                call.message.chat.id,
                f"⚠️ Восстановить БД из последнего бэкапа?\n\n"
                f"Файл: {latest_name}\n"
                f"Путь: {latest_path}",
                reply_markup=markup
            )
            bot.answer_callback_query(call.id, "♻️ Подтвердите восстановление")

        elif action == 'admin_restore_latest_confirm':
            if not db.is_super_admin(user_id):
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")
                return

            backup_info = db.get_backup_info()
            db_backups = backup_info.get("db_backups", [])
            if not db_backups:
                db_backups = [b for b in backup_info.get("backups", []) if b.get("path", "").endswith(".db")]
            if not db_backups:
                bot.send_message(call.message.chat.id, "❌ Не найдено .db бэкапов для восстановления")
                bot.answer_callback_query(call.id, "❌ Нет бэкапов")
                return

            latest_path = db_backups[0]["path"]
            ok, msg = db.restore_from_backup_file(latest_path)
            if ok:
                bot.send_message(call.message.chat.id, f"✅ {msg}\nИсточник: {latest_path}")
            else:
                bot.send_message(call.message.chat.id, f"❌ {msg}")
            bot.answer_callback_query(call.id, "♻️ Готово")

        elif action == 'admin_restore_latest_cancel':
            bot.send_message(call.message.chat.id, "❌ Восстановление БД отменено")
            bot.answer_callback_query(call.id, "❌ Отменено")

        elif action == 'admin_clear_db':
            from handlers.admin_handlers import clear_database
            clear_database(_message_as_caller(call), bot)
            bot.answer_callback_query(call.id, "🧹 Подтвердите очистку")

        elif action == 'admin_manage':
            if db.is_super_admin(user_id):
                from handlers.admin_handlers import manage_admins
                manage_admins(_message_as_caller(call))
                bot.answer_callback_query(call.id, "👑 Управление админами")
            else:
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")

        elif action == 'admin_list':
            if db.is_super_admin(user_id):
                admins = db.get_all_admins()
                if not admins:
                    bot.send_message(call.message.chat.id, "📭 Нет администраторов в базе данных")
                else:
                    admin_list = "👥 Список администраторов:\n\n"
                    for admin in admins:
                        admin_id, username, added_at, added_by_name = admin
                        role = "👑 Супер-админ" if admin_id == Config.SUPER_ADMIN_ID else "👨‍💻 Админ"
                        admin_list += f"• {role}: {username} (ID: {admin_id})\n"
                        admin_list += f"  Добавлен: {added_at} by {added_by_name}\n\n"

                    bot.send_message(call.message.chat.id, admin_list)
                bot.answer_callback_query(call.id, "👥 Список админов")
            else:
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")

        elif action == 'admin_add':
            if db.is_super_admin(user_id):
                buttons = [
                    [types.InlineKeyboardButton("📝 Ввести ID вручную", callback_data='add_manual')],
                    [types.InlineKeyboardButton("📇 Выбрать из контакта", callback_data='add_contact')],
                    [types.InlineKeyboardButton("❌ Отмена", callback_data='add_cancel')]
                ]
                markup = types.InlineKeyboardMarkup(buttons)
                bot.send_message(
                    call.message.chat.id,
                    "Выберите способ добавления администратора:",
                    reply_markup=markup
                )
                bot.answer_callback_query(call.id, "➕ Добавление админа")
            else:
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")

        elif action == 'admin_remove':
            if db.is_super_admin(user_id):
                admins = db.get_all_admins()
                admins_to_remove = [admin for admin in admins if admin[0] != Config.SUPER_ADMIN_ID]

                if not admins_to_remove:
                    bot.send_message(call.message.chat.id, "❌ Нет администраторов для удаления")
                    bot.answer_callback_query(call.id, "❌ Нет админов для удаления")
                    return

                buttons = []
                for admin in admins_to_remove:
                    admin_id, username, added_at, added_by_name = admin
                    buttons.append([types.InlineKeyboardButton(
                        f"🗑️ {username} (ID: {admin_id})",
                        callback_data=f'remove_admin_{admin_id}'
                    )])

                markup = types.InlineKeyboardMarkup(buttons)
                bot.send_message(call.message.chat.id, "Выберите администратора для удаления:", reply_markup=markup)
                bot.answer_callback_query(call.id, "➖ Удаление админа")
            else:
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")

    @bot.callback_query_handler(
        func=lambda call: call.data in ['confirm_clear_with_backup', 'confirm_clear_no_backup', 'cancel_clear'])
    def handle_clear_confirmation(call):
        user_id = call.from_user.id

        if not db.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        if call.data == 'cancel_clear':
            bot.send_message(call.message.chat.id, "❌ Очистка отменена")
            bot.answer_callback_query(call.id, "❌ Отменено")
            return

        # Создаем бэкап перед очисткой если выбрано
        if call.data == 'confirm_clear_with_backup':
            bot.send_message(call.message.chat.id, "💾 Создаем резервную копию перед очисткой...")
            backup_file = db.create_full_backup("before_clear_all")
            if backup_file:
                bot.send_message(call.message.chat.id, f"✅ Резервная копия создана: {os.path.basename(backup_file)}")
            else:
                bot.send_message(call.message.chat.id, "⚠️ Не удалось создать бэкап, продолжаем без него")

        # Выполняем очистку
        bot.send_message(call.message.chat.id, "🧹 Очищаем базу данных...")

        if db.clear_all_users():
            bot.send_message(call.message.chat.id, "✅ База данных очищена")
            logger.warning(f"БД очищена администратором {user_id}")
            bot.answer_callback_query(call.id, "✅ БД очищена")
        else:
            bot.send_message(call.message.chat.id, "❌ Ошибка очистки базы данных")
            bot.answer_callback_query(call.id, "❌ Ошибка очистки")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('remove_admin_'))
    def handle_remove_admin(call):
        user_id = call.from_user.id

        if not db.is_super_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        try:
            admin_id_to_remove = int(call.data.replace('remove_admin_', ''))

            if admin_id_to_remove == Config.SUPER_ADMIN_ID:
                bot.send_message(call.message.chat.id, "❌ Нельзя удалить супер-администратора")
                bot.answer_callback_query(call.id, "❌ Нельзя удалить супер-админа")
                return

            if db.delete_admin(admin_id_to_remove):
                bot.send_message(call.message.chat.id, f"✅ Администратор (ID: {admin_id_to_remove}) удален")
            else:
                bot.send_message(call.message.chat.id, f"❌ Ошибка удаления администратора")

            bot.answer_callback_query(call.id, "✅ Админ удален")

        except ValueError:
            bot.answer_callback_query(call.id, "❌ Ошибка формата ID")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('add_'))
    def handle_add_methods(call):
        user_id = call.from_user.id

        if not db.is_super_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        method = call.data

        if method == 'add_manual':
            msg = bot.send_message(call.message.chat.id, "Введите ID пользователя для добавления в администраторы:")
            bot.register_next_step_handler(msg, process_add_admin_manual, bot)
            bot.answer_callback_query(call.id, "📝 Ввод ID")

        elif method == 'add_forward':
            msg = bot.send_message(
                call.message.chat.id,
                "Перешлите любое сообщение от пользователя, которого хотите добавить в администраторы.\n\n"
                "Если Telegram скрывает ID при пересылке, используйте добавление по ID или через контакт."
            )
            bot.register_next_step_handler(msg, process_add_admin_forward, bot)
            bot.answer_callback_query(call.id, "🔗 Перешлите сообщение")

        elif method == 'add_contact':
            keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            if hasattr(types, "KeyboardButtonRequestUsers"):
                request = types.KeyboardButtonRequestUsers(
                    request_id=1,
                    user_is_bot=False,
                    max_quantity=1
                )
                keyboard.add(types.KeyboardButton("📇 Выбрать пользователя", request_users=request))
            else:
                keyboard.add(types.KeyboardButton("📱 Отправить контакт", request_contact=True))
            keyboard.add(types.KeyboardButton("❌ Отмена"))
            msg = bot.send_message(
                call.message.chat.id,
                "Выберите пользователя из списка Telegram контактов.\n\n"
                "Если список не откроется (старая версия Telegram API), отправьте контакт вручную.",
                reply_markup=keyboard
            )
            bot.register_next_step_handler(msg, process_add_admin_contact, bot)
            bot.answer_callback_query(call.id, "📇 Отправьте контакт")

        elif method == 'add_cancel':
            bot.send_message(call.message.chat.id, "❌ Добавление админа отменено")
            bot.answer_callback_query(call.id, "❌ Отменено")


def process_add_admin_manual(message, bot):
    if message.text and message.text.startswith('/'):
        bot.send_message(message.chat.id, "❌ Добавление админа отменено")
        return

    try:
        new_admin_id = int(message.text.strip())

        try:
            user_info = bot.get_chat(new_admin_id)
            username = f"@{user_info.username}" if user_info.username else f"{user_info.first_name}"
        except:
            username = f"Пользователь {new_admin_id}"

        if db.add_admin(new_admin_id, username, Config.SUPER_ADMIN_ID):
            bot.send_message(message.chat.id,
                             f"✅ Пользователь {username} (ID: {new_admin_id}) добавлен в администраторы")
        else:
            bot.send_message(message.chat.id, f"❌ Не удалось добавить пользователя в администраторы")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверный формат ID. Введите числовой ID.")


def process_add_admin_forward(message, bot):
    if message.text and message.text.startswith('/cancel'):
        bot.send_message(message.chat.id, "❌ Добавление админа отменено")
        return

    user_id, username, error_message = _extract_forwarded_user(message)
    if not user_id:
        bot.send_message(message.chat.id, error_message)
        return

    if db.add_admin(user_id, username, Config.SUPER_ADMIN_ID):
        bot.send_message(message.chat.id, f"✅ Пользователь {username} (ID: {user_id}) добавлен в администраторы")
    else:
        bot.send_message(message.chat.id, f"❌ Не удалось добавить пользователя в администраторы")


def process_add_admin_contact(message, bot):
    if message.text and (message.text.startswith('/cancel') or message.text == "❌ Отмена"):
        bot.send_message(message.chat.id, "❌ Добавление админа отменено", reply_markup=types.ReplyKeyboardRemove())
        return

    contact_user_id = None
    username = None

    users_shared = getattr(message, 'users_shared', None)
    if users_shared and getattr(users_shared, 'users', None):
        selected_user = users_shared.users[0]
        contact_user_id = getattr(selected_user, 'user_id', None) or getattr(selected_user, 'id', None)
        if contact_user_id:
            try:
                user_info = bot.get_chat(contact_user_id)
                username = f"@{user_info.username}" if user_info.username else f"{user_info.first_name}"
            except Exception:
                username = f"Пользователь {contact_user_id}"

    contact = getattr(message, 'contact', None)
    if contact and not contact_user_id:
        contact_user_id = getattr(contact, 'user_id', None)
        first_name = getattr(contact, 'first_name', '') or ''
        last_name = getattr(contact, 'last_name', '') or ''
        full_name = (first_name + (' ' + last_name if last_name else '')).strip()
        username = full_name if full_name else f"Пользователь {contact_user_id}"

    if not contact_user_id:
        bot.send_message(
            message.chat.id,
            "❌ Пользователь не выбран. Используйте выбор из контактов или ввод ID вручную.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    if db.add_admin(contact_user_id, username, Config.SUPER_ADMIN_ID):
        bot.send_message(
            message.chat.id,
            f"✅ Пользователь {username} (ID: {contact_user_id}) добавлен в администраторы",
            reply_markup=types.ReplyKeyboardRemove()
        )
    else:
        bot.send_message(
            message.chat.id,
            f"❌ Не удалось добавить пользователя в администраторы",
            reply_markup=types.ReplyKeyboardRemove()
        )


def send_ios_profile(bot, call, username):
    server_ip = _get_server_ip()
    bot.send_message(call.message.chat.id, f"📱 Отправка профиля для iOS ({username})...")
    bot.send_message(
        call.message.chat.id,
        f"📘 Инструкция для iOS\n\n"
        f"1. 📥 Откройте файл в Telegram.\n"
        f"2. 📤 Нажмите «Поделиться» -> «Сохранить в Файлы».\n"
        f"3. 📂 Откройте приложение «Файлы» -> «Недавние» и запустите сохраненный файл.\n"
        f"4. ⚙️ Нажмите «Установить профиль».\n"
        f"5. 🔐 Перейдите в «Настройки» -> «Загружен профиль», подтвердите установку и введите пароль.\n"
        f"6. ✅ Готово: подключение появится в «Настройки» -> «VPN».\n\n"
        f"🌐 Сервер: {server_ip}"
    )

    file_path = vpn_manager.get_profile_path(username, 'ios')
    if file_path:
        with open(file_path, 'rb') as file:
            bot.send_document(call.message.chat.id, file, caption="iOS профиль")
    else:
        bot.send_message(call.message.chat.id, f"❌ Файл iOS профиль не найден")


def send_android_profile(bot, call, username):
    server_ip = _get_server_ip()
    bot.send_message(call.message.chat.id, f"🤖 Отправка профиля для Android v11+ ({username})...")
    bot.send_message(
        call.message.chat.id,
        f"📘 Инструкция для Android v11+\n\n"
        f"1. 📥 Скачайте файл из сообщения ниже.\n"
        f"2. 📂 Перейдите в «Файлы» -> Telegram и откройте скачанный сертификат.\n"
        f"3. 🔐 Нажмите «Установить сертификат».\n"
        f"4. ⚙️ Перейдите в «Настройки» -> «Подключения» -> «VPN» (или аналогичный раздел) и нажмите «+».\n"
        f"5. 📝 Введите имя VPN-профиля.\n"
        f"6. 🔽 Выберите тип «IKEv2/IPSec RSA».\n"
        f"7. 🌐 Введите адрес сервера: {server_ip}\n"
        f"8. 🆔 Если есть поле идентификатора IPSec, укажите любое значение.\n"
        f"9. 👤 В «Сертификат пользователя» выберите импортированный сертификат и сохраните.\n"
        f"10. 🛡️ В «Сертификат ЦС IPSec» выберите тот же импортированный сертификат.\n"
        f"11. ✅ Сохраните профиль и подключитесь."
    )

    file_path = vpn_manager.get_profile_path(username, 'android')
    if file_path:
        with open(file_path, 'rb') as file:
            bot.send_document(call.message.chat.id, file, caption="Android профиль")
    else:
        bot.send_message(call.message.chat.id, f"❌ Файл Android профиль не найден")


def send_sswan_profile(bot, call, username):
    server_ip = _get_server_ip()
    bot.send_message(call.message.chat.id, f"🤖 Отправка профиля для StrongSwan ({username})...")
    bot.send_message(
        call.message.chat.id,
        f"📘 Инструкция для Android до 11 (StrongSwan)\n\n"
        f"1. 📥 Сохраните файл в «Загрузки».\n"
        f"2. 🛠️ Установите StrongSwan VPN Client (Google Play / F-Droid / strongSwan download server).\n"
        f"3. ▶️ Запустите приложение StrongSwan VPN Client.\n"
        f"4. ⋮ Нажмите «More options» (справа сверху) -> «Import VPN profile».\n"
        f"5. 📄 Выберите сохраненный файл `.sswan`.\n"
        f"6. 🔐 В окне импорта нажмите «IMPORT CERTIFICATE FROM VPN PROFILE» и следуйте шагам.\n"
        f"7. 📜 На экране «Choose certificate» выберите новый сертификат и нажмите «Выбрать».\n"
        f"8. ⬇️ Нажмите «IMPORT».\n"
        f"9. ✅ Нажмите на созданный VPN-профиль для подключения.\n\n"
        f"🌐 Сервер: {server_ip}"
    )

    file_path = vpn_manager.get_profile_path(username, 'sswan')
    if file_path:
        with open(file_path, 'rb') as file:
            bot.send_document(call.message.chat.id, file, caption="StrongSwan профиль")
    else:
        bot.send_message(call.message.chat.id, f"❌ Файл StrongSwan профиль не найден")


def send_macos_profile(bot, call, username):
    server_ip = _get_server_ip()
    bot.send_message(call.message.chat.id, f"💻 Отправка профиля для MacOS ({username})...")
    bot.send_message(
        call.message.chat.id,
        f"📘 Инструкция для macOS\n\n"
        f"1. 📥 Сохраните файл из сообщения ниже.\n"
        f"2. 🖱️ Запустите файл двойным кликом, дождитесь сообщения «Профиль загружен» и нажмите OK.\n"
        f"3. ⚙️ Перейдите в «Настройки» -> «Профиль загружен».\n"
        f"4. 🔁 Дважды нажмите на добавленный профиль и выберите «Установить».\n"
        f"5. 🔐 Введите пароль пользователя macOS для разблокировки.\n"
        f"6. ⏳ Дождитесь завершения установки.\n"
        f"7. ✅ Подключение появится в «Настройки» -> «VPN», включите его.\n\n"
        f"🌐 Сервер: {server_ip}"
    )

    file_path = vpn_manager.get_profile_path(username, 'macos')
    if file_path:
        with open(file_path, 'rb') as file:
            bot.send_document(call.message.chat.id, file, caption="MacOS профиль")
    else:
        bot.send_message(call.message.chat.id, f"❌ Файл MacOS профиль не найден")


def send_windows_profile(bot, call, username):
    server_ip = _get_server_ip()
    bot.send_message(call.message.chat.id, f"🪟 Отправка профиля для Windows ({username})...")
    bot.send_message(
        call.message.chat.id,
        f"📘 Инструкция для Windows\n\n"
        f"1. 📥 Сохраните файлы `.p12`, `ikev2_config_import.cmd` и "
        f"`Enable_Stronger_Ciphers_for_IKEv2_on_Windows.reg` на компьютер.\n"
        f"2. 📂 Поместите `ikev2_config_import.cmd` в одну папку с `.p12`.\n"
        f"3. 🖱️ Нажмите правой кнопкой по `ikev2_config_import.cmd` -> «Свойства», "
        f"нажмите «Разблокировать» внизу и OK.\n"
        f"4. 🛡️ Снова нажмите правой кнопкой по `ikev2_config_import.cmd` и запустите от имени администратора.\n"
        f"5. 🧭 Следуйте инструкциям мастера.\n"
        f"6. 🌐 При запросе IP введите: {server_ip}\n"
        f"7. ⚡ Запустите `Enable_Stronger_Ciphers_for_IKEv2_on_Windows.reg` и нажмите «Выполнить»."
    )

    # Основной файл P12
    file_path = vpn_manager.get_profile_path(username, 'win')
    if file_path:
        with open(file_path, 'rb') as file:
            bot.send_document(call.message.chat.id, file, caption="Windows сертификат")
    else:
        bot.send_message(call.message.chat.id, f"❌ Файл Windows сертификат не найден")

    # Дополнительные файлы для упрощенного импорта на Windows
    helper_files = [
        (Path("/root/ikev2_config_import.cmd"), "ikev2_config_import.cmd"),
        (Path("/root/Enable_Stronger_Ciphers_for_IKEv2_on_Windows.reg"),
         "Enable_Stronger_Ciphers_for_IKEv2_on_Windows.reg")
    ]
    for helper_path, caption in helper_files:
        if helper_path.exists():
            with open(helper_path, 'rb') as file:
                bot.send_document(call.message.chat.id, file, caption=caption)
        else:
            logger.warning(f"Windows helper file not found: {helper_path}")
