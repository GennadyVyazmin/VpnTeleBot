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

# Глобальные переменные для хранения состояния пагинации
list_users_pages = {}


def split_message(text, max_length=4000):
    """Разбивает текст на части не длиннее max_length символов"""
    return [text[i:i + max_length] for i in range(0, len(text), max_length)]


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
/debugtraffic - Отладочная информация о трафике
/resettrafficcounter - Сбросить счетчики трафика

👨‍💻 Админ-команды:
/admin - Панель администратора
/manage_admins - Управление администраторами
/deleteuser - Удалить пользователя
/dbclear - Очистить база данных
/backup - Создать бэкап БД
/backuplist - Список бэкапов
/resettraffic - Обнулить статистику трафика"""
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
/debugtraffic - Отладочная информация о трафике
/resettrafficcounter - Сбросить счетчики трафика

👨‍💻 Админ-команды:
/admin - Панель администратора
/deleteuser - Удалить пользователя
/backup - Создать бэкап БД
/resettraffic - Обнулить статистику трафика"""
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

        # Сохраняем данные для пагинации
        chat_id = message.chat.id
        list_users_pages[chat_id] = {
            'users': users,
            'page': 0,
            'page_size': 15  # Пользователей на страницу
        }

        # Показываем первую страницу
        show_list_users_page(bot, chat_id)

    def show_list_users_page(bot, chat_id, edit_message_id=None):
        """Показывает страницу списка пользователей"""
        if chat_id not in list_users_pages:
            return

        data = list_users_pages[chat_id]
        users = data['users']
        page = data['page']
        page_size = data['page_size']

        total_pages = (len(users) + page_size - 1) // page_size
        start_idx = page * page_size
        end_idx = min(start_idx + page_size, len(users))

        user_list = f"📋 Список пользователей (стр. {page + 1}/{total_pages}):\n\n"

        for i in range(start_idx, end_idx):
            user = users[i]

            # Безопасное извлечение данных
            username = user[1] if len(user) > 1 else "Unknown"
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

        user_list += f"Всего пользователей: {len(users)}"

        # Создаем кнопки навигации
        markup = types.InlineKeyboardMarkup()
        buttons = []

        if page > 0:
            buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f'listusers_prev_{page - 1}'))

        if page < total_pages - 1:
            buttons.append(types.InlineKeyboardButton("Вперед ➡️", callback_data=f'listusers_next_{page + 1}'))

        if buttons:
            markup.row(*buttons)

        # Кнопка обновления
        markup.row(types.InlineKeyboardButton("🔄 Обновить", callback_data='listusers_refresh'))

        if edit_message_id:
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_message_id,
                    text=user_list,
                    reply_markup=markup
                )
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" in str(e):
                    # Это нормально - пользователь нажал на ту же самую кнопку
                    bot.answer_callback_query(callback_query_id=None)
                else:
                    logger.error(f"Ошибка редактирования сообщения: {e}")
                    # Попробуем отправить новое сообщение
                    bot.send_message(chat_id, user_list, reply_markup=markup)
            except Exception as e:
                logger.error(f"Ошибка редактирования сообщения: {e}")
                bot.send_message(chat_id, user_list, reply_markup=markup)
        else:
            bot.send_message(chat_id, user_list, reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('listusers_'))
    def handle_listusers_callback(call):
        """Обработчик навигации по списку пользователей"""
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        if chat_id not in list_users_pages:
            bot.answer_callback_query(call.id, "Данные устарели. Используйте /listusers снова")
            return

        # Обрабатываем разные типы callback
        if call.data.startswith('listusers_prev_'):
            try:
                page = int(call.data.split('_')[2])
                list_users_pages[chat_id]['page'] = max(0, page)
            except:
                list_users_pages[chat_id]['page'] = max(0, list_users_pages[chat_id]['page'] - 1)
            show_list_users_page(bot, chat_id, message_id)
            bot.answer_callback_query(call.id)

        elif call.data.startswith('listusers_next_'):
            try:
                page = int(call.data.split('_')[2])
                total_pages = (len(list_users_pages[chat_id]['users']) +
                               list_users_pages[chat_id]['page_size'] - 1) // list_users_pages[chat_id]['page_size']
                list_users_pages[chat_id]['page'] = min(total_pages - 1, page)
            except:
                list_users_pages[chat_id]['page'] += 1
            show_list_users_page(bot, chat_id, message_id)
            bot.answer_callback_query(call.id)

        elif call.data == 'listusers_refresh':
            # Обновляем данные
            users = db.get_all_users()
            if users:
                current_page = list_users_pages[chat_id]['page']
                list_users_pages[chat_id]['users'] = users
                # Проверяем, чтобы страница не вышла за пределы
                total_pages = (len(users) + list_users_pages[chat_id]['page_size'] - 1) // list_users_pages[chat_id][
                    'page_size']
                if current_page >= total_pages:
                    list_users_pages[chat_id]['page'] = max(0, total_pages - 1)

                show_list_users_page(bot, chat_id, message_id)
                bot.answer_callback_query(call.id, "✅ Список обновлен")
            else:
                bot.answer_callback_query(call.id, "📭 Нет пользователей")

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
                traffic_mb = (info['absolute_sent'] + info['absolute_received']) / (1024 * 1024)
                stats_text += f"\n• {username}: {traffic_mb:.1f} MB (абсолютные значения)"

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
            total_traffic = (data['absolute_sent'] + data['absolute_received']) / (1024 ** 2)  # MB

            stats_text += f"👤 {username}\n"
            stats_text += f"   IP: {data['client_ip']}\n"
            stats_text += f"   ID: {data['connection_id']}\n"
            stats_text += f"   Абсолютные значения:\n"
            stats_text += f"     • Отправлено: {data['absolute_sent'] / 1024 / 1024:.1f} MB\n"
            stats_text += f"     • Получено: {data['absolute_received'] / 1024 / 1024:.1f} MB\n"
            stats_text += f"   Всего: {total_traffic:.2f} MB\n\n"

        stats_text += f"Всего активных: {len(traffic_data)}"

        # Защита от слишком длинных сообщений
        if len(stats_text) > 4000:
            parts = split_message(stats_text)
            for i, part in enumerate(parts):
                if i == 0:
                    bot.send_message(message.chat.id, part)
                else:
                    bot.send_message(message.chat.id, f"`{part}`", parse_mode='Markdown')
        else:
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

        # Создаем пагинированный список кнопок
        buttons_per_page = 10
        total_pages = (len(users) + buttons_per_page - 1) // buttons_per_page
        page = 0  # Можно добавить навигацию по страницам

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

        markup = types.InlineKeyboardMarkup(buttons)
        bot.send_message(
            message.chat.id,
            f"Выберите пользователя для просмотра статистики (стр. {page + 1}/{total_pages}):",
            reply_markup=markup
        )

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

    @bot.message_handler(commands=['debugtraffic'])
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
            debug_text += f"    • Отправлено: {data['absolute_sent']:,} bytes ({data['absolute_sent'] / 1024 / 1024:.1f} MB)\n"
            debug_text += f"    • Получено: {data['absolute_received']:,} bytes ({data['absolute_received'] / 1024 / 1024:.1f} MB)\n"

            # Получаем базовые значения
            base = traffic_monitor.get_base_traffic(username)
            debug_text += f"  Базовые значения:\n"
            debug_text += f"    • Отправлено: {base['sent']:,} bytes\n"
            debug_text += f"    • Получено: {base['received']:,} bytes\n"

            # Вычисляем разницу
            sent_diff = max(0, data['absolute_sent'] - base['sent'])
            received_diff = max(0, data['absolute_received'] - base['received'])
            debug_text += f"  Разница (будет добавлено):\n"
            debug_text += f"    • Отправлено: +{sent_diff:,} bytes (+{sent_diff / 1024 / 1024:.1f} MB)\n"
            debug_text += f"    • Получено: +{received_diff:,} bytes (+{received_diff / 1024 / 1024:.1f} MB)\n\n"

        # Разбиваем если слишком длинное
        if len(debug_text) > 4000:
            parts = split_message(debug_text)
            for i, part in enumerate(parts):
                if i == 0:
                    bot.send_message(message.chat.id, f"```{part}```", parse_mode='Markdown')
                else:
                    bot.send_message(message.chat.id, f"```{part}```", parse_mode='Markdown')
        else:
            bot.send_message(message.chat.id, f"```{debug_text}```", parse_mode='Markdown')

    @bot.message_handler(commands=['resettrafficcounter'])
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