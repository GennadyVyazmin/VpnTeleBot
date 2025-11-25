import os
import sqlite3
import logging
import subprocess
import re
import shutil
import time
import sys
import atexit
import threading
from pathlib import Path
from datetime import datetime, timedelta
from sqlite3 import OperationalError

import telebot
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()


# Проверка единственного экземпляра
def check_single_instance():
    """Проверка, что запущен только один экземпляр бота"""
    lock_file = '/tmp/vpn_bot.lock'

    if os.path.exists(lock_file):
        try:
            with open(lock_file, 'r') as f:
                old_pid = int(f.read().strip())
            try:
                os.kill(old_pid, 0)
                print(f"❌ Бот уже запущен с PID: {old_pid}")
                print("Если это не так, удалите lock файл:")
                print(f"rm -f {lock_file}")
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


cleanup_function = check_single_instance()


# Конфигурация
class Config:
    DB_PATH = 'users.db'
    SUPER_ADMIN_ID = 149999149
    BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

    IKEV2_SCRIPT_PATH = '/usr/bin/ikev2.sh'
    VPN_PROFILES_PATH = '/root/'

    MIN_USERNAME_LENGTH = 3
    MAX_USERNAME_LENGTH = 20
    USERNAME_PATTERN = r'^[a-zA-Z0-9_-]+$'


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('vpn_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

if not Config.BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN не установлен в переменных окружения")
    raise ValueError("Токен бота не найден")

if not os.path.exists(Config.IKEV2_SCRIPT_PATH):
    logger.error(f"Скрипт {Config.IKEV2_SCRIPT_PATH} не найден!")
    raise FileNotFoundError(f"ikev2.sh не найден по пути {Config.IKEV2_SCRIPT_PATH}")

if not os.access(Config.IKEV2_SCRIPT_PATH, os.X_OK):
    logger.error(f"Скрипт {Config.IKEV2_SCRIPT_PATH} не исполняемый!")
    raise PermissionError(f"Нет прав на выполнение {Config.IKEV2_SCRIPT_PATH}")

logger.info(f"Скрипт ikev2.sh найден: {Config.IKEV2_SCRIPT_PATH}")
logger.info(f"Директория конфигураций: {Config.VPN_PROFILES_PATH}")

bot = telebot.TeleBot(Config.BOT_TOKEN)


# Улучшенная модель работы с базой данных
class UserDB:
    def __init__(self):
        self.db_path = Config.DB_PATH
        self.max_retries = 5
        self.retry_delay = 1
        self.conn = self._create_connection()
        self.create_tables()  # Этот вызов должен быть после определения метода
    
    def _create_connection(self):
        for attempt in range(self.max_retries):
            try:
                conn = sqlite3.connect(
                    self.db_path,
                    check_same_thread=False,
                    timeout=30.0
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
                logger.info("Соединение с БД установлено успешно")
                return conn
            except OperationalError as e:
                if "locked" in str(e) and attempt < self.max_retries - 1:
                    logger.warning(f"БД заблокирована, попытка {attempt + 1}/{self.max_retries}")
                    time.sleep(self.retry_delay)
                else:
                    logger.error(f"Не удалось подключиться к БД: {str(e)}")
                    raise e
        raise OperationalError("Не удалось подключиться к БД после нескольких попыток")
    
    def execute_safe(self, query, params=()):
        for attempt in range(self.max_retries):
            try:
                cursor = self.conn.cursor()
                cursor.execute(query, params)
                return cursor
            except OperationalError as e:
                if "locked" in str(e) and attempt < self.max_retries - 1:
                    logger.warning(f"БД заблокирована при запросе, попытка {attempt + 1}")
                    time.sleep(self.retry_delay)
                    self.conn = self._create_connection()
                else:
                    logger.error(f"Ошибка выполнения запроса: {str(e)}")
                    raise e
    
    def commit_safe(self):
        for attempt in range(self.max_retries):
            try:
                self.conn.commit()
                return True
            except OperationalError as e:
                if "locked" in str(e) and attempt < self.max_retries - 1:
                    logger.warning(f"БД заблокирована при коммите, попытка {attempt + 1}")
                    time.sleep(self.retry_delay)
                    self.conn = self._create_connection()
                else:
                    logger.error(f"Ошибка коммита: {str(e)}")
                    return False
    
    def create_tables(self):
        """Создание таблиц с проверкой существования и миграцией данных"""
        try:
            # ПРОВЕРЯЕМ СУЩЕСТВОВАНИЕ ТАБЛИЦ ВМЕСТО УДАЛЕНИЯ
            tables_to_check = ['users', 'admins', 'user_stats', 'traffic_log']
            existing_tables = []
            
            for table in tables_to_check:
                cursor = self.execute_safe(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
                if cursor.fetchone() is not None:
                    existing_tables.append(table)
            
            if existing_tables:
                logger.info(f"Таблицы уже существуют: {', '.join(existing_tables)}")
                
                # Проверяем структуру таблицы users (добавлены ли новые поля)
                cursor = self.execute_safe("PRAGMA table_info(users)")
                columns = [column[1] for column in cursor.fetchall()]
                
                # Если нет новых полей created_by и created_by_username, добавляем их
                if 'created_by' not in columns:
                    logger.info("Добавляем новые поля в таблицу users...")
                    self.execute_safe("ALTER TABLE users ADD COLUMN created_by INTEGER")
                    self.execute_safe("ALTER TABLE users ADD COLUMN created_by_username TEXT")
                    
                    # Заполняем существующие записи значениями по умолчанию
                    self.execute_safe("UPDATE users SET created_by = ?, created_by_username = ? WHERE created_by IS NULL", 
                                   (Config.SUPER_ADMIN_ID, "Система"))
                    logger.info("Новые поля добавлены в таблицу users")
                
                return  # Таблицы уже существуют, выходим
            
            # Создаем таблицы только если они не существуют
            logger.info("Создаем таблицы базы данных...")
            
            # Создаем таблицу VPN пользователей с информацией о создателе
            self.execute_safe('''CREATE TABLE users (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                username TEXT UNIQUE NOT NULL,
                                created_by INTEGER NOT NULL,
                                created_by_username TEXT NOT NULL,
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                total_connections INTEGER DEFAULT 0,
                                last_connected TIMESTAMP,
                                total_bytes_sent BIGINT DEFAULT 0,
                                total_bytes_received BIGINT DEFAULT 0,
                                is_active BOOLEAN DEFAULT 0,
                                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                             )''')
            
            # Создаем таблицу администраторов
            self.execute_safe('''CREATE TABLE admins (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                user_id INTEGER UNIQUE NOT NULL,
                                username TEXT NOT NULL,
                                added_by INTEGER NOT NULL,
                                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                             )''')
            
            # Создаем таблицу для детальной статистики подключений
            self.execute_safe('''CREATE TABLE user_stats (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                username TEXT NOT NULL,
                                connection_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                connection_end TIMESTAMP,
                                duration_seconds INTEGER,
                                bytes_sent BIGINT DEFAULT 0,
                                bytes_received BIGINT DEFAULT 0,
                                client_ip TEXT,
                                status TEXT DEFAULT 'completed'
                             )''')
            
            # Создаем таблицу для ежедневной статистики трафика
            self.execute_safe('''CREATE TABLE traffic_log (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                username TEXT NOT NULL,
                                log_date DATE NOT NULL,
                                bytes_sent BIGINT DEFAULT 0,
                                bytes_received BIGINT DEFAULT 0,
                                connections_count INTEGER DEFAULT 0,
                                UNIQUE(username, log_date)
                             )''')
            
            # Добавляем супер-админа
            self.execute_safe("INSERT OR IGNORE INTO admins (user_id, username, added_by) VALUES (?, ?, ?)",
                          (Config.SUPER_ADMIN_ID, "Супер-админ", Config.SUPER_ADMIN_ID))
            
            self.commit_safe()
            logger.info("Все таблицы созданы с улучшенной структурой")
            
        except Exception as e:
            logger.error(f"Ошибка при создании/проверке таблиц: {str(e)}")
            raise e

    # Методы для VPN пользователей
    def user_exists(self, username):
        try:
            cursor = self.execute_safe("SELECT id FROM users WHERE username = ?", (username,))
            exists = cursor.fetchone() is not None
            logger.info(f"Проверка пользователя {username}: {'существует' if exists else 'не существует'}")
            return exists
        except Exception as e:
            logger.error(f"Ошибка проверки пользователя {username}: {str(e)}")
            return False
    
    def add_user(self, username, created_by, created_by_username):
        try:
            # Проверяем, существует ли пользователь
            if self.user_exists(username):
                logger.warning(f"Попытка добавить существующего пользователя {username}")
                return False
                
            self.execute_safe("INSERT INTO users (username, created_by, created_by_username) VALUES (?, ?, ?)", 
                           (username, created_by, created_by_username))
            success = self.commit_safe()
            if success:
                logger.info(f"Пользователь {username} добавлен в БД администратором {created_by_username} (ID: {created_by})")
            return success
        except sqlite3.IntegrityError:
            logger.warning(f"Попытка добавить существующего пользователя {username}")
            return False
        except Exception as e:
            logger.error(f"Ошибка добавления пользователя {username}: {str(e)}")
            return False
    
    def get_all_users(self):
        try:
            cursor = self.execute_safe("SELECT id, username, created_by, created_by_username, created_at, total_connections, last_connected, total_bytes_sent, total_bytes_received, is_active FROM users ORDER BY created_at DESC")
            users = cursor.fetchall()
            logger.info(f"Получено {len(users)} пользователей из БД")
            return users
        except Exception as e:
            logger.error(f"Ошибка получения пользователей: {str(e)}")
            return []
    
    def get_users_by_admin(self, admin_id):
        """Получить пользователей, созданных конкретным администратором"""
        try:
            cursor = self.execute_safe("SELECT id, username, created_by, created_by_username, created_at, total_connections, last_connected, total_bytes_sent, total_bytes_received, is_active FROM users WHERE created_by = ? ORDER BY created_at DESC", (admin_id,))
            users = cursor.fetchall()
            logger.info(f"Получено {len(users)} пользователей для администратора {admin_id}")
            return users
        except Exception as e:
            logger.error(f"Ошибка получения пользователей администратора {admin_id}: {str(e)}")
            return []
    
    def get_user_count(self):
        try:
            cursor = self.execute_safe("SELECT COUNT(*) FROM users")
            count = cursor.fetchone()[0]
            return count
        except Exception as e:
            logger.error(f"Ошибка получения количества пользователей: {str(e)}")
            return 0
    
    def delete_user(self, username):
        try:
            cursor = self.execute_safe("DELETE FROM users WHERE username = ?", (username,))
            success = self.commit_safe()
            if success:
                deleted = cursor.rowcount > 0
                if deleted:
                    logger.info(f"Пользователь {username} удален из БД")
                else:
                    logger.warning(f"Пользователь {username} не найден при удалении")
                return deleted
            return False
        except Exception as e:
            logger.error(f"Ошибка удаления пользователя {username}: {str(e)}")
            return False
    
    def clear_all_users(self):
        try:
            self.execute_safe("DELETE FROM users")
            success = self.commit_safe()
            if success:
                logger.info("Все пользователи удалены из БД")
            return success
        except Exception as e:
            logger.error(f"Ошибка очистки БД: {str(e)}")
            return False
    
    # Методы для администраторов
    def is_admin(self, user_id):
        try:
            cursor = self.execute_safe("SELECT id FROM admins WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"Ошибка проверки администратора {user_id}: {str(e)}")
            return False
    
    def is_super_admin(self, user_id):
        return user_id == Config.SUPER_ADMIN_ID
    
    def get_all_admins(self):
        try:
            cursor = self.execute_safe('''SELECT a.user_id, a.username, a.added_at, 
                         COALESCE(s.username, 'Супер-админ') as added_by_name
                         FROM admins a 
                         LEFT JOIN admins s ON a.added_by = s.user_id
                         ORDER BY a.added_at''')
            admins = cursor.fetchall()
            logger.info(f"Получено {len(admins)} администраторов из БД")
            return admins
        except Exception as e:
            logger.error(f"Ошибка получения администраторов: {str(e)}")
            return []
    
    def add_admin(self, user_id, username, added_by):
        try:
            self.execute_safe("INSERT INTO admins (user_id, username, added_by) VALUES (?, ?, ?)",
                          (user_id, username, added_by))
            success = self.commit_safe()
            if success:
                logger.info(f"Администратор {username} (ID: {user_id}) добавлен в БД")
            return success
        except sqlite3.IntegrityError:
            logger.warning(f"Попытка добавить существующего администратора {user_id}")
            return False
        except Exception as e:
            logger.error(f"Ошибка добавления администратора {user_id}: {str(e)}")
            return False
    
    def delete_admin(self, user_id):
        if user_id == Config.SUPER_ADMIN_ID:
            logger.warning("Попытка удалить супер-админа")
            return False
            
        try:
            cursor = self.execute_safe("DELETE FROM admins WHERE user_id = ?", (user_id,))
            success = self.commit_safe()
            if success:
                deleted = cursor.rowcount > 0
                if deleted:
                    logger.info(f"Администратор {user_id} удален из БД")
                else:
                    logger.warning(f"Администратор {user_id} не найден при удалении")
                return deleted
            return False
        except Exception as e:
            logger.error(f"Ошибка удаления администратора {user_id}: {str(e)}")
            return False

    # УЛУЧШЕННЫЕ МЕТОДЫ ДЛЯ СТАТИСТИКИ
    def update_user_activity(self, username, is_active=False, bytes_sent=0, bytes_received=0):
        """Обновление активности пользователя и статистики"""
        try:
            current_time = datetime.now().isoformat()
            
            if is_active:
                # Пользователь активен - обновляем время последнего подключения
                self.execute_safe('''UPDATE users 
                                  SET last_connected = ?, 
                                      is_active = 1,
                                      last_updated = ?,
                                      total_bytes_sent = total_bytes_sent + ?,
                                      total_bytes_received = total_bytes_received + ?
                                  WHERE username = ?''',
                              (current_time, current_time, bytes_sent, bytes_received, username))
            else:
                # Пользователь неактивен
                self.execute_safe('''UPDATE users 
                                  SET is_active = 0,
                                      last_updated = ?
                                  WHERE username = ?''',
                              (current_time, username))
            
            return self.commit_safe()
        except Exception as e:
            logger.error(f"Ошибка обновления активности {username}: {str(e)}")
            return False
    
    def log_connection(self, username, client_ip, connection_type="start", bytes_sent=0, bytes_received=0):
        """Логирование подключения/отключения пользователя"""
        try:
            if connection_type == "start":
                # Логируем начало подключения
                self.execute_safe(
                    "INSERT INTO user_stats (username, connection_start, client_ip, status) VALUES (?, CURRENT_TIMESTAMP, ?, 'active')",
                    (username, client_ip)
                )
                # Увеличиваем счетчик подключений
                self.execute_safe(
                    "UPDATE users SET total_connections = total_connections + 1 WHERE username = ?",
                    (username,)
                )
            elif connection_type == "end":
                # Находим активное подключение и завершаем его
                cursor = self.execute_safe(
                    "SELECT id, connection_start FROM user_stats WHERE username = ? AND status = 'active' ORDER BY connection_start DESC LIMIT 1",
                    (username,)
                )
                connection = cursor.fetchone()
                
                if connection:
                    connection_id, start_time = connection
                    duration = int((datetime.now() - datetime.fromisoformat(start_time)).total_seconds())
                    
                    self.execute_safe('''UPDATE user_stats 
                                      SET connection_end = CURRENT_TIMESTAMP, 
                                          duration_seconds = ?,
                                          bytes_sent = ?,
                                          bytes_received = ?,
                                          status = 'completed'
                                      WHERE id = ?''',
                                  (duration, bytes_sent, bytes_received, connection_id))
            
            # Обновляем ежедневную статистику
            self.update_daily_traffic(username, bytes_sent, bytes_received, connection_type)
            
            return self.commit_safe()
        except Exception as e:
            logger.error(f"Ошибка логирования подключения {username}: {str(e)}")
            return False
    
    def update_daily_traffic(self, username, bytes_sent, bytes_received, connection_type):
        """Обновление ежедневной статистики трафика"""
        try:
            today = datetime.now().date()
            
            if connection_type == "start":
                # При начале подключения увеличиваем счетчик подключений
                self.execute_safe('''INSERT OR REPLACE INTO traffic_log 
                                  (username, log_date, bytes_sent, bytes_received, connections_count)
                                  VALUES (?, ?, 
                                          COALESCE((SELECT bytes_sent FROM traffic_log WHERE username = ? AND log_date = ?), 0),
                                          COALESCE((SELECT bytes_received FROM traffic_log WHERE username = ? AND log_date = ?), 0),
                                          COALESCE((SELECT connections_count FROM traffic_log WHERE username = ? AND log_date = ?), 0) + 1
                                  )''',
                              (username, today, username, today, username, today, username, today))
            else:
                # При передаче данных обновляем трафик
                self.execute_safe('''INSERT OR REPLACE INTO traffic_log 
                                  (username, log_date, bytes_sent, bytes_received, connections_count)
                                  VALUES (?, ?, 
                                          COALESCE((SELECT bytes_sent FROM traffic_log WHERE username = ? AND log_date = ?), 0) + ?,
                                          COALESCE((SELECT bytes_received FROM traffic_log WHERE username = ? AND log_date = ?), 0) + ?,
                                          COALESCE((SELECT connections_count FROM traffic_log WHERE username = ? AND log_date = ?), 0)
                                  )''',
                              (username, today, username, today, bytes_sent, username, today, bytes_received, username, today))
            
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления ежедневной статистики {username}: {str(e)}")
            return False
    
    def get_user_statistics(self, username):
        """Получение статистики по конкретному пользователю"""
        try:
            # Основная статистика
            cursor = self.execute_safe(
                "SELECT total_connections, last_connected, total_bytes_sent, total_bytes_received, is_active FROM users WHERE username = ?",
                (username,)
            )
            user_stats = cursor.fetchone()
            
            if not user_stats:
                return None
            
            total_connections, last_connected, total_sent, total_received, is_active = user_stats
            
            # Статистика за последние 30 дней
            cursor = self.execute_safe(
                "SELECT SUM(bytes_sent), SUM(bytes_received), SUM(connections_count) FROM traffic_log WHERE username = ? AND log_date >= date('now', '-30 days')",
                (username,)
            )
            monthly_stats = cursor.fetchone()
            
            # Активные сессии
            cursor = self.execute_safe(
                "SELECT COUNT(*) FROM user_stats WHERE username = ? AND status = 'active'",
                (username,)
            )
            active_sessions = cursor.fetchone()[0] or 0
            
            return {
                'total_connections': total_connections or 0,
                'last_connected': last_connected,
                'total_bytes_sent': total_sent or 0,
                'total_bytes_received': total_received or 0,
                'is_active': bool(is_active),
                'active_sessions': active_sessions,
                'monthly_sent': monthly_stats[0] or 0,
                'monthly_received': monthly_stats[1] or 0,
                'monthly_connections': monthly_stats[2] or 0
            }
        except Exception as e:
            logger.error(f"Ошибка получения статистики пользователя {username}: {str(e)}")
            return None
    
    def get_all_users_stats(self):
        """Статистика по всем пользователям"""
        try:
            cursor = self.execute_safe('''SELECT username, total_connections, last_connected, 
                                       total_bytes_sent, total_bytes_received, is_active
                                       FROM users ORDER BY total_bytes_sent + total_bytes_received DESC''')
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Ошибка получения общей статистики: {str(e)}")
            return []
    
    def get_active_users_count(self):
        """Количество активных пользователей"""
        try:
            cursor = self.execute_safe("SELECT COUNT(*) FROM users WHERE is_active = 1")
            return cursor.fetchone()[0] or 0
        except Exception as e:
            logger.error(f"Ошибка получения количества активных пользователей: {str(e)}")
            return 0

# Утилиты
def validate_username(username):
    """Валидация имени пользователя"""
    if not username:
        return False, "Имя не может быть пустым"

    username = username.strip()

    if len(username) < Config.MIN_USERNAME_LENGTH:
        return False, f"Имя должно быть не менее {Config.MIN_USERNAME_LENGTH} символов"

    if len(username) > Config.MAX_USERNAME_LENGTH:
        return False, f"Имя должно быть не более {Config.MAX_USERNAME_LENGTH} символов"

    if not re.match(Config.USERNAME_PATTERN, username):
        return False, "Только латиница, цифры, _ и - без пробелов"

    return True, "OK"


def create_vpn_user(username):
    """Безопасное создание VPN пользователя"""
    try:
        safe_username = re.sub(r'[^a-zA-Z0-9_-]', '', username)

        command = [Config.IKEV2_SCRIPT_PATH, '--addclient', safe_username]
        logger.info(f"Выполнение команды: {' '.join(command)}")

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            logger.info(f"VPN пользователь {safe_username} успешно создан")

            # Проверяем создались ли файлы
            expected_files = [
                f"{Config.VPN_PROFILES_PATH}{safe_username}.mobileconfig",
                f"{Config.VPN_PROFILES_PATH}{safe_username}.p12",
                f"{Config.VPN_PROFILES_PATH}{safe_username}.sswan"
            ]

            created_files = []
            for file_path in expected_files:
                if os.path.exists(file_path):
                    created_files.append(os.path.basename(file_path))

            if created_files:
                logger.info(f"Созданные файлы: {', '.join(created_files)}")
            else:
                logger.warning(f"Файлы конфигурации не найдены в {Config.VPN_PROFILES_PATH}")

            return True, "Пользователь VPN создан успешно"
        else:
            error_msg = f"Ошибка создания VPN: {result.stderr}"
            logger.error(error_msg)
            return False, error_msg

    except subprocess.TimeoutExpired:
        error_msg = "Таймаут выполнения команды создания VPN"
        logger.error(error_msg)
        return False, error_msg
    except FileNotFoundError:
        error_msg = f"Скрипт {Config.IKEV2_SCRIPT_PATH} не найден"
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"Неожиданная ошибка: {str(e)}"
        logger.error(error_msg)
        return False, error_msg


def delete_vpn_user(username):
    """Безопасное удаление VPN пользователя с автоматическим подтверждением"""
    try:
        safe_username = re.sub(r'[^a-zA-Z0-9_-]', '', username)

        # Отзываем сертификат с автоматическим подтверждением (это также удалит конфигурации)
        revoke_command = [Config.IKEV2_SCRIPT_PATH, '--revokeclient', safe_username]
        logger.info(f"Выполнение команды отзыва: {' '.join(revoke_command)}")

        revoke_result = subprocess.run(
            revoke_command,
            capture_output=True,
            text=True,
            timeout=30,
            input='y\n'  # Автоматическое подтверждение
        )

        if revoke_result.returncode == 0:
            logger.info(f"VPN пользователь {safe_username} успешно отозван и удален")

            # Дополнительно проверяем и удаляем файлы конфигурации на всякий случай
            config_files = [
                f"{Config.VPN_PROFILES_PATH}{safe_username}.mobileconfig",
                f"{Config.VPN_PROFILES_PATH}{safe_username}.p12",
                f"{Config.VPN_PROFILES_PATH}{safe_username}.sswan"
            ]

            deleted_files = []
            for file_path in config_files:
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        deleted_files.append(os.path.basename(file_path))
                    except Exception as e:
                        logger.warning(f"Не удалось удалить файл {file_path}: {e}")

            if deleted_files:
                logger.info(f"Дополнительно удалены файлы: {', '.join(deleted_files)}")

            return True, "Пользователь VPN успешно удален"
        else:
            error_msg = f"Ошибка удаления VPN пользователя: {revoke_result.stderr}"
            logger.error(error_msg)
            return False, error_msg

    except subprocess.TimeoutExpired:
        error_msg = "Таймаут выполнения команды удаления VPN"
        logger.error(error_msg)
        return False, error_msg
    except FileNotFoundError:
        error_msg = f"Скрипт {Config.IKEV2_SCRIPT_PATH} не найден"
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"Неожиданная ошибка при удалении: {str(e)}"
        logger.error(error_msg)
        return False, error_msg


def parse_ipsec_trafficstatus():
    """ПРОСТОЙ И НАДЕЖНЫЙ ПАРСИНГ статистики трафика"""
    try:
        result = subprocess.run(['ipsec', 'trafficstatus'], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            logger.error(f"Ошибка выполнения ipsec trafficstatus: {result.stderr}")
            return {}

        traffic_data = {}
        lines = result.stdout.split('\n')

        logger.info(f"=== НАЧАЛО ПАРСИНГА ===")
        logger.info(f"Всего строк: {len(lines)}")

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            logger.info(f"Строка {i}: {line}")

            # Пропускаем строки без CN=
            if 'CN=' not in line:
                logger.info(f"Пропуск строки {i}: нет CN=")
                continue

            # Извлекаем имя пользователя
            username = None
            try:
                cn_match = re.search(r"CN=([^,]+)", line)
                if cn_match:
                    username = cn_match.group(1).strip()
                    logger.info(f"Найден username: {username}")
                else:
                    logger.info(f"Не удалось извлечь username из строки: {line}")
                    continue
            except Exception as e:
                logger.error(f"Ошибка извлечения username: {e}")
                continue

            # Извлекаем IP адрес
            client_ip = "unknown"
            try:
                ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                if ip_match:
                    client_ip = ip_match.group(1)
                    logger.info(f"Найден IP: {client_ip}")
            except Exception as e:
                logger.error(f"Ошибка извлечения IP: {e}")

            # Извлекаем ID подключения
            connection_id = "unknown"
            try:
                id_match = re.search(r'#(\d+):', line)
                if id_match:
                    connection_id = id_match.group(1)
                    logger.info(f"Найден ID: {connection_id}")
            except Exception as e:
                logger.error(f"Ошибка извлечения ID: {e}")

            # Извлекаем байты
            in_bytes = 0
            out_bytes = 0
            try:
                in_match = re.search(r'inBytes=(\d+)', line)
                if in_match:
                    in_bytes = int(in_match.group(1))
                    logger.info(f"Найден inBytes: {in_bytes}")

                out_match = re.search(r'outBytes=(\d+)', line)
                if out_match:
                    out_bytes = int(out_match.group(1))
                    logger.info(f"Найден outBytes: {out_bytes}")
            except Exception as e:
                logger.error(f"Ошибка извлечения байтов: {e}")

            # Сохраняем данные
            traffic_data[username] = {
                'bytes_received': in_bytes,
                'bytes_sent': out_bytes,
                'connection_id': connection_id,
                'client_ip': client_ip
            }

            logger.info(f"УСПЕХ: {username} -> IP={client_ip}, in={in_bytes}, out={out_bytes}")

        logger.info(f"=== ЗАВЕРШЕНИЕ ПАРСИНГА ===")
        logger.info(f"Найдено записей: {len(traffic_data)}")
        return traffic_data

    except Exception as e:
        logger.error(f"Критическая ошибка парсинга: {str(e)}")
        return {}


def update_connection_stats():
    """Обновление статистики подключений"""
    try:
        logger.info("=== НАЧАЛО ОБНОВЛЕНИЯ СТАТИСТИКИ ===")

        # Получаем активные подключения из trafficstatus
        traffic_data = parse_ipsec_trafficstatus()
        logger.info(f"Найдено записей в trafficstatus: {len(traffic_data)}")

        # Получаем всех пользователей из БД
        all_users = db.get_all_users()
        logger.info(f"Пользователей в БД: {len(all_users)}")

        active_usernames = set(traffic_data.keys())
        logger.info(f"Активные пользователи из trafficstatus: {active_usernames}")

        # Обновляем статусы всех пользователей
        updated_count = 0

        for user in all_users:
            user_id, username, created_by, created_by_username, created_at, total_conn, last_conn, sent, received, is_active = user

            if username in active_usernames:
                # Пользователь активен
                traffic_info = traffic_data[username]

                # Логируем подключение если его еще нет
                existing_active = db.execute_safe(
                    "SELECT id FROM user_stats WHERE username = ? AND status = 'active'",
                    (username,)
                ).fetchone()

                if not existing_active:
                    logger.info(f"Регистрируем новое подключение для {username}")
                    db.log_connection(username, traffic_info['client_ip'], "start")

                # Обновляем активность и трафик
                if db.update_user_activity(
                        username,
                        True,
                        traffic_info.get('bytes_sent', 0),
                        traffic_info.get('bytes_received', 0)
                ):
                    updated_count += 1
                    logger.info(
                        f"Обновлен пользователь {username}: активен, трафик in={traffic_info['bytes_received']}, out={traffic_info['bytes_sent']}")
                else:
                    logger.error(f"Ошибка обновления пользователя {username}")

            else:
                # Пользователь неактивен
                if is_active:
                    # Если был активен, но теперь нет - завершаем сессию
                    active_session = db.execute_safe(
                        "SELECT id FROM user_stats WHERE username = ? AND status = 'active'",
                        (username,)
                    ).fetchone()

                    if active_session:
                        logger.info(f"Завершаем сессию для {username}")
                        db.log_connection(username, "", "end")
                        db.update_user_activity(username, False)
                else:
                    # Просто обновляем время последнего обновления
                    db.update_user_activity(username, False)

        logger.info(f"=== ОБНОВЛЕНИЕ ЗАВЕРШЕНО. Обновлено пользователей: {updated_count} ===")
        return len(active_usernames)

    except Exception as e:
        logger.error(f"Ошибка обновления статистики: {str(e)}")
        return 0


def schedule_stats_update():
    """Планировщик периодического обновления статистики"""
    while True:
        try:
            active_count = update_connection_stats()
            logger.info(f"Плановое обновление статистики. Активных: {active_count}")
            time.sleep(60)  # Обновляем каждую минуту
        except Exception as e:
            logger.error(f"Ошибка в планировщике статистики: {str(e)}")
            time.sleep(30)


# Инициализация БД
try:
    db = UserDB()
    logger.info("БД успешно инициализирована")
except Exception as e:
    logger.critical(f"Критическая ошибка инициализации БД: {str(e)}")
    raise

# Запуск планировщика статистики
stats_thread = threading.Thread(target=schedule_stats_update, daemon=True)
stats_thread.start()


# Декораторы для проверки прав
def admin_required(func):
    def wrapper(message):
        user_id = message.from_user.id
        if not db.is_admin(user_id):
            logger.warning(f"Попытка доступа к команде {message.text} от неавторизованного пользователя {user_id}")
            bot.send_message(message.chat.id, "⛔ Доступ запрещен. Только для администраторов.")
            return
        return func(message)

    return wrapper


def super_admin_required(func):
    def wrapper(message):
        user_id = message.from_user.id
        if not db.is_super_admin(user_id):
            logger.warning(f"Попытка доступа к супер-админ команде {message.text} от пользователя {user_id}")
            bot.send_message(message.chat.id, "⛔ Доступ запрещен. Только для супер-администратора.")
            return
        return func(message)

    return wrapper


# Обработчики команд бота
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    logger.info(f"Команда /start от пользователя {user_id}")

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
/debugstats - Отладочная информация

👨‍💻 Админ-команды:
/admin - Панель администратора
/manage_admins - Управление администраторами
/deleteuser - Удалить пользователя
/dbclear - Очистить базу данных
/backup - Создать бэкап БД"""
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
/debugstats - Отладочная информация

👨‍💻 Админ-команды:
/admin - Панель администратора
/deleteuser - Удалить пользователя"""
    else:
        welcome_text = """🚀 VPN Manager Bot

У вас нет прав доступа к этому боту.
Обратитесь к администратору."""

    bot.send_message(message.chat.id, welcome_text)


@bot.message_handler(commands=['adduser'])
@admin_required
def add_user(message):
    logger.info(f"Команда /adduser от администратора {message.from_user.id}")

    name_prompt = bot.send_message(
        message.chat.id,
        'Введите имя пользователя (только латиница, цифры, _ и -):'
    )
    bot.register_next_step_handler(name_prompt, process_username)


def process_username(message):
    username = message.text.strip()
    user_id = message.from_user.id

    if not db.is_admin(user_id):
        logger.warning(f"Попытка создания пользователя от неавторизованного пользователя {user_id}")
        bot.send_message(message.chat.id, "⛔ Доступ запрещен")
        return

    is_valid, validation_msg = validate_username(username)

    if not is_valid:
        logger.warning(f"Невалидное имя {username} от администратора {user_id}: {validation_msg}")
        retry_msg = bot.send_message(
            message.chat.id,
            f"❌ {validation_msg}\n\nПопробуйте еще раз:"
        )
        bot.register_next_step_handler(retry_msg, process_username)
        return

    if db.user_exists(username):
        logger.warning(f"Попытка создать существующего пользователя {username}")
        retry_msg = bot.send_message(
            message.chat.id,
            f"❌ Пользователь '{username}' уже существует\nВведите другое имя:"
        )
        bot.register_next_step_handler(retry_msg, process_username)
        return

    bot.send_message(message.chat.id, f"⏳ Создаем пользователя '{username}'...")
    logger.info(f"Создание VPN пользователя {username} администратором {user_id}")

    success, result_msg = create_vpn_user(username)

    if not success:
        error_msg = f"❌ Не удалось создать пользователя: {result_msg}"
        bot.send_message(message.chat.id, error_msg)
        logger.error(f"Ошибка создания пользователя {username}: {result_msg}")
        return

    # Получаем информацию об администраторе
    admin_username = f"@{message.from_user.username}" if message.from_user.username else f"{message.from_user.first_name}"

    if db.add_user(username, user_id, admin_username):
        bot.send_message(message.chat.id, f"✅ Пользователь '{username}' успешно создан!")
        show_platform_selector(message, username)
    else:
        bot.send_message(
            message.chat.id,
            f"⚠️ VPN создан, но ошибка записи в БД. Файлы конфигурации доступны."
        )
        show_platform_selector(message, username)


def show_platform_selector(message, username):
    logger.info(f"Показ селектора платформ для пользователя {username}")

    ios_btn = telebot.types.InlineKeyboardButton("📱 iOS", callback_data=f'platform_ios_{username}')
    android_old_btn = telebot.types.InlineKeyboardButton("🤖 Android до v11", callback_data=f'platform_sswan_{username}')
    android_new_btn = telebot.types.InlineKeyboardButton("🤖 Android v11+", callback_data=f'platform_android_{username}')
    mac_btn = telebot.types.InlineKeyboardButton("💻 MacOS", callback_data=f'platform_macos_{username}')
    win_btn = telebot.types.InlineKeyboardButton("🪟 Windows", callback_data=f'platform_win_{username}')

    buttons = [
        [ios_btn, mac_btn],
        [android_old_btn, android_new_btn],
        [win_btn]
    ]

    markup = telebot.types.InlineKeyboardMarkup(buttons)
    bot.send_message(
        message.chat.id,
        f"Выберите платформу для установки VPN пользователя '{username}':",
        reply_markup=markup
    )


# Обработчики callback для выбора платформ
@bot.callback_query_handler(func=lambda call: call.data.startswith('platform_'))
def handle_platform_selection(call):
    try:
        user_id = call.from_user.id
        logger.info(f"Callback платформы от пользователя {user_id}: {call.data}")

        data_without_prefix = call.data[9:]

        if '_' not in data_without_prefix:
            logger.error(f"Неверный формат callback data: {call.data}")
            bot.answer_callback_query(call.id, "❌ Ошибка формата данных")
            return

        platform = data_without_prefix.split('_')[0]
        username = data_without_prefix[len(platform) + 1:]

        logger.info(f"Выбор платформы {platform} для пользователя {username} администратором {user_id}")

        platform_handlers = {
            'ios': send_ios_profile,
            'sswan': send_sswan_profile,
            'android': send_android_profile,
            'macos': send_macos_profile,
            'win': send_windows_profile
        }

        handler = platform_handlers.get(platform)
        if handler:
            handler(call, username)
            bot.answer_callback_query(call.id, f"📤 Отправляем конфиг для {platform}")
        else:
            logger.error(f"Неизвестная платформа: {platform}")
            bot.answer_callback_query(call.id, "❌ Неизвестная платформа")

    except Exception as e:
        logger.error(f"Ошибка обработки callback {call.data}: {str(e)}")
        bot.answer_callback_query(call.id, "❌ Ошибка обработки запроса")


def send_ios_profile(call, username):
    bot.send_message(call.message.chat.id, f"📱 Отправка профиля для iOS ({username})...")
    bot.send_message(call.message.chat.id, "<a href='https://telegra.ph/Testovaya-instrukciya-dlya-IOS-01-17'>Инструкция iOS</a>", parse_mode='HTML')
    try:
        file_path = f"{Config.VPN_PROFILES_PATH}{username}.mobileconfig"
        if os.path.exists(file_path):
            with open(file_path, 'rb') as file:
                bot.send_document(call.message.chat.id, file, caption="iOS профиль")
            logger.info(f"Файл {file_path} отправлен успешно")
        else:
            bot.send_message(call.message.chat.id, f"❌ Файл iOS профиль не найден по пути: {file_path}")
            logger.error(f"Файл не найден: {file_path}")
    except Exception as e:
        error_msg = f"❌ Ошибка отправки iOS профиля: {str(e)}"
        bot.send_message(call.message.chat.id, error_msg)
        logger.error(f"Ошибка отправки файла {file_path}: {str(e)}")

def send_android_profile(call, username):
    bot.send_message(call.message.chat.id, f"🤖 Отправка профиля для Android v11+ ({username})...")
    bot.send_message(call.message.chat.id, "<a href='https://telegra.ph/Instrukciya-Android-v11-01-17'>Инструкция Android</a>", parse_mode='HTML')
    try:
        file_path = f"{Config.VPN_PROFILES_PATH}{username}.p12"
        if os.path.exists(file_path):
            with open(file_path, 'rb') as file:
                bot.send_document(call.message.chat.id, file, caption="Android профиль")
            logger.info(f"Файл {file_path} отправлен успешно")
        else:
            bot.send_message(call.message.chat.id, f"❌ Файл Android профиль не найден по пути: {file_path}")
            logger.error(f"Файл не найден: {file_path}")
    except Exception as e:
        error_msg = f"❌ Ошибка отправки Android профиля: {str(e)}"
        bot.send_message(call.message.chat.id, error_msg)
        logger.error(f"Ошибка отправки файла {file_path}: {str(e)}")

def send_sswan_profile(call, username):
    bot.send_message(call.message.chat.id, f"🤖 Отправка профиля для StrongSwan ({username})...")
    bot.send_message(call.message.chat.id, "<a href='https://telegra.ph/Instrukciya-Android-do-11v-01-17'>Инструкция StrongSwan</a>", parse_mode='HTML')
    try:
        file_path = f"{Config.VPN_PROFILES_PATH}{username}.sswan"
        if os.path.exists(file_path):
            with open(file_path, 'rb') as file:
                bot.send_document(call.message.chat.id, file, caption="StrongSwan профиль")
            logger.info(f"Файл {file_path} отправлен успешно")
        else:
            bot.send_message(call.message.chat.id, f"❌ Файл StrongSwan профиль не найден по пути: {file_path}")
            logger.error(f"Файл не найден: {file_path}")
    except Exception as e:
        error_msg = f"❌ Ошибка отправки StrongSwan профиля: {str(e)}"
        bot.send_message(call.message.chat.id, error_msg)
        logger.error(f"Ошибка отправки файла {file_path}: {str(e)}")

def send_macos_profile(call, username):
    bot.send_message(call.message.chat.id, f"💻 Отправка профиля для MacOS ({username})...")
    bot.send_message(call.message.chat.id, "<a href='https://telegra.ph/Instrukciya-macOS-01-17'>Инструкция MacOS</a>", parse_mode='HTML')
    try:
        file_path = f"{Config.VPN_PROFILES_PATH}{username}.mobileconfig"
        if os.path.exists(file_path):
            with open(file_path, 'rb') as file:
                bot.send_document(call.message.chat.id, file, caption="MacOS профиль")
            logger.info(f"Файл {file_path} отправлен успешно")
        else:
            bot.send_message(call.message.chat.id, f"❌ Файл MacOS профиль не найден по пути: {file_path}")
            logger.error(f"Файл не найден: {file_path}")
    except Exception as e:
        error_msg = f"❌ Ошибка отправки MacOS профиля: {str(e)}"
        bot.send_message(call.message.chat.id, error_msg)
        logger.error(f"Ошибка отправки файла {file_path}: {str(e)}")

def send_windows_profile(call, username):
    bot.send_message(call.message.chat.id, f"🪟 Отправка профиля для Windows ({username})...")
    bot.send_message(call.message.chat.id, "<a href='https://telegra.ph/Instrukciya-dlya-Windows-01-17'>Инструкция Windows</a>", parse_mode='HTML')
    
    # Основной файл P12
    try:
        file_path = f"{Config.VPN_PROFILES_PATH}{username}.p12"
        if os.path.exists(file_path):
            with open(file_path, 'rb') as file:
                bot.send_document(call.message.chat.id, file, caption="Windows сертификат")
            logger.info(f"Файл {file_path} отправлен успешно")
        else:
            bot.send_message(call.message.chat.id, f"❌ Файл Windows сертификат не найден по пути: {file_path}")
            logger.error(f"Файл не найден: {file_path}")
    except Exception as e:
        error_msg = f"❌ Ошибка отправки Windows сертификата: {str(e)}"
        bot.send_message(call.message.chat.id, error_msg)
        logger.error(f"Ошибка отправки файла {file_path}: {str(e)}")
    
    # Дополнительные файлы для Windows
    additional_files = [
        ("Enable_Stronger_Ciphers_for_IKEv2_on_Windows.reg", "Реестр для включения сильных шифров"),
        ("ikev2_config_import.cmd", "CMD скрипт для импорта конфигурации")
    ]
    
    for filename, description in additional_files:
        try:
            file_path = f"{Config.VPN_PROFILES_PATH}{filename}"
            if os.path.exists(file_path):
                with open(file_path, 'rb') as file:
                    bot.send_document(call.message.chat.id, file, caption=description)
                logger.info(f"Файл {file_path} отправлен успешно")
            else:
                logger.warning(f"Дополнительный файл для Windows не найден: {file_path}")
        except Exception as e:
            logger.error(f"Ошибка отправки дополнительного файла {filename}: {str(e)}")

@bot.message_handler(commands=['listusers'])
@admin_required
def list_users(message):
    logger.info(f"Команда /listusers от администратора {message.from_user.id}")

    users = db.get_all_users()

    if not users:
        bot.send_message(message.chat.id, "📭 В базе данных нет пользователей")
        return

    user_list = "📋 Список пользователей:\n\n"
    for user in users:
        user_id, username, created_by, created_by_username, created_at, total_conn, last_conn, sent, received, is_active = user
        status = "🟢" if is_active else "⚪"
        user_list += f"{status} {username}\n"
        user_list += f"   Создан: {created_at[:10]} администратором {created_by_username}\n"
        if total_conn > 0:
            total_traffic = (sent or 0) + (received or 0)
            user_list += f"   Подключений: {total_conn}, трафик: {total_traffic / (1024 ** 3):.2f} GB\n"
        user_list += "\n"

    bot.send_message(message.chat.id, user_list)


@bot.message_handler(commands=['stats'])
@admin_required
def show_stats(message):
    user_id = message.from_user.id
    logger.info(f"Команда /stats от администратора {user_id}")

    total_users = db.get_user_count()

    # Принудительно обновляем статистику перед показом
    current_active = update_connection_stats()

    # Получаем детальную информацию о трафике
    traffic_data = parse_ipsec_trafficstatus()

    stats_text = f"""📊 Статистика VPN сервера

👥 Всего пользователей: {total_users}
🟢 Активных подключений: {current_active}
📊 Подключений в trafficstatus: {len(traffic_data)}

📁 Директория конфигов: {Config.VPN_PROFILES_PATH}
🕒 Время сервера: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🔄 Статистика обновлена: {datetime.now().strftime('%H:%M:%S')}"""

    # Добавляем информацию о конкретных подключениях
    if traffic_data:
        stats_text += "\n\n🔍 Активные пользователи:"
        for username, info in list(traffic_data.items())[:5]:  # Показываем первые 5
            traffic_mb = (info['bytes_sent'] + info['bytes_received']) / (1024 * 1024)
            stats_text += f"\n• {username}: {traffic_mb:.1f} MB"

    bot.send_message(message.chat.id, stats_text)


@bot.message_handler(commands=['syncstats'])
@admin_required
def sync_stats(message):
    """Принудительная синхронизация статистики"""
    user_id = message.from_user.id
    logger.info(f"Команда /syncstats от администратора {user_id}")

    bot.send_message(message.chat.id, "🔄 Синхронизация статистики...")

    # Показываем сырой вывод для диагностики
    try:
        result = subprocess.run(['ipsec', 'trafficstatus'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            # Отправляем первые несколько строк для диагностики
            lines = result.stdout.split('\n')[:5]
            diagnostic_info = "Диагностика - первые 5 строк trafficstatus:\n" + "\n".join(lines)
            bot.send_message(message.chat.id, f"```\n{diagnostic_info}\n```", parse_mode='Markdown')
        else:
            bot.send_message(message.chat.id, f"❌ Ошибка trafficstatus: {result.stderr}")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка диагностики: {str(e)}")

    # Синхронизируем статистику
    active_count = update_connection_stats()

    if active_count > 0:
        bot.send_message(message.chat.id, f"✅ Синхронизация завершена. Активных пользователей: {active_count}")
    else:
        bot.send_message(message.chat.id, "ℹ️ Активных подключений не найдено")


@bot.message_handler(commands=['debugstats'])
@admin_required
def debug_stats(message):
    """Детальная отладочная информация о статистике"""
    user_id = message.from_user.id
    logger.info(f"Команда /debugstats от администратора {user_id}")

    # Получаем сырые данные
    result = subprocess.run(['ipsec', 'trafficstatus'], capture_output=True, text=True, timeout=10)

    debug_text = "🔧 ДЕТАЛЬНАЯ ОТЛАДОЧНАЯ ИНФОРМАЦИЯ\n\n"

    if result.returncode == 0:
        debug_text += "📋 Полный вывод trafficstatus:\n"
        lines = result.stdout.split('\n')
        for i, line in enumerate(lines):
            debug_text += f"{i}: {line}\n"

        debug_text += "\n🔍 АНАЛИЗ СТРОК:\n"

        # Анализируем каждую строку
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            debug_text += f"\n--- Строка {i} ---\n"
            debug_text += f"Содержимое: {line}\n"

            # Проверяем наличие CN=
            if 'CN=' in line:
                debug_text += "✅ Содержит CN=\n"

                # Извлекаем username
                cn_match = re.search(r"CN=([^,]+)", line)
                if cn_match:
                    username = cn_match.group(1).strip()
                    debug_text += f"✅ Username: {username}\n"
                else:
                    debug_text += "❌ Не удалось извлечь username\n"

                # Извлекаем IP
                ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                if ip_match:
                    debug_text += f"✅ IP: {ip_match.group(1)}\n"
                else:
                    debug_text += "❌ Не удалось извлечь IP\n"

                # Извлекаем байты
                in_match = re.search(r'inBytes=(\d+)', line)
                out_match = re.search(r'outBytes=(\d+)', line)

                if in_match:
                    debug_text += f"✅ inBytes: {in_match.group(1)}\n"
                else:
                    debug_text += "❌ Не удалось извлечь inBytes\n"

                if out_match:
                    debug_text += f"✅ outBytes: {out_match.group(1)}\n"
                else:
                    debug_text += "❌ Не удалось извлечь outBytes\n"
            else:
                debug_text += "❌ Не содержит CN=\n"

        # Тестируем парсинг
        debug_text += "\n🎯 РЕЗУЛЬТАТ ПАРСИНГА:\n"
        traffic_data = parse_ipsec_trafficstatus()
        debug_text += f"Найдено записей: {len(traffic_data)}\n"

        for username, info in traffic_data.items():
            debug_text += f"• {username}: IP={info['client_ip']}, in={info['bytes_received']}, out={info['bytes_sent']}\n"

    else:
        debug_text += f"❌ Ошибка выполнения команды: {result.stderr}"

    # Разбиваем сообщение если слишком длинное
    if len(debug_text) > 4000:
        parts = [debug_text[i:i + 4000] for i in range(0, len(debug_text), 4000)]
        for part in parts:
            bot.send_message(message.chat.id, f"```{part}```", parse_mode='Markdown')
    else:
        bot.send_message(message.chat.id, f"```{debug_text}```", parse_mode='Markdown')


@bot.message_handler(commands=['activestats'])
@admin_required
def show_active_stats(message):
    """Показать активные подключения в реальном времени"""
    user_id = message.from_user.id
    logger.info(f"Команда /activestats от администратора {user_id}")

    # Получаем свежие данные
    active_connections = parse_ipsec_trafficstatus()

    if not active_connections:
        bot.send_message(message.chat.id, "📭 Нет активных подключений")
        return

    stats_text = "🟢 Активные подключения:\n\n"

    for username, conn_info in active_connections.items():
        bytes_sent = conn_info.get('bytes_sent', 0)
        bytes_received = conn_info.get('bytes_received', 0)
        total_traffic = (bytes_sent + bytes_received) / (1024 ** 2)  # в MB

        stats_text += f"👤 {username}\n"
        stats_text += f"   IP: {conn_info['client_ip']}\n"
        stats_text += f"   ID: {conn_info['connection_id']}\n"
        stats_text += f"   Трафик: {total_traffic:.2f} MB\n\n"

    stats_text += f"Всего активных: {len(active_connections)}"

    bot.send_message(message.chat.id, stats_text)


@bot.message_handler(commands=['userstats'])
@admin_required
def user_stats(message):
    logger.info(f"Команда /userstats от администратора {message.from_user.id}")

    users = db.get_all_users()
    if not users:
        bot.send_message(message.chat.id, "📭 В базе данных нет пользователей")
        return

    buttons = []
    for user in users:
        user_id, username, created_by, created_by_username, created_at, total_conn, last_conn, sent, received, is_active = user
        status = "🟢" if is_active else "⚪"
        buttons.append([telebot.types.InlineKeyboardButton(
            f"{status} {username}",
            callback_data=f'userstats_{username}'
        )])

    markup = telebot.types.InlineKeyboardMarkup(buttons)
    bot.send_message(message.chat.id, "Выберите пользователя для просмотра статистики:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('userstats_'))
def handle_user_stats(call):
    user_id = call.from_user.id
    username = call.data.replace('userstats_', '')

    if not db.is_admin(user_id):
        bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
        return

    stats = db.get_user_statistics(username)
    if not stats:
        bot.send_message(call.message.chat.id, f"❌ Статистика для пользователя '{username}' не найдена")
        bot.answer_callback_query(call.id, "❌ Статистика не найдена")
        return

    # Форматируем данные
    total_traffic_gb = (stats['total_bytes_sent'] + stats['total_bytes_received']) / (1024 ** 3)
    monthly_traffic_gb = (stats['monthly_sent'] + stats['monthly_received']) / (1024 ** 3)

    status_icon = "🟢" if stats['is_active'] else "⚪"
    active_text = "активен" if stats['is_active'] else "неактивен"

    stats_text = f"""📊 Статистика пользователя: {username}

{status_icon} Статус: {active_text}
📈 Активных сессий: {stats['active_sessions']}

📊 Основные метрики:
• Всего подключений: {stats['total_connections']}
• Последнее подключение: {stats['last_connected'] or 'Никогда'}
• Всего трафика: {total_traffic_gb:.2f} GB

📅 За последние 30 дней:
• Подключений: {stats['monthly_connections']}
• Трафик: {monthly_traffic_gb:.2f} GB
• Отправлено: {stats['monthly_sent'] / (1024 ** 3):.2f} GB
• Получено: {stats['monthly_received'] / (1024 ** 3):.2f} GB

📈 Детали трафика:
• Всего отправлено: {stats['total_bytes_sent'] / (1024 ** 3):.2f} GB
• Всего получено: {stats['total_bytes_received'] / (1024 ** 3):.2f} GB"""

    bot.send_message(call.message.chat.id, stats_text)
    bot.answer_callback_query(call.id, f"📊 Статистика {username}")


@bot.message_handler(commands=['traffic'])
@admin_required
def traffic_stats(message):
    logger.info(f"Команда /traffic от администратора {message.from_user.id}")

    all_stats = db.get_all_users_stats()
    if not all_stats:
        bot.send_message(message.chat.id, "📭 Нет данных о трафике")
        return

    # Сортируем по трафику (убывание)
    all_stats_sorted = sorted(all_stats, key=lambda x: (x[3] or 0) + (x[4] or 0), reverse=True)

    stats_text = "📊 Общая статистика трафика\n\n"
    total_traffic_all = 0

    for user_stats in all_stats_sorted[:10]:  # Показываем топ-10
        username, total_conn, last_conn, sent, received, is_active = user_stats
        total_traffic = (sent or 0) + (received or 0)
        total_traffic_all += total_traffic

        if total_traffic > 0:
            status = "🟢" if is_active else "⚪"
            stats_text += f"{status} {username}:\n"
            stats_text += f"   • Подключений: {total_conn or 0}\n"
            stats_text += f"   • Трафик: {total_traffic / (1024 ** 3):.2f} GB\n"
            stats_text += f"   • Активность: {last_conn[:10] if last_conn else 'Никогда'}\n\n"

    stats_text += f"📈 Всего трафика: {total_traffic_all / (1024 ** 3):.2f} GB"

    bot.send_message(message.chat.id, stats_text)


# Команды управления администраторами
@bot.message_handler(commands=['manage_admins'])
@super_admin_required
def manage_admins(message):
    user_id = message.from_user.id
    logger.info(f"Команда /manage_admins от супер-администратора {user_id}")

    buttons = [
        [telebot.types.InlineKeyboardButton("👥 Список админов", callback_data='admin_list')],
        [telebot.types.InlineKeyboardButton("➕ Добавить админа", callback_data='admin_add')],
        [telebot.types.InlineKeyboardButton("➖ Удалить админа", callback_data='admin_remove')]
    ]

    markup = telebot.types.InlineKeyboardMarkup(buttons)
    bot.send_message(message.chat.id, "👑 Управление администраторами", reply_markup=markup)


@bot.message_handler(commands=['admin'])
@admin_required
def admin_panel(message):
    user_id = message.from_user.id
    logger.info(f"Открытие админ-панели администратором {user_id}")

    if db.is_super_admin(user_id):
        buttons = [
            [telebot.types.InlineKeyboardButton("📊 Статистика", callback_data='admin_stats')],
            [telebot.types.InlineKeyboardButton("🔄 Перезапустить VPN", callback_data='admin_restart')],
            [telebot.types.InlineKeyboardButton("💾 Бэкап БД", callback_data='admin_backup')],
            [telebot.types.InlineKeyboardButton("🧹 Очистить БД", callback_data='admin_clear_db')],
            [telebot.types.InlineKeyboardButton("👑 Управление админами", callback_data='admin_manage')]
        ]
    else:
        buttons = [
            [telebot.types.InlineKeyboardButton("📊 Статистика", callback_data='admin_stats')],
            [telebot.types.InlineKeyboardButton("🔄 Перезапустить VPN", callback_data='admin_restart')],
            [telebot.types.InlineKeyboardButton("💾 Бэкап БД", callback_data='admin_backup')]
        ]

    markup = telebot.types.InlineKeyboardMarkup(buttons)
    bot.send_message(message.chat.id, "👨‍💻 Панель администратора", reply_markup=markup)


# Обработчики callback для админ-панели
@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_'))
def handle_admin_actions(call):
    user_id = call.from_user.id
    logger.info(f"Админ действие от пользователя {user_id}: {call.data}")

    if not db.is_admin(user_id):
        logger.warning(f"Попытка доступа к админ-панели от неавторизованного пользователя {user_id}")
        bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
        return

    action = call.data

    if action == 'admin_stats':
        show_stats(call.message)
        bot.answer_callback_query(call.id, "📊 Статистика обновлена")

    elif action == 'admin_restart':
        bot.send_message(call.message.chat.id, "🔄 Перезапуск VPN службы...")
        try:
            subprocess.run(['systemctl', 'restart', 'strongswan'], check=True)
            bot.send_message(call.message.chat.id, "✅ StrongSwan перезапущен")
        except subprocess.CalledProcessError:
            bot.send_message(call.message.chat.id, "❌ Ошибка перезапуска StrongSwan")
        bot.answer_callback_query(call.id, "🔄 Перезапуск")

    elif action == 'admin_backup':
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = f"backup_users_{timestamp}.db"
            shutil.copy2(Config.DB_PATH, backup_file)

            bot.send_document(call.message.chat.id, open(backup_file, 'rb'), caption="💾 Бэкап базы данных")
            bot.send_message(call.message.chat.id, "✅ Бэкап создан успешно")

            os.remove(backup_file)

        except Exception as e:
            error_msg = f"❌ Ошибка создания бэкапа: {str(e)}"
            bot.send_message(call.message.chat.id, error_msg)
            logger.error(f"Ошибка бэкапа: {str(e)}")
        bot.answer_callback_query(call.id, "💾 Бэкап создан")

    elif action == 'admin_clear_db':
        buttons = [
            [telebot.types.InlineKeyboardButton("✅ Да, очистить", callback_data='confirm_clear')],
            [telebot.types.InlineKeyboardButton("❌ Отмена", callback_data='cancel_clear')]
        ]
        markup = telebot.types.InlineKeyboardMarkup(buttons)
        bot.send_message(
            call.message.chat.id,
            "⚠️ ВНИМАНИЕ! Вы уверены что хотите очистить всю базу данных?\nЭто действие нельзя отменить!",
            reply_markup=markup
        )
        bot.answer_callback_query(call.id, "🧹 Подтвердите очистку")

    elif action == 'admin_manage':
        if db.is_super_admin(user_id):
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
                [telebot.types.InlineKeyboardButton("📝 Ввести ID вручную", callback_data='add_manual')],
                [telebot.types.InlineKeyboardButton("🔗 Переслать сообщение", callback_data='add_forward')],
                [telebot.types.InlineKeyboardButton("❌ Отмена", callback_data='add_cancel')]
            ]
            markup = telebot.types.InlineKeyboardMarkup(buttons)
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
                buttons.append([telebot.types.InlineKeyboardButton(
                    f"🗑️ {username} (ID: {admin_id})",
                    callback_data=f'remove_admin_{admin_id}'
                )])

            markup = telebot.types.InlineKeyboardMarkup(buttons)
            bot.send_message(call.message.chat.id, "Выберите администратора для удаления:", reply_markup=markup)
            bot.answer_callback_query(call.id, "➖ Удаление админа")
        else:
            bot.answer_callback_query(call.id, "⛔ Только для супер-админа")


# Обработчики для способов добавления админа
@bot.callback_query_handler(func=lambda call: call.data.startswith('add_'))
def handle_add_methods(call):
    user_id = call.from_user.id

    if not db.is_super_admin(user_id):
        bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
        return

    method = call.data

    if method == 'add_manual':
        msg = bot.send_message(call.message.chat.id, "Введите ID пользователя для добавления в администраторы:")
        bot.register_next_step_handler(msg, process_add_admin_manual)
        bot.answer_callback_query(call.id, "📝 Ввод ID")

    elif method == 'add_forward':
        msg = bot.send_message(
            call.message.chat.id,
            "Перешлите любое сообщение от пользователя, которого хотите добавить в администраторы.\n\n"
            "ℹ️ Как это сделать:\n"
            "1. Найдите пользователя в чатах\n"
            "2. Нажмите на его сообщение\n"
            "3. Выберите 'Переслать'\n"
            "4. Выберите этого бота\n\n"
            "Или отправьте /cancel для отмены."
        )
        bot.register_next_step_handler(msg, process_add_admin_forward)
        bot.answer_callback_query(call.id, "🔗 Перешлите сообщение")

    elif method == 'add_cancel':
        bot.send_message(call.message.chat.id, "❌ Добавление админа отменено")
        bot.answer_callback_query(call.id, "❌ Отменено")


def process_add_admin_manual(message):
    if message.text and message.text.startswith('/'):
        bot.send_message(message.chat.id, "❌ Добавление админа отменено")
        return

    try:
        new_admin_id = int(message.text.strip())

        try:
            user_info = bot.get_chat(new_admin_id)
            username = f"@{user_info.username}" if user_info.username else f"{user_info.first_name}" + (
                f" {user_info.last_name}" if user_info.last_name else "")
        except:
            username = f"Пользователь {new_admin_id}"

        if db.add_admin(new_admin_id, username, Config.SUPER_ADMIN_ID):
            bot.send_message(message.chat.id,
                             f"✅ Пользователь {username} (ID: {new_admin_id}) добавлен в администраторы")

            try:
                bot.send_message(new_admin_id,
                                 "🎉 Вас добавили в администраторы VPN бота!\n\nИспользуйте /start для просмотра доступных команд.")
            except:
                logger.info(f"Не удалось отправить уведомление новому администратору {new_admin_id}")
        else:
            bot.send_message(message.chat.id,
                             f"❌ Не удалось добавить пользователя в администраторы (возможно, уже является админом)")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверный формат ID. Введите числовой ID.")


def process_add_admin_forward(message):
    if message.text and message.text.startswith('/cancel'):
        bot.send_message(message.chat.id, "❌ Добавление админа отменено")
        return

    if not message.forward_from:
        bot.send_message(message.chat.id, "❌ Не удалось получить информацию о пользователе. Убедитесь, что:\n\n"
                                          "• Пользователь не скрыл свой профиль\n"
                                          "• Вы переслали сообщение, а не скопировали текст\n"
                                          "• Попробуйте другой способ добавления")
        return

    forward_from = message.forward_from
    user_id = forward_from.id
    username = f"@{forward_from.username}" if forward_from.username else f"{forward_from.first_name}" + (
        f" {forward_from.last_name}" if forward_from.last_name else "")

    if db.add_admin(user_id, username, Config.SUPER_ADMIN_ID):
        bot.send_message(message.chat.id, f"✅ Пользователь {username} (ID: {user_id}) добавлен в администраторы")

        try:
            bot.send_message(user_id,
                             "🎉 Вас добавили в администраторы VPN бота!\n\nИспользуйте /start для просмотра доступных команд.")
        except:
            logger.info(f"Не удалось отправить уведомление новому администратору {user_id}")
    else:
        bot.send_message(message.chat.id,
                         f"❌ Не удалось добавить пользователя в администраторы (возможно, уже является админом)")


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

            try:
                bot.send_message(admin_id_to_remove, "ℹ️ Ваши права администратора в VPN боте были отозваны.")
            except:
                logger.info(f"Не удалось отправить уведомление удаленному администратору {admin_id_to_remove}")
        else:
            bot.send_message(call.message.chat.id, f"❌ Ошибка удаления администратора")

        bot.answer_callback_query(call.id, "✅ Админ удален")

    except ValueError:
        bot.answer_callback_query(call.id, "❌ Ошибка формата ID")


@bot.message_handler(commands=['deleteuser'])
@admin_required
def delete_user(message):
    user_id = message.from_user.id
    logger.info(f"Команда /deleteuser от администратора {user_id}")

    # Если супер-админ - показывает всех пользователей
    # Если обычный админ - показывает только своих пользователей
    if db.is_super_admin(user_id):
        users = db.get_all_users()
    else:
        users = db.get_users_by_admin(user_id)

    if not users:
        if db.is_super_admin(user_id):
            bot.send_message(message.chat.id, "❌ В базе данных нет пользователей для удаления")
        else:
            bot.send_message(message.chat.id, "❌ У вас нет созданных пользователей для удаления")
        return

    buttons = []
    for user in users:
        user_id_db, username, created_by, created_by_username, created_at, total_conn, last_conn, sent, received, is_active = user

        # Для супер-админа показываем кто создал пользователя
        if db.is_super_admin(user_id):
            button_text = f"🗑️ {username} (создал: {created_by_username})"
        else:
            button_text = f"🗑️ {username}"

        buttons.append([telebot.types.InlineKeyboardButton(
            button_text,
            callback_data=f'delete_{username}'
        )])

    markup = telebot.types.InlineKeyboardMarkup(buttons)

    if db.is_super_admin(user_id):
        bot.send_message(message.chat.id, "Выберите пользователя для удаления:", reply_markup=markup)
    else:
        bot.send_message(message.chat.id, "Выберите пользователя для удаления (только ваши пользователи):",
                         reply_markup=markup)


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
        user_creator = db.execute_safe("SELECT created_by FROM users WHERE username = ?", (username,)).fetchone()
        if not user_creator or user_creator[0] != user_id:
            bot.send_message(call.message.chat.id, f"❌ Вы можете удалять только своих пользователей")
            bot.answer_callback_query(call.id, "❌ Нет прав на удаление")
            return

    # Показываем уведомление о начале удаления
    bot.answer_callback_query(call.id, "⏳ Начинаем удаление...")

    bot.send_message(call.message.chat.id, f"⏳ Удаляем пользователя '{username}'...")

    # Удаляем пользователя из VPN системы
    success, result_msg = delete_vpn_user(username)

    if not success:
        error_msg = f"❌ Ошибка удаления VPN пользователя: {result_msg}"
        bot.send_message(call.message.chat.id, error_msg)
        return

    # Удаляем пользователя из БД
    if db.delete_user(username):
        bot.send_message(call.message.chat.id, f"✅ Пользователь '{username}' полностью удален из системы")
        logger.info(f"Пользователь {username} удален администратором {user_id}")
    else:
        bot.send_message(call.message.chat.id, f"⚠️ VPN пользователь удален, но ошибка удаления из БД")


@bot.message_handler(commands=['dbclear'])
@admin_required
def clear_database(message):
    user_id = message.from_user.id

    logger.warning(f"Очистка БД инициирована администратором {user_id}")

    buttons = [
        [telebot.types.InlineKeyboardButton("✅ Да, очистить", callback_data='confirm_clear')],
        [telebot.types.InlineKeyboardButton("❌ Отмена", callback_data='cancel_clear')]
    ]
    markup = telebot.types.InlineKeyboardMarkup(buttons)
    bot.send_message(
        message.chat.id,
        "⚠️ ВНИМАНИЕ! Вы уверены что хотите очистить всю базу данных?\nЭто действие нельзя отменить!",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data in ['confirm_clear', 'cancel_clear'])
def handle_clear_confirmation(call):
    user_id = call.from_user.id

    if not db.is_admin(user_id):
        bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
        return

    if call.data == 'confirm_clear':
        if db.clear_all_users():
            bot.send_message(call.message.chat.id, "✅ База данных очищена")
            logger.warning(f"БД очищена администратором {user_id}")
            bot.answer_callback_query(call.id, "✅ БД очищена")
        else:
            bot.send_message(call.message.chat.id, "❌ Ошибка очистки базы данных")
            bot.answer_callback_query(call.id, "❌ Ошибка очистки")
    else:
        bot.send_message(call.message.chat.id, "❌ Очистка отменена")
        bot.answer_callback_query(call.id, "❌ Отменено")


@bot.message_handler(commands=['backup'])
@admin_required
def backup_database(message):
    user_id = message.from_user.id

    logger.info(f"Создание бэкапа БД администратором {user_id}")

    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"backup_users_{timestamp}.db"
        shutil.copy2(Config.DB_PATH, backup_file)

        bot.send_document(message.chat.id, open(backup_file, 'rb'), caption="💾 Бэкап базы данных")
        bot.send_message(message.chat.id, "✅ Бэкап создан успешно")

        os.remove(backup_file)

    except Exception as e:
        error_msg = f"❌ Ошибка создания бэкапа: {str(e)}"
        bot.send_message(message.chat.id, error_msg)
        logger.error(f"Ошибка бэкапа: {str(e)}")


# Обработка ошибок
@bot.message_handler(func=lambda message: True)
def handle_unknown_commands(message):
    user_id = message.from_user.id
    logger.info(f"Неизвестная команда от пользователя {user_id}: {message.text}")

    if db.is_admin(user_id):
        bot.send_message(
            message.chat.id,
            "❓ Неизвестная команда. Используйте /start для просмотра доступных команд."
        )
    else:
        bot.send_message(
            message.chat.id,
            "⛔ У вас нет доступа к этому боту. Обратитесь к администратору."
        )


def main():
    logger.info("Запуск VPN Manager Bot с исправленным парсингом и улучшенным управлением пользователями...")

    try:
        print("🚀 VPN Manager Bot запущен...")
        print(f"📁 Конфигурации в: {Config.VPN_PROFILES_PATH}")
        print(f"👑 Супер-админ ID: {Config.SUPER_ADMIN_ID}")
        print(f"🔧 ИСПРАВЛЕННЫЙ парсинг активирован")
        print(f"🗑️  Полное удаление пользователей (revoke + delete)")
        print(f"👤 Отслеживание создателей пользователей")
        print(f"⏱️  Мониторинг активных подключений: каждые 60 секунд")
        print("=" * 50)
        print("Для диагностики используйте команды:")
        print("/debugstats - детальная отладочная информация")
        print("/syncstats - принудительная синхронизация")
        print("/activestats - активные подключения")
        print("=" * 50)

        bot.polling(none_stop=True)

    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {str(e)}")
        print(f"❌ Критическая ошибка: {str(e)}")
    finally:
        pass


if __name__ == "__main__":
    main()
