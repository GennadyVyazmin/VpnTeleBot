import os
import telebot
import logging
import subprocess
import threading
import sys
from pathlib import Path
from telebot import types
from database import db
from vpn_manager import vpn_manager
from utils import format_traffic_stats, get_backup_info_text, format_bytes
from config import Config
from traffic_monitor import traffic_monitor
from datetime import datetime

logger = logging.getLogger(__name__)


def _extract_forwarded_user(message):
    """Извлекает пользователя из пересланного сообщения (старый и новый формат Telegram)."""
    forward_from = getattr(message, 'forward_from', None)
    if forward_from:
        username = f"@{forward_from.username}" if forward_from.username else f"{forward_from.first_name}"
        return forward_from.id, username, None

    forward_origin = getattr(message, 'forward_origin', None)
    if forward_origin:
        sender_user = getattr(forward_origin, 'sender_user', None)
        if sender_user:
            username = f"@{sender_user.username}" if sender_user.username else f"{sender_user.first_name}"
            return sender_user.id, username, None

        sender_user_name = getattr(forward_origin, 'sender_user_name', None)
        if sender_user_name:
            return None, None, (
                "❌ У пересланного сообщения скрыт ID отправителя.\n\n"
                "Используйте добавление по ID вручную или выбор из пользователей бота."
            )

    return None, None, "❌ Не удалось получить данные отправителя из пересланного сообщения."


def _clear_admin_add_state(user_id):
    """Очищает состояния добавления админа."""
    from handlers.user_handlers import user_states
    if user_id in user_states:
        state = user_states[user_id]
        state.pop('waiting_for_admin_id', None)
        state.pop('waiting_for_admin_forward', None)
        state.pop('waiting_for_admin_contact', None)
        if not state:
            del user_states[user_id]


