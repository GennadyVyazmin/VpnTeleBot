import telebot
import logging
from telebot import types
from datetime import datetime
from database import db
from utils import validate_username, format_traffic_stats, format_database_info, get_backup_info_text, format_bytes
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
            # ВНИМАНИЕ: теперь в кортеже 11 элементов!
            # 0: id, 1: username, 2: created_by, 3: created_by_username, 4: created_at,
            # 5: total_connections, 6: last_connected, 7: total_bytes_sent, 8: total_bytes_received,
            # 9: is_active, 10: last_updated
            if len(user) >= 11:
                username = user[1]
                created_by_username = user[3]
                created_at = user[4]
                total_conn = user[5] or 0
                last_conn = user[6]
                sent = user[7] or 0
                received = user[8] or 0
                is_active = user[9]
            else:
                # Для обратной совместимости со старыми версиями
                username = user[1]
                created_by_username = user[3] if len(user) > 3 else "Неизвестно"
                created_at = user[4] if len(user) > 4 else ""
                total_conn = user[5] if len(user) > 5 else 0
                last_conn = user[6] if len(user) > 6 else ""
                sent = user[7] if len(user) > 7 else 0
                received = user[8] if len(user) > 8 else 0
                is_active = user[9] if len(user) > 9 else 0

            status = "🟢" if is_active else "⚪"
            user_list += f"{status} {username}\n"
            user_list += f"   Создан: {created_at[:10] if created_at else 'Неизвестно'} администратором {created_by_username}\n"
            if total_conn > 0:
                total_traffic = sent + received
                user_list += f"   Подключений: {total_conn}, трафик: {format_bytes(total_traffic)}\n"
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
            if len(user) >= 2:
                username = user[1]
                is_active = user[9] if len(user) > 9 else 0
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
        users_sorted = sorted(users, key=lambda x: ((x[7] or 0) + (x[8] or 0) if len(x) > 8 else 0), reverse=True)

        stats_text = "📊 Общая статистика трафика (Топ-10)\n\n"
        total_traffic_all = 0

        for user in users_sorted[:10]:
            if len(user) >= 9:
                username = user[1]
                total_conn = user[5] or 0
                sent = user[7] or 0
                received = user[8] or 0
                is_active = user[9] if len(user) > 9 else 0
                last_conn = user[6] if len(user) > 6 else None
            else:
                # Для обратной совместимости
                continue

            total_traffic = sent + received
            total_traffic_all += total_traffic

            if total_traffic > 0:
                status = "🟢" if is_active else "⚪"
                stats_text += f"{status} {username}:\n"
                stats_text += f"   • Подключений: {total_conn}\n"
                stats_text += f"   • Трафик: {format_bytes(total_traffic)}\n"
                if last_conn:
                    stats_text += f"   • Активность: {last_conn[:10]}\n"
                stats_text += "\n"

        stats_text += f"📈 Всего трафика: {format_bytes(total_traffic_all)}"

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


@bot.message_handler(commands=['debugtraffic'])
@admin_required
def debug_traffic(message):
    """Отладочная информация о трафике"""
    user_id = message.from_user.id

    if not db.is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ Доступ запрещен")
        return

    logger.info(f"Команда /debugtraffic от администратора {user_id}")

    # Получаем сырые данные из ipsec
    traffic_data = traffic_monitor.parse_ipsec_status()

    if not traffic_data:
        bot.send_message(message.chat.id, "📭 Нет активных подключений")
        return

    debug_text = "🔧 Отладочная информация о трафике:\n\n"

    for username, data in traffic_data.items():
        debug_text += f"👤 {username}:\n"
        debug_text += f"  IP: {data['client_ip']}\n"
        debug_text += f"  Connection ID: {data['connection_id']}\n"
        debug_text += f"  Абсолютные значения из ipsec:\n"
        debug_text += f"    • Отправлено: {data['absolute_sent']} bytes ({data['absolute_sent'] / 1024 / 1024:.1f} MB)\n"
        debug_text += f"    • Получено: {data['absolute_received']} bytes ({data['absolute_received'] / 1024 / 1024:.1f} MB)\n"

        # Получаем базовые значения
        base = traffic_monitor.get_base_traffic(username)
        debug_text += f"  Базовые значения:\n"
        debug_text += f"    • Отправлено: {base['sent']} bytes\n"
        debug_text += f"    • Получено: {base['received']} bytes\n"

        # Вычисляем разницу
        sent_diff = max(0, data['absolute_sent'] - base['sent'])
        received_diff = max(0, data['absolute_received'] - base['received'])
        debug_text += f"  Разница (будет добавлено):\n"
        debug_text += f"    • Отправлено: +{sent_diff} bytes (+{sent_diff / 1024 / 1024:.1f} MB)\n"
        debug_text += f"    • Получено: +{received_diff} bytes (+{received_diff / 1024 / 1024:.1f} MB)\n\n"

    bot.send_message(message.chat.id, f"```{debug_text}```", parse_mode='Markdown')


@bot.message_handler(commands=['resettrafficcounter'])
@admin_required
def reset_traffic_counter(message):
    """Сбросить счетчики трафика (для тестирования)"""
    user_id = message.from_user.id

    if not db.is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ Доступ запрещен")
        return

    logger.info(f"Команда /resettrafficcounter от администратора {user_id}")

    # Получаем имя пользователя если указано
    text = message.text.strip()
    parts = text.split()

    if len(parts) > 1:
        username = parts[1]
        if traffic_monitor.reset_traffic_counter(username):
            bot.send_message(message.chat.id, f"✅ Счетчики трафика сброшены для пользователя {username}")
        else:
            bot.send_message(message.chat.id, f"❌ Ошибка сброса счетчиков для {username}")
    else:
        # Сброс всех счетчиков
        buttons = [
            [types.InlineKeyboardButton("✅ Сбросить ВСЕ счетчики", callback_data='reset_all_counters')],
            [types.InlineKeyboardButton("❌ Отмена", callback_data='cancel_reset_counters')]
        ]
        markup = types.InlineKeyboardMarkup(buttons)

        bot.send_message(
            message.chat.id,
            "⚠️ Сбросить все счетчики трафика?\n\n"
            "Это приведет к тому, что текущие абсолютные значения из ipsec станут базовыми.\n"
            "Следующее обновление будет считать трафик от новых базовых значений.",
            reply_markup=markup
        )