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

    @bot.callback_query_handler(func=lambda call: call.data.startswith('userstats_'))
    def handle_user_stats(call):
        user_id = call.from_user.id

        if not db.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
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
            show_stats(call.message)
            bot.answer_callback_query(call.id, "📊 Статистика обновлена")

        elif action == 'admin_restart':
            bot.send_message(call.message.chat.id, "🔄 Перезапуск VPN службы...")
            try:
                subprocess.run(['systemctl', 'restart', 'strongswan'], check=True)
                bot.send_message(call.message.chat.id, "✅ StrongSwan перезапущен")
            except subprocess.CalledProcessError as e:
                bot.send_message(call.message.chat.id, f"❌ Ошибка перезапуска StrongSwan: {e}")
            except Exception as e:
                bot.send_message(call.message.chat.id, f"❌ Неожиданная ошибка: {str(e)}")
            bot.answer_callback_query(call.id, "🔄 Перезапуск")

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

        elif action == 'admin_clear_db':
            from handlers.admin_handlers import clear_database
            clear_database(call.message)
            bot.answer_callback_query(call.id, "🧹 Подтвердите очистку")

        elif action == 'admin_manage':
            if db.is_super_admin(user_id):
                from handlers.admin_handlers import manage_admins
                manage_admins(call.message)
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
                "Перешлите любое сообщение от пользователя, которого хотите добавить в администраторы."
            )
            bot.register_next_step_handler(msg, process_add_admin_forward, bot)
            bot.answer_callback_query(call.id, "🔗 Перешлите сообщение")

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
            if traffic_monitor.reset_traffic_counter():
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

    if not message.forward_from:
        bot.send_message(message.chat.id, "❌ Не удалось получить информацию о пользователе.")
        return

    forward_from = message.forward_from
    user_id = forward_from.id
    username = f"@{forward_from.username}" if forward_from.username else f"{forward_from.first_name}"

    if db.add_admin(user_id, username, Config.SUPER_ADMIN_ID):
        bot.send_message(message.chat.id, f"✅ Пользователь {username} (ID: {user_id}) добавлен в администраторы")
    else:
        bot.send_message(message.chat.id, f"❌ Не удалось добавить пользователя в администраторы")


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