def _get_latest_db_backup_file():
    Config.ensure_directories()
    backup_files = sorted(Config.BACKUP_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return backup_files[0] if backup_files else None


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
            # Выводим список пользователей напрямую
            users = db.get_all_users()
            if not users:
                bot.send_message(call.message.chat.id, "📭 В базе данных нет пользователей")
                bot.answer_callback_query(call.id, "📭 Нет пользователей")
                return

            # Используем функцию из user_handlers
            from handlers.user_handlers import list_users_pages, show_list_users_page
            chat_id = call.message.chat.id
            list_users_pages[chat_id] = {
                'users': users,
                'page': 0,
                'page_size': 15
            }
            show_list_users_page(bot, chat_id)
            bot.answer_callback_query(call.id, "⚡ Список пользователей")

        elif action == 'stats':
            # Выводим статистику напрямую
            total_users = db.get_user_count()
            active_users = db.get_active_users_count()
            traffic_data = traffic_monitor.parse_ipsec_status()

            stats_text = f"""📊 Статистика VPN сервера

👥 Всего пользователей: {total_users}
🟢 Активных в БД: {active_users}
🔌 Активных в ipsec: {len(traffic_data)}

⏱️  Мониторинг: каждые {Config.STATS_UPDATE_INTERVAL} сек
📁 Директория конфигов: {Config.VPN_PROFILES_PATH}
🕒 Время сервера: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""

            if traffic_data:
                stats_text += "\n\n🔍 Активные подключения:"
                for username, info in list(traffic_data.items())[:5]:
                    traffic_mb = (info['absolute_sent'] + info['absolute_received']) / (1024 * 1024)
                    stats_text += f"\n• {username}: {traffic_mb:.1f} MB"

            bot.send_message(call.message.chat.id, stats_text)
            bot.answer_callback_query(call.id, "⚡ Статистика сервера")

        elif action == 'userstats':
            # Создаем список пользователей для выбора статистики
            users = db.get_all_users()
            if not users:
                bot.send_message(call.message.chat.id, "📭 В базе данных нет пользователей")
                bot.answer_callback_query(call.id, "📭 Нет пользователей")
                return

            # Показываем первую страницу
            buttons_per_page = 10
            total_pages = (len(users) + buttons_per_page - 1) // buttons_per_page
            page = 0
            start_idx = page * buttons_per_page
            end_idx = min(start_idx + buttons_per_page, len(users))

            buttons = []
            for i in range(start_idx, end_idx):
                user = users[i]
                if len(user) >= 2:
                    username = user[1]
                    is_active = user[9] if len(user) > 9 else 0
                    status = "🟢" if is_active else "⚪"
                    buttons.append([types.InlineKeyboardButton(
                        f"{status} {username}",
                        callback_data=f'userstats_{username}'
                    )])

            if total_pages > 1:
                nav_buttons = []
                if page > 0:
                    nav_buttons.append(
                        types.InlineKeyboardButton("⬅️ Назад", callback_data=f'userstats_page_{page - 1}'))
                if page < total_pages - 1:
                    nav_buttons.append(
                        types.InlineKeyboardButton("Вперед ➡️", callback_data=f'userstats_page_{page + 1}'))

                if nav_buttons:
                    buttons.append(nav_buttons)

            buttons.append([types.InlineKeyboardButton("🔄 Обновить список", callback_data='userstats_refresh')])
            markup = types.InlineKeyboardMarkup(buttons)
            bot.send_message(
                call.message.chat.id,
                f"Выберите пользователя для просмотра статистики (стр. {page + 1}/{total_pages}):",
                reply_markup=markup
            )
            bot.answer_callback_query(call.id, "⚡ Статистика пользователей")

        elif action == 'activestats':
            # Выводим активные подключения напрямую
            traffic_data = traffic_monitor.parse_ipsec_status()

            if not traffic_data:
                bot.send_message(call.message.chat.id, "📭 Нет активных подключений")
                bot.answer_callback_query(call.id, "📭 Нет активных подключений")
                return

            stats_text = "🟢 Активные подключения (из ipsec):\n\n"

            for username, data in traffic_data.items():
                total_traffic = (data['absolute_sent'] + data['absolute_received']) / (1024 ** 2)  # MB
                stats_text += f"👤 {username}\n"
                stats_text += f"   IP: {data['client_ip']}\n"
                stats_text += f"   ID: {data['connection_id']}\n"
                stats_text += f"   Абсолютные значения:\n"
                stats_text += f"     • Отправлено: {data['absolute_sent'] / 1024 / 1024:.1f} MB\n"
                stats_text += f"     • Получено: {data['absolute_received'] / 1024 / 1024:.1f} MB\n"
                stats_text += f"   Всего: {total_traffic:.2f} MB\n\n"

            stats_text += f"Всего активных: {len(traffic_data)}"

            if len(stats_text) > 4000:
                parts = [stats_text[i:i + 4000] for i in range(0, len(stats_text), 4000)]
                for i, part in enumerate(parts):
                    if i == 0:
                        bot.send_message(call.message.chat.id, part)
                    else:
                        bot.send_message(call.message.chat.id, f"`{part}`", parse_mode='Markdown')
            else:
                bot.send_message(call.message.chat.id, stats_text)
            bot.answer_callback_query(call.id, "⚡ Активные подключения")

        elif action == 'admin':
            # Открываем панель администратора
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
                    [types.InlineKeyboardButton("🔄 Перезапустить VPN", callback_data='admin_restart')],
                    [types.InlineKeyboardButton("💾 Создать бэкап", callback_data='admin_backup')],
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
            # Показываем список пользователей для удаления
            if db.is_super_admin(user_id):
                users = db.get_all_users()
            else:
                # Получаем только своих пользователей
                users = []
                all_users = db.get_all_users()
                for user in all_users:
                    if len(user) >= 3 and user[2] == user_id:  # created_by
                        users.append(user)

            if not users:
                if db.is_super_admin(user_id):
                    bot.send_message(call.message.chat.id, "❌ В базе данных нет пользователей для удаления")
                else:
                    bot.send_message(call.message.chat.id, "❌ У вас нет созданных пользователей для удаления")
                bot.answer_callback_query(call.id, "❌ Нет пользователей")
                return

            buttons = []
            for user in users:
                if len(user) >= 2:
                    username = user[1]
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
                bot.send_message(call.message.chat.id, "Выберите пользователя для удаления:", reply_markup=markup)
            else:
                bot.send_message(call.message.chat.id, "Выберите пользователя для удаления (только ваши пользователи):",
                                 reply_markup=markup)
            bot.answer_callback_query(call.id, "⚡ Удаление пользователя")

    # ========== ОБРАБОТЧИКИ ПАГИНАЦИИ ДЛЯ USERSTATS ==========

    @bot.callback_query_handler(func=lambda call: call.data.startswith('userstats_page_'))
    def handle_userstats_pagination(call):
        user_id = call.from_user.id

        if not db.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        try:
            page = int(call.data.replace('userstats_page_', ''))

            # Получаем всех пользователей
            users = db.get_all_users()
            if not users:
                bot.send_message(call.message.chat.id, "📭 В базе данных нет пользователей")
                bot.answer_callback_query(call.id, "📭 Нет пользователей")
                return

            # Создаем пагинированный список кнопок
            buttons_per_page = 10
            total_pages = (len(users) + buttons_per_page - 1) // buttons_per_page

            # Проверяем валидность страницы
            page = max(0, min(page, total_pages - 1))

            start_idx = page * buttons_per_page
            end_idx = min(start_idx + buttons_per_page, len(users))

            # Создаем кнопки пользователей
            buttons = []
            for i in range(start_idx, end_idx):
                user = users[i]
                if len(user) >= 2:
                    username = user[1]
                    is_active = user[9] if len(user) > 9 else 0
                    status = "🟢" if is_active else "⚪"
                    buttons.append([types.InlineKeyboardButton(
                        f"{status} {username}",
                        callback_data=f'userstats_{username}'
                    )])

            # Добавляем кнопки навигации если есть больше одной страницы
            if total_pages > 1:
                nav_buttons = []
                if page > 0:
                    nav_buttons.append(
                        types.InlineKeyboardButton("⬅️ Назад", callback_data=f'userstats_page_{page - 1}'))
                if page < total_pages - 1:
                    nav_buttons.append(
                        types.InlineKeyboardButton("Вперед ➡️", callback_data=f'userstats_page_{page + 1}'))

                if nav_buttons:
                    buttons.append(nav_buttons)

            # Кнопка обновления списка
            buttons.append([types.InlineKeyboardButton("🔄 Обновить список", callback_data='userstats_refresh')])

            markup = types.InlineKeyboardMarkup(buttons)

            try:
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=f"Выберите пользователя для просмотра статистики (стр. {page + 1}/{total_pages}):",
                    reply_markup=markup
                )
            except Exception as e:
                # Если сообщение нельзя редактировать, отправляем новое
                bot.send_message(
                    call.message.chat.id,
                    f"Выберите пользователя для просмотра статистики (стр. {page + 1}/{total_pages}):",
                    reply_markup=markup
                )

            bot.answer_callback_query(call.id, f"Страница {page + 1}")

        except Exception as e:
            logger.error(f"Ошибка пагинации userstats: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda call: call.data == 'userstats_refresh')
    def handle_userstats_refresh(call):
        user_id = call.from_user.id

        if not db.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        # Обновляем список пользователей
        users = db.get_all_users()
        if not users:
            bot.send_message(call.message.chat.id, "📭 В базе данных нет пользователей")
            bot.answer_callback_query(call.id, "📭 Нет пользователей")
            return

        # Показываем первую страницу
        buttons_per_page = 10
        total_pages = (len(users) + buttons_per_page - 1) // buttons_per_page
        page = 0
        start_idx = page * buttons_per_page
        end_idx = min(start_idx + buttons_per_page, len(users))

        buttons = []
        for i in range(start_idx, end_idx):
            user = users[i]
            if len(user) >= 2:
                username = user[1]
                is_active = user[9] if len(user) > 9 else 0
                status = "🟢" if is_active else "⚪"
                buttons.append([types.InlineKeyboardButton(
                    f"{status} {username}",
                    callback_data=f'userstats_{username}'
                )])

        if total_pages > 1:
            nav_buttons = []
            if page > 0:
                nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f'userstats_page_{page - 1}'))
            if page < total_pages - 1:
                nav_buttons.append(types.InlineKeyboardButton("Вперед ➡️", callback_data=f'userstats_page_{page + 1}'))

            if nav_buttons:
                buttons.append(nav_buttons)

        buttons.append([types.InlineKeyboardButton("🔄 Обновить список", callback_data='userstats_refresh')])
        markup = types.InlineKeyboardMarkup(buttons)

        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"Выберите пользователя для просмотра статистики (стр. {page + 1}/{total_pages}):",
                reply_markup=markup
            )
        except Exception as e:
            bot.send_message(
                call.message.chat.id,
                f"Выберите пользователя для просмотра статистики (стр. {page + 1}/{total_pages}):",
                reply_markup=markup
            )

        bot.answer_callback_query(call.id, "🔄 Список обновлен")

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

            if platform == 'ios':
                send_ios_profile(bot, call, username)
            elif platform == 'sswan':
                send_sswan_profile(bot, call, username)
            elif platform == 'android':
                send_android_profile(bot, call, username)
            elif platform == 'macos':
                send_macos_profile(bot, call, username)
            elif platform == 'win':
                send_windows_profile(bot, call, username)
            else:
                bot.answer_callback_query(call.id, "❌ Неизвестная платформа")
                return

            bot.answer_callback_query(call.id, f"📤 Отправляем конфиг для {platform}")

        except Exception as e:
            logger.error(f"Ошибка обработки callback {call.data}: {str(e)}")
            bot.answer_callback_query(call.id, "❌ Ошибка обработки запроса")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('userstats_'))
    def handle_user_stats(call):
        user_id = call.from_user.id

        if not db.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        # Проверяем, не является ли это пагинацией
        if call.data.startswith('userstats_page_'):
            # Уже обработано в другом обработчике
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
            total_users = db.get_user_count()
            active_users = db.get_active_users_count()
            traffic_data = traffic_monitor.parse_ipsec_status()

            stats_text = f"""📊 Статистика VPN сервера

👥 Всего пользователей: {total_users}
🟢 Активных в БД: {active_users}
🔌 Активных в ipsec: {len(traffic_data)}

⏱️  Мониторинг: каждые {Config.STATS_UPDATE_INTERVAL} сек
📁 Директория конфигов: {Config.VPN_PROFILES_PATH}
🕒 Время сервера: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""

            if traffic_data:
                stats_text += "\n\n🔍 Активные подключения:"
                for username, info in list(traffic_data.items())[:5]:
                    traffic_mb = (info['absolute_sent'] + info['absolute_received']) / (1024 * 1024)
                    stats_text += f"\n• {username}: {traffic_mb:.1f} MB"

            bot.send_message(call.message.chat.id, stats_text)
            bot.answer_callback_query(call.id, "📊 Статистика обновлена")

        elif action == 'admin_restart':
            # Сначала подтверждаем callback, чтобы Telegram не прислал его повторно после перезапуска.
            bot.answer_callback_query(call.id, "🔄 Reboot")
            bot.send_message(call.message.chat.id, "🔄 Выполняем перезагрузку сервера...")

            def do_reboot():
                try:
                    subprocess.run(['reboot'], check=True)
                except Exception as e:
                    logger.error(f"Ошибка выполнения reboot: {e}")

            # Небольшая задержка дает боту отправить ответы в Telegram до остановки системы.
            threading.Timer(2.0, do_reboot).start()

        elif action == 'admin_backup':
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

            buttons = [
                [types.InlineKeyboardButton("✅ Запустить fix_database.py", callback_data='confirm_fixdb')],
                [types.InlineKeyboardButton("❌ Отмена", callback_data='cancel_fixdb')]
            ]
            markup = types.InlineKeyboardMarkup(buttons)
            bot.send_message(
                call.message.chat.id,
                "⚠️ Запустить восстановление пользователей через fix_database.py?\n"
                "Скрипт добавит пользователей, найденных в VPN конфигурациях/ipsec.",
                reply_markup=markup
            )
            bot.answer_callback_query(call.id, "🛠️ Подтвердите запуск")

        elif action == 'admin_restore_db':
            if not db.is_super_admin(user_id):
                bot.answer_callback_query(call.id, "⛔ Только для супер-админа")
                return

            latest_backup = _get_latest_db_backup_file()
            if not latest_backup:
                bot.send_message(call.message.chat.id, "❌ В папке бэкапов нет .db файлов для восстановления")
                bot.answer_callback_query(call.id, "❌ Нет бэкапов")
                return

            backup_time = datetime.fromtimestamp(latest_backup.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            buttons = [
                [types.InlineKeyboardButton("✅ Восстановить из последнего бэкапа", callback_data='confirm_restore_db')],
                [types.InlineKeyboardButton("❌ Отмена", callback_data='cancel_restore_db')]
            ]
            markup = types.InlineKeyboardMarkup(buttons)
            bot.send_message(
                call.message.chat.id,
                f"⚠️ Восстановить БД из последнего бэкапа?\n\n"
                f"Файл: {latest_backup.name}\n"
                f"Дата: {backup_time}\n\n"
                f"Текущая БД будет перезаписана.",
                reply_markup=markup
            )
            bot.answer_callback_query(call.id, "♻️ Подтвердите восстановление")

        elif action == 'admin_clear_db':
            buttons = [
                [types.InlineKeyboardButton("✅ Создать бэкап и очистить", callback_data='confirm_clear_with_backup')],
                [types.InlineKeyboardButton("⚠️ Очистить без бэкапа", callback_data='confirm_clear_no_backup')],
                [types.InlineKeyboardButton("❌ Отмена", callback_data='cancel_clear')]
            ]

            markup = types.InlineKeyboardMarkup(buttons)
            bot.send_message(
                call.message.chat.id,
                "⚠️ Вы собираетесь очистить всю базу данных!\n\n"
                "Это действие удалит:\n"
                "• Всех пользователей\n"
                "• Всю статистику трафика\n"
                "• Все сессии\n\n"
                "Выберите действие:",
                reply_markup=markup
            )
            bot.answer_callback_query(call.id, "🧹 Подтвердите очистку")

        elif action == 'admin_manage':
            if db.is_super_admin(user_id):
                buttons = [
                    [types.InlineKeyboardButton("👥 Список админов", callback_data='admin_list')],
                    [types.InlineKeyboardButton("➕ Добавить админа", callback_data='admin_add')],
                    [types.InlineKeyboardButton("➖ Удалить админа", callback_data='admin_remove')]
                ]
                markup = types.InlineKeyboardMarkup(buttons)
                bot.send_message(call.message.chat.id, "👑 Управление администраторами", reply_markup=markup)
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
                    [types.InlineKeyboardButton("🔗 Переслать сообщение", callback_data='add_forward')],
                    [types.InlineKeyboardButton("📇 Из контактов Telegram", callback_data='add_contact')],
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

    @bot.callback_query_handler(
        func=lambda call: call.data in ['confirm_fixdb', 'cancel_fixdb', 'confirm_restore_db', 'cancel_restore_db'])
    def handle_recovery_confirmation(call):
        user_id = call.from_user.id

        if not db.is_super_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Только для супер-админа")
            return

        if call.data in ['cancel_fixdb', 'cancel_restore_db']:
            bot.send_message(call.message.chat.id, "❌ Действие отменено")
            bot.answer_callback_query(call.id, "❌ Отменено")
            return

        if call.data == 'confirm_fixdb':
            bot.send_message(call.message.chat.id, "⏳ Запускаем fix_database.py...")
            try:
                script_path = Path(__file__).resolve().parent.parent / 'fix_database.py'
                result = subprocess.run(
                    [sys.executable, str(script_path)],
                    capture_output=True,
                    text=True,
                    timeout=180
                )
                if result.returncode == 0:
                    output_tail = "\n".join(result.stdout.strip().splitlines()[-12:])
                    bot.send_message(call.message.chat.id, f"✅ fix_database.py выполнен успешно\n\n{output_tail}")
                    bot.answer_callback_query(call.id, "✅ Готово")
                else:
                    error_tail = "\n".join((result.stderr or result.stdout or "").strip().splitlines()[-12:])
                    bot.send_message(call.message.chat.id, f"❌ Ошибка выполнения fix_database.py\n\n{error_tail}")
                    bot.answer_callback_query(call.id, "❌ Ошибка")
            except Exception as e:
                bot.send_message(call.message.chat.id, f"❌ Не удалось запустить fix_database.py: {str(e)}")
                bot.answer_callback_query(call.id, "❌ Ошибка")
            return

        if call.data == 'confirm_restore_db':
            latest_backup = _get_latest_db_backup_file()
            if not latest_backup:
                bot.send_message(call.message.chat.id, "❌ Бэкап для восстановления не найден")
                bot.answer_callback_query(call.id, "❌ Нет бэкапа")
                return

            bot.send_message(call.message.chat.id, f"⏳ Восстанавливаем БД из {latest_backup.name}...")
            success, msg = db.restore_from_backup_file(latest_backup)
            if success:
                bot.send_message(call.message.chat.id, f"✅ {msg}")
                bot.answer_callback_query(call.id, "✅ Восстановлено")
            else:
                bot.send_message(call.message.chat.id, f"❌ {msg}")
                bot.answer_callback_query(call.id, "❌ Ошибка")

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
            from handlers.user_handlers import user_states
            user_states[user_id] = {'waiting_for_admin_id': True}

            bot.send_message(call.message.chat.id, "Введите ID пользователя для добавления в администраторы:")
            bot.answer_callback_query(call.id, "📝 Ввод ID")

        elif method == 'add_forward':
            from handlers.user_handlers import user_states
            user_states[user_id] = {'waiting_for_admin_forward': True}

            bot.send_message(
                call.message.chat.id,
                "Перешлите любое сообщение от пользователя, которого хотите добавить в администраторы.\n\n"
                "Для отмены: /cancel"
            )
            bot.answer_callback_query(call.id, "🔗 Перешлите сообщение")

        elif method == 'add_contact':
            from handlers.user_handlers import user_states
            user_states[user_id] = {'waiting_for_admin_contact': True}

            # request_contact=True отправляет только контакт отправителя.
            # Используем request_users (если поддерживается), чтобы открыть выбор пользователя в Telegram.
            keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            request_users_supported = hasattr(types, 'KeyboardButtonRequestUsers')
            if request_users_supported:
                request = types.KeyboardButtonRequestUsers(
                    request_id=1,
                    user_is_bot=False
                )
                keyboard.add(types.KeyboardButton("👥 Выбрать пользователя", request_users=request))
            keyboard.add(types.KeyboardButton("❌ Отмена"))

            if request_users_supported:
                text = (
                    "Нажмите «👥 Выбрать пользователя» и выберите нужного человека.\n\n"
                    "Если кнопка не работает в вашем клиенте Telegram, отправьте контакт вручную:\n"
                    "Скрепка -> Контакт."
                )
            else:
                text = (
                    "Ваша версия библиотеки не поддерживает кнопку выбора пользователя.\n"
                    "Отправьте контакт вручную:\n"
                    "Скрепка -> Контакт -> выбрать пользователя."
                )

            bot.send_message(call.message.chat.id, text, reply_markup=keyboard)
            bot.answer_callback_query(call.id, "📇 Запрос контакта")

        elif method == 'add_cancel':
            bot.send_message(call.message.chat.id, "❌ Добавление админа отменено")
            bot.answer_callback_query(call.id, "❌ Отменено")

    @bot.callback_query_handler(func=lambda call: call.data in ['reset_all_counters', 'cancel_reset_counters'])
    def handle_reset_counters(call):
        user_id = call.from_user.id

        if not db.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
            return

        if call.data == 'reset_all_counters':
            # Используем функцию reset_all_traffic из database
            if db.reset_all_traffic():
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text="✅ Все счетчики трафика сброшены!\n\n"
                         "Следующее обновление будет считать трафик от новых базовых значений."
                )
            else:
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text="❌ Ошибка сброса счетчиков"
                )
        else:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="❌ Сброс счетчиков отменен"
            )

        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('listusers_'))
    def handle_listusers_callback(call):
        """Обработчик навигации по списку пользователей"""
        chat_id = call.message.chat.id
        message_id = call.message.message_id
        callback_id = call.id

        # Импортируем здесь, чтобы избежать циклического импорта
        from handlers.user_handlers import list_users_pages, show_list_users_page

        if chat_id not in list_users_pages:
            try:
                bot.answer_callback_query(callback_id, "Данные устарели. Используйте /listusers снова")
            except:
                pass
            return

        try:
            # Обрабатываем разные типы callback
            if call.data.startswith('listusers_prev_'):
                try:
                    page = int(call.data.split('_')[2])
                    list_users_pages[chat_id]['page'] = max(0, page)
                except:
                    list_users_pages[chat_id]['page'] = max(0, list_users_pages[chat_id]['page'] - 1)

            elif call.data.startswith('listusers_next_'):
                try:
                    page = int(call.data.split('_')[2])
                    total_pages = (len(list_users_pages[chat_id]['users']) +
                                   list_users_pages[chat_id]['page_size'] - 1) // list_users_pages[chat_id]['page_size']
                    list_users_pages[chat_id]['page'] = min(total_pages - 1, page)
                except:
                    list_users_pages[chat_id]['page'] += 1

            elif call.data == 'listusers_refresh':
                # Обновляем данные
                users = db.get_all_users()
                if users:
                    current_page = list_users_pages[chat_id]['page']
                    list_users_pages[chat_id]['users'] = users
                    # Проверяем, чтобы страница не вышла за пределы
                    total_pages = (len(users) + list_users_pages[chat_id]['page_size'] - 1) // \
                                  list_users_pages[chat_id]['page_size']
                    if current_page >= total_pages:
                        list_users_pages[chat_id]['page'] = max(0, total_pages - 1)

                    # Показываем обновленную страницу
                    show_list_users_page(bot, chat_id, message_id, callback_id)
                    return
                else:
                    try:
                        bot.answer_callback_query(callback_id, "📭 Нет пользователей")
                    except:
                        pass
                    return

            # Показываем страницу
            show_list_users_page(bot, chat_id, message_id, callback_id)

        except Exception as e:
            logger.error(f"Ошибка обработки callback: {e}")
            try:
                bot.answer_callback_query(callback_id, "⚠️ Ошибка обработки")
            except:
                pass


