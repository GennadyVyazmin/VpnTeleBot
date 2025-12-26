[file name]: main.py
[file content begin]
import os
import sys
import atexit
import logging
import telebot
from pathlib import Path

# Добавляем путь к модулям
sys.path.append(str(Path(__file__).parent))

from config import Config
from database import db
from vpn_manager import vpn_manager
from traffic_monitor import traffic_monitor

# Импортируем обработчики
from handlers.user_handlers import setup_user_handlers
from handlers.admin_handlers import setup_admin_handlers
from handlers.callback_handlers import setup_callback_handlers

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(Config.LOG_FILE),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


def check_single_instance():
    """Проверка единственного экземпляра"""
    lock_file = '/tmp/vpn_bot.lock'

    if os.path.exists(lock_file):
        try:
            with open(lock_file, 'r') as f:
                old_pid = int(f.read().strip())
            try:
                os.kill(old_pid, 0)
                print(f"❌ Бот уже запущен с PID: {old_pid}")
                print(f"Если это не так, удалите lock файл: rm -f {lock_file}")
                sys.exit(1)
            except OSError:
                os.unlink(lock_file)
                print("⚠️ Удален старый lock файл")
        except Exception as e:
            os.unlink(lock_file)
            print("⚠️ Удален поврежденный lock файл")

    try:
        with open(lock_file, 'w') as f:
            f.write(str(os.getpid()))
        print(f"🔒 Lock файл создан: {lock_file}")
    except Exception as e:
        print(f"❌ Ошибка создания lock файла: {e}")
        sys.exit(1)

    def cleanup():
        try:
            if os.path.exists(lock_file):
                os.unlink(lock_file)
                print("🔓 Lock файл удален")
        except Exception as e:
            print(f"⚠️ Ошибка удаления lock файла: {e}")

    atexit.register(cleanup)
    return cleanup


def main():
    """Основная функция запуска бота"""

    # Проверка единственного экземпляра
    cleanup = check_single_instance()

    # Проверка токена бота
    if not Config.BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не установлен в переменных окружения")
        raise ValueError("Токен бота не найден")

    # Инициализация бота
    bot = telebot.TeleBot(Config.BOT_TOKEN)

    # Импортируем здесь, чтобы избежать циклического импорта
    from handlers.user_handlers import user_states

    # Настройка обработчиков
    setup_user_handlers(bot)
    setup_admin_handlers(bot)
    setup_callback_handlers(bot)

    # Обработчик неизвестных команд
    @bot.message_handler(func=lambda message: True)
    def handle_unknown(message):
        user_id = message.from_user.id

        # Проверяем, не находится ли пользователь в процессе ввода
        if user_id in user_states:
            state = user_states[user_id]

            if state.get('waiting_for_username'):
                # Пользователь вводит имя - обрабатываем в user_handlers
                from handlers.user_handlers import process_username_step
                process_username_step(bot, message)
                return

            elif state.get('waiting_for_admin_id'):
                # Пользователь вводит ID админа
                from handlers.callback_handlers import process_add_admin_manual
                process_add_admin_manual(message, bot)
                return

        # Если не в состоянии ожидания ввода, показываем сообщение
        logger.info(f"Неизвестная команда от {user_id}: {message.text}")

        if db.is_admin(user_id):
            bot.send_message(message.chat.id, "❓ Неизвестная команда. Используйте /start")
        else:
            bot.send_message(message.chat.id, "⛔ У вас нет доступа")

    # Запуск мониторинга трафика
    traffic_monitor.start_monitoring()

    # Информация о запуске
    print("=" * 60)
    print("🚀 VPN Manager Bot запущен!")
    print(f"📁 Конфигурации: {Config.VPN_PROFILES_PATH}")
    print(f"👑 Супер-админ ID: {Config.SUPER_ADMIN_ID}")
    print(f"🗄️  База данных: {Config.DB_PATH}")
    print(f"💾 Директория бэкапов: {Config.BACKUP_DIR}")
    print(f"⏱️  Мониторинг: каждые {Config.STATS_UPDATE_INTERVAL} секунд")
    print(f"🧹 Очистка сессий: каждые {Config.SESSION_CLEANUP_INTERVAL} секунд")
    print(f"📈 Хранение бэкапов: {Config.BACKUP_RETENTION_DAYS} дней")
    print("=" * 60)
    print("🎯 Основные исправления:")
    print("✅ Правильный подсчет трафика (разница между обновлениями)")
    print("✅ Резервное копирование перед очисткой сессий")
    print("✅ Автоматическое обнаружение отключений")
    print("✅ Уникальные хэши сессий для переподключений")
    print("✅ Graceful shutdown с фиксацией трафика")
    print("✅ Очистка старых бэкапов")
    print("=" * 60)

    # Запуск бота
    try:
        logger.info("Запуск polling бота...")
        bot.polling(none_stop=True, interval=1, timeout=30)
    except Exception as e:
        logger.critical(f"Критическая ошибка бота: {str(e)}")
        print(f"❌ Критическая ошибка: {str(e)}")
        raise
    finally:
        cleanup()


if __name__ == "__main__":
    main()
[file content end]