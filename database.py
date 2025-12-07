import sqlite3
import logging
import time
import json
import shutil
import hashlib
from datetime import datetime, timedelta
from sqlite3 import OperationalError
from pathlib import Path
from config import Config

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.db_path = Config.DB_PATH
        self.backup_dir = Config.BACKUP_DIR
        self.max_retries = 5
        self.retry_delay = 1
        self.conn = self._create_connection()
        self._create_tables()

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
                # Временно отключаем foreign keys для совместимости
                conn.execute("PRAGMA foreign_keys = OFF")
                logger.info("Соединение с БД установлено")
                return conn
            except OperationalError as e:
                if "locked" in str(e) and attempt < self.max_retries - 1:
                    logger.warning(f"БД заблокирована, попытка {attempt + 1}/{self.max_retries}")
                    time.sleep(self.retry_delay)
                else:
                    logger.error(f"Не удалось подключиться к БД: {str(e)}")
                    raise
        raise OperationalError("Не удалось подключиться к БД после нескольких попыток")

    def execute(self, query, params=()):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return cursor

    def commit(self):
        self.conn.commit()

    def _create_tables(self):
        """Создание всех необходимых таблиц с проверкой существующих"""
        try:
            # Отключаем foreign keys для совместимости
            self.execute("PRAGMA foreign_keys = OFF")

            # Получаем список существующих таблиц
            cursor = self.execute("SELECT name FROM sqlite_master WHERE type='table'")
            existing_tables = [row[0] for row in cursor.fetchall()]
            logger.info(f"Существующие таблицы: {existing_tables}")

            # Таблица users (основная)
            if 'users' not in existing_tables:
                logger.info("Создаем таблицу users")
                self.execute('''CREATE TABLE users (
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
            else:
                # Проверяем и добавляем недостающие колонки
                cursor = self.execute("PRAGMA table_info(users)")
                columns = [col[1] for col in cursor.fetchall()]

                if 'last_updated' not in columns:
                    logger.info("Добавляем колонку last_updated в users")
                    try:
                        self.execute("ALTER TABLE users ADD COLUMN last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                    except:
                        pass  # Колонка может уже существовать

            # Таблица user_stats с session_id
            if 'user_stats' not in existing_tables:
                logger.info("Создаем таблицу user_stats с session_id")
                self.execute('''CREATE TABLE user_stats (
                              id INTEGER PRIMARY KEY AUTOINCREMENT,
                              username TEXT NOT NULL,
                              connection_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                              connection_end TIMESTAMP,
                              duration_seconds INTEGER,
                              bytes_sent BIGINT DEFAULT 0,
                              bytes_received BIGINT DEFAULT 0,
                              client_ip TEXT,
                              status TEXT DEFAULT 'completed',
                              session_id TEXT
                           )''')
            else:
                # Проверяем есть ли session_id
                cursor = self.execute("PRAGMA table_info(user_stats)")
                columns = [col[1] for col in cursor.fetchall()]

                if 'session_id' not in columns:
                    logger.info("Добавляем колонку session_id в user_stats")
                    try:
                        self.execute("ALTER TABLE user_stats ADD COLUMN session_id TEXT")
                    except:
                        pass

            # Остальные таблицы создаем если их нет
            tables_to_create = [
                ('admins', '''CREATE TABLE IF NOT EXISTS admins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL,
                    username TEXT NOT NULL,
                    added_by INTEGER NOT NULL,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )'''),

                ('traffic_log', '''CREATE TABLE IF NOT EXISTS traffic_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    log_date DATE NOT NULL,
                    bytes_sent BIGINT DEFAULT 0,
                    bytes_received BIGINT DEFAULT 0,
                    connections_count INTEGER DEFAULT 0,
                    UNIQUE(username, log_date)
                )'''),

                ('active_sessions', '''CREATE TABLE IF NOT EXISTS active_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    session_hash TEXT NOT NULL,
                    last_bytes_sent BIGINT DEFAULT 0,
                    last_bytes_received BIGINT DEFAULT 0,
                    client_ip TEXT,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(username, connection_id, session_hash)
                )'''),

                ('session_backup', '''CREATE TABLE IF NOT EXISTS session_backup (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    session_hash TEXT NOT NULL,
                    total_bytes_sent BIGINT DEFAULT 0,
                    total_bytes_received BIGINT DEFAULT 0,
                    start_time TIMESTAMP,
                    end_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    backup_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    backup_reason TEXT
                )''')
            ]

            for table_name, create_sql in tables_to_create:
                if table_name not in existing_tables:
                    logger.info(f"Создаем таблицу {table_name}")
                    self.execute(create_sql)

            # Добавляем супер-админа если нет
            self.execute("INSERT OR IGNORE INTO admins (user_id, username, added_by) VALUES (?, ?, ?)",
                         (Config.SUPER_ADMIN_ID, "Супер-админ", Config.SUPER_ADMIN_ID))

            self.commit()
            logger.info("Все таблицы созданы/проверены")

            # Включаем foreign keys обратно
            self.execute("PRAGMA foreign_keys = ON")

        except Exception as e:
            logger.error(f"Ошибка при создании таблиц: {str(e)}")
            # Оставляем foreign_keys отключенными для совместимости
            try:
                self.execute("PRAGMA foreign_keys = OFF")
            except:
                pass
            raise

    def ensure_user_exists(self, username):
        """Гарантирует что пользователь существует в БД"""
        try:
            cursor = self.execute("SELECT id FROM users WHERE username = ?", (username,))
            if cursor.fetchone() is None:
                # Пользователя нет - создаем
                logger.warning(f"Пользователь {username} не найден в БД, создаем...")
                self.execute(
                    "INSERT INTO users (username, created_by, created_by_username) VALUES (?, ?, ?)",
                    (username, Config.SUPER_ADMIN_ID, "Система (автосоздание)")
                )
                self.commit()
                logger.info(f"Пользователь {username} создан в БД")
                return True
            return True
        except Exception as e:
            logger.error(f"Ошибка проверки/создания пользователя {username}: {str(e)}")
            return False

    def update_active_session(self, username, connection_id, client_ip, current_sent, current_received):
        """Обновляет или создает активную сессию с проверкой пользователя"""
        try:
            # Сначала гарантируем что пользователь существует
            if not self.ensure_user_exists(username):
                return 0, 0, None

            # Создаем хэш сессии
            import hashlib
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            data = f"{username}_{connection_id}_{client_ip}_{timestamp}"
            session_hash = hashlib.md5(data.encode()).hexdigest()[:16]

            # Получаем предыдущие значения
            cursor = self.execute(
                "SELECT session_hash, last_bytes_sent, last_bytes_received FROM active_sessions WHERE username = ? AND connection_id = ?",
                (username, connection_id)
            )
            previous = cursor.fetchone()

            bytes_sent_diff = 0
            bytes_received_diff = 0

            if previous:
                prev_hash, prev_sent, prev_received = previous

                if prev_hash != session_hash:
                    # Новая сессия
                    self.finalize_session(username, connection_id, prev_hash, "session_reconnected")
                    bytes_sent_diff = current_sent
                    bytes_received_diff = current_received
                else:
                    # Та же сессия
                    bytes_sent_diff = max(0, current_sent - prev_sent)
                    bytes_received_diff = max(0, current_received - prev_received)

                # Обновляем запись
                self.execute('''UPDATE active_sessions 
                             SET last_bytes_sent = ?,
                                 last_bytes_received = ?,
                                 client_ip = ?,
                                 last_updated = CURRENT_TIMESTAMP
                             WHERE username = ? AND connection_id = ? AND session_hash = ?''',
                             (current_sent, current_received, client_ip, username, connection_id, prev_hash))
            else:
                # Новая сессия
                bytes_sent_diff = current_sent
                bytes_received_diff = current_received

                self.execute('''INSERT INTO active_sessions 
                             (username, connection_id, session_hash, last_bytes_sent, last_bytes_received, client_ip)
                             VALUES (?, ?, ?, ?, ?, ?)''',
                             (username, connection_id, session_hash, current_sent, current_received, client_ip))

                # Регистрируем начало подключения
                try:
                    self.execute('''INSERT INTO user_stats 
                                 (username, connection_start, client_ip, status, session_id)
                                 VALUES (?, CURRENT_TIMESTAMP, ?, 'active', ?)''',
                                 (username, client_ip, session_hash))
                except Exception as e:
                    # Если session_id нет в таблице, вставляем без него
                    logger.warning(f"Ошибка вставки с session_id: {e}, вставляем без него")
                    self.execute('''INSERT INTO user_stats 
                                 (username, connection_start, client_ip, status)
                                 VALUES (?, CURRENT_TIMESTAMP, ?, 'active')''',
                                 (username, client_ip))

                # Увеличиваем счетчик подключений
                self.execute('''UPDATE users 
                             SET total_connections = total_connections + 1,
                                 last_connected = CURRENT_TIMESTAMP,
                                 is_active = 1
                             WHERE username = ?''',
                             (username,))

            self.commit()
            return bytes_sent_diff, bytes_received_diff, session_hash

        except Exception as e:
            logger.error(f"Ошибка обновления сессии {username}: {str(e)}")
            return 0, 0, None

    # ... остальные методы остаются ...


# Глобальный экземпляр БД
db = Database()