def process_add_admin_manual(message, bot):
    if message.text and message.text.startswith('/'):
        bot.send_message(message.chat.id, "❌ Добавление админа отменено")
        _clear_admin_add_state(message.from_user.id)
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
            _clear_admin_add_state(message.from_user.id)
        else:
            bot.send_message(message.chat.id, f"❌ Не удалось добавить пользователя в администраторы")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверный формат ID. Введите числовой ID.")


def process_add_admin_forward(message, bot):
    if message.text and (message.text.startswith('/cancel') or message.text == "❌ Отмена"):
        bot.send_message(message.chat.id, "❌ Добавление админа отменено")
        _clear_admin_add_state(message.from_user.id)
        return

    user_id, username, error_message = _extract_forwarded_user(message)
    if not user_id:
        bot.send_message(message.chat.id, error_message)
        return

    if db.add_admin(user_id, username, Config.SUPER_ADMIN_ID):
        bot.send_message(message.chat.id, f"✅ Пользователь {username} (ID: {user_id}) добавлен в администраторы")
        _clear_admin_add_state(message.from_user.id)
    else:
        bot.send_message(message.chat.id, f"❌ Не удалось добавить пользователя в администраторы")


def process_add_admin_contact(message, bot):
    """Обработчик добавления администратора через контакт"""
    if message.content_type == 'users_shared':
        users_shared = getattr(message, 'users_shared', None)
        users = getattr(users_shared, 'users', None) if users_shared else None
        if not users:
            bot.send_message(message.chat.id, "❌ Пользователь не выбран. Попробуйте снова.")
            return

        selected = users[0]
        selected_user_id = getattr(selected, 'user_id', None)
        if not selected_user_id:
            bot.send_message(message.chat.id, "❌ Не удалось получить ID выбранного пользователя.")
            return

        if selected_user_id == message.from_user.id:
            bot.send_message(message.chat.id, "❌ Вы выбрали себя. Выберите другого пользователя.")
            return

        username = f"Пользователь {selected_user_id}"
        if db.add_admin(selected_user_id, username, Config.SUPER_ADMIN_ID):
            bot.send_message(
                message.chat.id,
                f"✅ Пользователь {username} (ID: {selected_user_id}) добавлен в администраторы",
                reply_markup=types.ReplyKeyboardRemove()
            )
            _clear_admin_add_state(message.from_user.id)
        else:
            bot.send_message(message.chat.id, "❌ Не удалось добавить пользователя в администраторы")
        return

    if message.content_type == 'contact':
        contact = message.contact

        # Скрываем клавиатуру
        remove_markup = types.ReplyKeyboardRemove()
        bot.send_message(message.chat.id, "✅ Контакт получен", reply_markup=remove_markup)

        if contact.user_id:
            user_id = contact.user_id
            if user_id == message.from_user.id:
                bot.send_message(
                    message.chat.id,
                    "❌ Получен ваш собственный контакт. Отправьте контакт другого пользователя.",
                )
                return

            username = contact.first_name
            if contact.last_name:
                username += f" {contact.last_name}"

            # Добавляем номер телефона если есть
            if contact.phone_number:
                username += f" ({contact.phone_number})"

            if db.add_admin(user_id, username, Config.SUPER_ADMIN_ID):
                bot.send_message(message.chat.id, f"✅ Пользователь {username} добавлен в администраторы")
                _clear_admin_add_state(message.from_user.id)
            else:
                bot.send_message(message.chat.id, f"❌ Не удалось добавить пользователя в администраторы")
        else:
            bot.send_message(message.chat.id, "❌ Контакт не содержит информации о пользователе")

    elif message.text and message.text == "❌ Отмена":
        remove_markup = types.ReplyKeyboardRemove()
        bot.send_message(message.chat.id, "❌ Добавление админа отменено", reply_markup=remove_markup)
        _clear_admin_add_state(message.from_user.id)

    else:
        bot.send_message(message.chat.id, "❌ Не удалось получить контакт. Нажмите кнопку контакта или отправьте /cancel.")


