import telebot
import logging
from telebot import types
from datetime import datetime
from database import db
from utils import validate_username, format_traffic_stats, format_database_info, get_backup_info_text
from vpn_manager import vpn_manager
from config import Config
from traffic_monitor import traffic_monitor

logger = logging.getLogger(__name__)


def setup_user_handlers(bot):
    """Настройка обработчиков команд пользователя"""

    @bot.message_handler(commands=['start'])
    def start(message):
        user_id = message.from_user.id
        logger.info(f"Команда /start от {user_id}")

        if db.is_admin(user_id):
            if db.is_super_admin(user_id):
                welcome_text = """🚀 VPN Manager Bot - Супер Админ Панель

👑 Вы - супер-администратор

Доступные команды:
/adduser - Добавить пользователя VPN
/listusers - Список всех пользователей  
/stats - Статистика сервера
/userstats - Статистика по пользователям
/traffic - Общая статистика трафика
/activestats - Активные подключения
/syncstats - Синхронизировать статистику
/dbstatus - Информация о базе данных

👨‍💻 Админ-команды:
/admin - Панель администратора
/manage_admins - Управление администраторами
/deleteuser - Удалить пользователя
/dbclear - Очистить базу данных
/backup - Создать бэкап БД
/backuplist - Список бэкапов"""
            else:
                welcome_text = """🚀 VPN Manager Bot - Админ Панель

Доступные команды:
/adduser - Добавить пользователя VPN
/listusers - Список всех пользователей
/stats - Статистика сервера  
/userstats - Статистика по пользователям
/traffic - Общая статистика трафика
/activestats - Активные подключения
/syncstats - Синхронизировать статистику
/dbstatus - Информация о базе данных

👨‍💻 Админ-команды:
/admin - Панель администратора
/deleteuser - Удалить пользователя
/backup - Создать бэкап БД"""
        else:
            welcome_text = """🚀 VPN Manager Bot

У вас нет прав доступа к этому боту.
Обратитесь к администратору."""

        bot.send_message(message.chat.id, welcome_text)

    @bot.message_handler(commands=['adduser'])
    def add_user(message):
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Доступ запрещен")
            return

        logger.info(f"Команда /adduser от администратора {user_id}")

        msg = bot.send_message(
            message.chat.id,
            'Введите имя пользователя (только латиница, цифры, _ и -):'
        )
        bot.register_next_step_handler(msg, process_username_step, bot)

    def process_username_step(message, bot):
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            return

        username = message.text.strip()
        is_valid, validation_msg = validate_username(username)

        if not is_valid:
            retry_msg = bot.send_message(
                message.chat.id,
                f"❌ {validation_msg}\n\nПопробуйте еще раз:"
            )
            bot.register_next_step_handler(retry_msg, process_username_step, bot)
            return

        if db.user_exists(username):
            retry_msg = bot.send_message(
                message.chat.id,
                f"❌ Пользователь '{username}' уже существует\nВведите другое имя:"
            )
            bot.register_next_step_handler(retry_msg, process_username_step, bot)
            return

        bot.send_message(message.chat.id, f"⏳ Создаем пользователя '{username}'...")

        success, result_msg = vpn_manager.create_user(username)

        if not success:
            bot.send_message(message.chat.id, f"❌ Не удалось создать пользователя: {result_msg}")
            return

        # Получаем информацию об администраторе
        admin_username = f"@{message.from_user.username}" if message.from_user.username else f"{message.from_user.first_name}"

        if db.add_user(username, user_id, admin_username):
            bot.send_message(message.chat.id, f"✅ Пользователь '{username}' успешно создан!")
            show_platform_selector(bot, message.chat.id, username)
        else:
            bot.send_message(message.chat.id, f"⚠️ VPN создан, но ошибка записи в БД")
            show_platform_selector(bot, message.chat.id, username)

    @bot.message_handler(commands=['listusers'])
    def list_users(message):
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Доступ запрещен")
            return

        logger.info(f"Команда /listusers от администратора {user_id}")

        users = db.get_all_users()

        if not users:
            bot.send_message(message.chat.id, "📭 В базе данных нет пользователей")
            return

        user_list = "📋 Список пользователей:\n\n"
        for user in users:
            user_id_db, username, created_by, created_by_username, created_at, total_conn, last_conn, sent, received, is_active = user
            status = "🟢" if is_active else "⚪"
            user_list += f"{status} {username}\n"
            user_list += f"   Создан: {created_at[:10]} администратором {created_by_username}\n"
            if total_conn > 0:
                total_traffic = (sent or 0) + (received or 0)
                user_list += f"   Подключений: {total_conn}, трафик: {total_traffic / (1024 ** 3):.2f} GB\n"
            user_list += "\n"

        bot.send_message(message.chat.id, user_list)

    @bot.message_handler(commands=['stats'])
    def show_stats(message):
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Доступ запрещен")
            return

        logger.info(f"Команда /stats от администратора {user_id}")

        total_users = db.get_user_count()
        active_users = db.get_active_users_count()

        # Получаем свежие данные
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
                traffic_mb = (info['current_sent'] + info['current_received']) / (1024 * 1024)
                stats_text += f"\n• {username}: {traffic_mb:.1f} MB"

        bot.send_message(message.chat.id, stats_text)

    @bot.message_handler(commands=['syncstats'])
    def sync_stats(message):
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Доступ запрещен")
            return

        logger.info(f"Команда /syncstats от администратора {user_id}")

        bot.send_message(message.chat.id, "🔄 Принудительная синхронизация статистики...")

        active_count, updated_count, disconnected_count = traffic_monitor.update_traffic_stats()

        if active_count > 0 or disconnected_count > 0:
            bot.send_message(message.chat.id, f"✅ Синхронизация завершена.\n"
                                              f"🔌 Активных: {active_count}\n"
                                              f"📤 Обновлено трафика: {updated_count}\n"
                                              f"🔴 Отключений: {disconnected_count}")
        else:
            bot.send_message(message.chat.id, "ℹ️ Активных подключений не найдено")

    @bot.message_handler(commands=['activestats'])
    def show_active_stats(message):
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Доступ запрещен")
            return

        logger.info(f"Команда /activestats от администратора {user_id}")

        traffic_data = traffic_monitor.parse_ipsec_status()

        if not traffic_data:
            bot.send_message(message.chat.id, "📭 Нет активных подключений")
            return

        stats_text = "🟢 Активные подключения (из ipsec):\n\n"

        for username, data in traffic_data.items():
            total_traffic = (data['current_sent'] + data['current_received']) / (1024 ** 2)  # MB

            stats_text += f"👤 {username}\n"
            stats_text += f"   IP: {data['client_ip']}\n"
            stats_text += f"   ID: {data['connection_id']}\n"
            stats_text += f"   Трафик: {total_traffic:.2f} MB\n"
            stats_text += f"   (отправлено: {data['current_sent'] / 1024 / 1024:.1f} MB, получено: {data['current_received'] / 1024 / 1024:.1f} MB)\n\n"

        stats_text += f"Всего активных: {len(traffic_data)}"

        bot.send_message(message.chat.id, stats_text)

    @bot.message_handler(commands=['userstats'])
    def user_stats(message):
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Доступ запрещен")
            return

        logger.info(f"Команда /userstats от администратора {user_id}")

        users = db.get_all_users()
        if not users:
            bot.send_message(message.chat.id, "📭 В базе данных нет пользователей")
            return

        buttons = []
        for user in users:
            user_id_db, username, created_by, created_by_username, created_at, total_conn, last_conn, sent, received, is_active = user
            status = "🟢" if is_active else "⚪"
            buttons.append([types.InlineKeyboardButton(
                f"{status} {username}",
                callback_data=f'userstats_{username}'
            )])

        markup = types.InlineKeyboardMarkup(buttons)
        bot.send_message(message.chat.id, "Выберите пользователя для просмотра статистики:", reply_markup=markup)

    @bot.message_handler(commands=['traffic'])
    def traffic_stats(message):
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Доступ запрещен")
            return

        logger.info(f"Команда /traffic от администратора {user_id}")

        users = db.get_all_users()
        if not users:
            bot.send_message(message.chat.id, "📭 Нет данных о трафике")
            return

        # Сортируем по трафику
        users_sorted = sorted(users, key=lambda x: (x[7] or 0) + (x[8] or 0), reverse=True)

        stats_text = "📊 Общая статистика трафика (Топ-10)\n\n"
        total_traffic_all = 0

        for user in users_sorted[:10]:
            username = user[1]
            total_conn = user[5] or 0
            sent = user[7] or 0
            received = user[8] or 0
            is_active = user[9]
            last_conn = user[6]

            total_traffic = sent + received
            total_traffic_all += total_traffic

            if total_traffic > 0:
                status = "🟢" if is_active else "⚪"
                stats_text += f"{status} {username}:\n"
                stats_text += f"   • Подключений: {total_conn}\n"
                stats_text += f"   • Трафик: {total_traffic / (1024 ** 3):.2f} GB\n"
                if last_conn:
                    stats_text += f"   • Активность: {last_conn[:10]}\n"
                stats_text += "\n"

        stats_text += f"📈 Всего трафика: {total_traffic_all / (1024 ** 3):.2f} GB"

        bot.send_message(message.chat.id, stats_text)

    @bot.message_handler(commands=['dbstatus'])
    def show_db_status(message):
        user_id = message.from_user.id

        if not db.is_admin(user_id):
            bot.send_message(message.chat.id, "⛔ Доступ запрещен")
            return

        logger.info(f"Команда /dbstatus от администратора {user_id}")

        db_info = format_database_info()
        monitor_status = traffic_monitor.get_monitor_status()

        status_text = f"""📊 Статус системы

{db_info}

⏱️ Мониторинг трафика:
{'🟢 Активен' if monitor_status['running'] else '🔴 Остановлен'}
Последнее обновление: {monitor_status['last_update'][:19]}
Следующее обновление через: {monitor_status['next_update_in']:.0f} сек
Интервал обновления: {monitor_status['update_interval']} сек"""

        bot.send_message(message.chat.id, status_text)


def show_platform_selector(bot, chat_id, username):
    """Показывает выбор платформы для конфигурации"""
    ios_btn = types.InlineKeyboardButton("📱 iOS", callback_data=f'platform_ios_{username}')
    android_old_btn = types.InlineKeyboardButton("🤖 Android до v11", callback_data=f'platform_sswan_{username}')
    android_new_btn = types.InlineKeyboardButton("🤖 Android v11+", callback_data=f'platform_android_{username}')
    mac_btn = types.InlineKeyboardButton("💻 MacOS", callback_data=f'platform_macos_{username}')
    win_btn = types.InlineKeyboardButton("🪟 Windows", callback_data=f'platform_win_{username}')

    buttons = [
        [ios_btn, mac_btn],
        [android_old_btn, android_new_btn],
        [win_btn]
    ]

    markup = types.InlineKeyboardMarkup(buttons)
    bot.send_message(
        chat_id,
        f"Выберите платформу для установки VPN пользователя '{username}':",
        reply_markup=markup
    )