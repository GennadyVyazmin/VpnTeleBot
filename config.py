import os
from pathlib import Path

# Загрузка переменных окружения
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Основные настройки
    BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    SUPER_ADMIN_ID = 149999149

    # Пути
    BASE_DIR = Path(__file__).parent
    DB_PATH = BASE_DIR / 'users.db'
    BACKUP_DIR = BASE_DIR / 'backups'
    IKEV2_SCRIPT_PATH = '/usr/bin/ikev2.sh'
    VPN_PROFILES_PATH = '/root/'

    # Настройки пользователей
    MIN_USERNAME_LENGTH = 3
    MAX_USERNAME_LENGTH = 20
    USERNAME_PATTERN = r'^[a-zA-Z0-9_-]+$'

    # Настройки мониторинга
    STATS_UPDATE_INTERVAL = 30  # секунды (увеличена частота!)
    SESSION_CLEANUP_INTERVAL = 300  # очистка старых сессий
    BACKUP_RETENTION_DAYS = 7  # хранить бэкапы 7 дней

    # Логирование
    LOG_LEVEL = 'INFO'
    LOG_FILE = BASE_DIR / 'vpn_bot.log'

    @classmethod
    def ensure_directories(cls):
        """Создает необходимые директории"""
        cls.BACKUP_DIR.mkdir(exist_ok=True)