def send_ios_profile(bot, call, username):
    bot.send_message(call.message.chat.id, f"📱 Отправка профиля для iOS ({username})...")
    bot.send_message(call.message.chat.id,
                     "<a href='https://telegra.ph/Testovaya-instrukciya-dlya-IOS-01-17'>Инструкция iOS</a>",
                     parse_mode='HTML')

    file_path = vpn_manager.get_profile_path(username, 'ios')
    if file_path:
        with open(file_path, 'rb') as file:
            bot.send_document(call.message.chat.id, file, caption="iOS профиль")
    else:
        bot.send_message(call.message.chat.id, f"❌ Файл iOS профиль не найден")


def send_android_profile(bot, call, username):
    bot.send_message(call.message.chat.id, f"🤖 Отправка профиля для Android v11+ ({username})...")
    bot.send_message(call.message.chat.id,
                     "<a href='https://telegra.ph/Instrukciya-Android-v11-01-17'>Инструкция Android</a>",
                     parse_mode='HTML')

    file_path = vpn_manager.get_profile_path(username, 'android')
    if file_path:
        with open(file_path, 'rb') as file:
            bot.send_document(call.message.chat.id, file, caption="Android профиль")
    else:
        bot.send_message(call.message.chat.id, f"❌ Файл Android профиль не найден")


def send_sswan_profile(bot, call, username):
    bot.send_message(call.message.chat.id, f"🤖 Отправка профиля для StrongSwan ({username})...")
    bot.send_message(call.message.chat.id,
                     "<a href='https://telegra.ph/Instrukciya-Android-do-11v-01-17'>Инструкция StrongSwan</a>",
                     parse_mode='HTML')

    file_path = vpn_manager.get_profile_path(username, 'sswan')
    if file_path:
        with open(file_path, 'rb') as file:
            bot.send_document(call.message.chat.id, file, caption="StrongSwan профиль")
    else:
        bot.send_message(call.message.chat.id, f"❌ Файл StrongSwan профиль не найден")


def send_macos_profile(bot, call, username):
    bot.send_message(call.message.chat.id, f"💻 Отправка профиля для MacOS ({username})...")
    bot.send_message(call.message.chat.id, "<a href='https://telegra.ph/Instrukciya-macOS-01-17'>Инструкция MacOS</a>",
                     parse_mode='HTML')

    file_path = vpn_manager.get_profile_path(username, 'macos')
    if file_path:
        with open(file_path, 'rb') as file:
            bot.send_document(call.message.chat.id, file, caption="MacOS профиль")
    else:
        bot.send_message(call.message.chat.id, f"❌ Файл MacOS профиль не найден")


def send_windows_profile(bot, call, username):
    bot.send_message(call.message.chat.id, f"🪟 Отправка профиля для Windows ({username})...")
    bot.send_message(call.message.chat.id,
                     "<a href='https://telegra.ph/Instrukciya-dlya-Windows-01-17'>Инструкция Windows</a>",
                     parse_mode='HTML')

    # Основной файл P12
    file_path = vpn_manager.get_profile_path(username, 'win')
    if file_path:
        with open(file_path, 'rb') as file:
            bot.send_document(call.message.chat.id, file, caption="Windows сертификат")
    else:
        bot.send_message(call.message.chat.id, f"❌ Файл Windows сертификат не найден")

    # Отправка дополнительных файлов из /root
    try:
        # Файл 1: Enable_Stronger_Ciphers_for_IKEv2_on_Windows.reg
        reg_file_path = "/root/Enable_Stronger_Ciphers_for_IKEv2_on_Windows.reg"
        if os.path.exists(reg_file_path):
            with open(reg_file_path, 'rb') as reg_file:
                bot.send_document(call.message.chat.id, reg_file,
                                  caption="Enable_Stronger_Ciphers_for_IKEv2_on_Windows.reg\n\n"
                                          "Этот файл реестра включает более сильные шифры для IKEv2 в Windows.\n"
                                          "Просто запустите его двойным кликом и согласитесь с изменениями.")
        else:
            logger.warning(f"Файл {reg_file_path} не найден")

        # Файл 2: ikev2_config_import.cmd
        cmd_file_path = "/root/ikev2_config_import.cmd"
        if os.path.exists(cmd_file_path):
            with open(cmd_file_path, 'rb') as cmd_file:
                bot.send_document(call.message.chat.id, cmd_file,
                                  caption="ikev2_config_import.cmd\n\n"
                                          "Этот командный файл импортирует конфигурацию VPN в Windows.\n"
                                          "Запустите его от имени администратора (правый клик -> Запуск от имени администратора).")
        else:
            logger.warning(f"Файл {cmd_file_path} не найден")

    except Exception as e:
        logger.error(f"Ошибка при отправке дополнительных файлов Windows: {e}")
        bot.send_message(call.message.chat.id, "⚠️ Не удалось отправить дополнительные файлы конфигурации")
