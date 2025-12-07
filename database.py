import sqlite3
import logging
import time
import json
import shutil
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
                conn.execute("PRAGMA foreign_keys = ON")
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
        """Создание всех необходимых таблиц"""
        try:
            # Таблица пользователей
            self.execute('''CREATE TABLE IF NOT EXISTS users (
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

            # Таблица администраторов
            self.execute('''CREATE TABLE IF NOT EXISTS admins (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER UNIQUE NOT NULL,
                          username TEXT NOT NULL,
                          added_by INTEGER NOT NULL,
                          added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                       )''')

            # Таблица детальной статистики
            self.execute('''CREATE TABLE IF NOT EXISTS user_stats (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          username TEXT NOT NULL,
                          connection_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                          connection_end TIMESTAMP,
                          duration_seconds INTEGER,
                          bytes_sent BIGINT DEFAULT 0,
                          bytes_received BIGINT DEFAULT 0,
                          client_ip TEXT,
                          status TEXT DEFAULT 'completed',
                          session_id TEXT,
                          FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
                       )''')

            # Таблица ежедневной статистики
            self.execute('''CREATE TABLE IF NOT EXISTS traffic_log (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          username TEXT NOT NULL,
                          log_date DATE NOT NULL,
                          bytes_sent BIGINT DEFAULT 0,
                          bytes_received BIGINT DEFAULT 0,
                          connections_count INTEGER DEFAULT 0,
                          UNIQUE(username, log_date),
                          FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
                       )''')

            # ТАБЛИЦА АКТИВНЫХ СЕССИЙ (ДЛЯ ПРАВИЛЬНОГО ПОДСЧЕТА ТРАФИКА)
            self.execute('''CREATE TABLE IF NOT EXISTS active_sessions (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          username TEXT NOT NULL,
                          connection_id TEXT NOT NULL,
                          session_hash TEXT NOT NULL,
                          last_bytes_sent BIGINT DEFAULT 0,
                          last_bytes_received BIGINT DEFAULT 0,
                          client_ip TEXT,
                          first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                          last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                          UNIQUE(username, connection_id, session_hash),
                          FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
                       )''')

            # Таблица для резервных копий завершенных сессий
            self.execute('''CREATE TABLE IF NOT EXISTS session_backup (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          username TEXT NOT NULL,
                          connection_id TEXT NOT NULL,
                          session_hash TEXT NOT NULL,
                          total_bytes_sent BIGINT DEFAULT 0,
                          total_bytes_received BIGINT DEFAULT 0,
                          start_time TIMESTAMP,
                          end_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                          backup_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                          backup_reason TEXT,
                          FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
                       )''')

            # Индексы для производительности
            self.execute('''CREATE INDEX IF NOT EXISTS idx_active_sessions_username 
                          ON active_sessions(username)''')
            self.execute('''CREATE INDEX IF NOT EXISTS idx_user_stats_username 
                          ON user_stats(username)''')
            self.execute('''CREATE INDEX IF NOT EXISTS idx_traffic_log_username_date 
                          ON traffic_log(username, log_date)''')
            self.execute('''CREATE INDEX IF NOT EXISTS idx_user_stats_active 
                          ON user_stats(username, status) WHERE status = 'active' ''')

            # Добавляем супер-админа
            self.execute("INSERT OR IGNORE INTO admins (user_id, username, added_by) VALUES (?, ?, ?)",
                         (Config.SUPER_ADMIN_ID, "Супер-админ", Config.SUPER_ADMIN_ID))

            self.commit()
            logger.info("Все таблицы созданы/проверены")

        except Exception as e:
            logger.error(f"Ошибка при создании таблиц: {str(e)}")
            raise

    # === МЕТОДЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ===

    def user_exists(self, username):
        cursor = self.execute("SELECT id FROM users WHERE username = ?", (username,))
        return cursor.fetchone() is not None

    def add_user(self, username, created_by, created_by_username):
        try:
            self.execute("INSERT INTO users (username, created_by, created_by_username) VALUES (?, ?, ?)",
                         (username, created_by, created_by_username))
            self.commit()
            logger.info(f"Пользователь {username} добавлен")
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Пользователь {username} уже существует")
            return False

    def get_all_users(self):
        cursor = self.execute("SELECT * FROM users ORDER BY created_at DESC")
        return cursor.fetchall()

    def get_user(self, username):
        cursor = self.execute("SELECT * FROM users WHERE username = ?", (username,))
        return cursor.fetchone()

    def delete_user(self, username):
        try:
            # Сначала создаем резервную копию данных пользователя
            self.backup_user_data(username, "user_deletion")

            # Затем удаляем пользователя (каскадно удалит все связанные записи)
            cursor = self.execute("DELETE FROM users WHERE username = ?", (username,))
            self.commit()
            deleted = cursor.rowcount > 0

            if deleted:
                logger.info(f"Пользователь {username} удален из БД")
            return deleted

        except Exception as e:
            logger.error(f"Ошибка удаления пользователя {username}: {str(e)}")
            return False

    def clear_all_users(self):
        try:
            # Создаем полную резервную копию перед очисткой
            backup_file = self.create_full_backup("before_clear_all")
            logger.info(f"Создана резервная копия перед очисткой: {backup_file}")

            # Очищаем таблицы в правильном порядке (с учетом foreign keys)
            self.execute("DELETE FROM session_backup")
            self.execute("DELETE FROM active_sessions")
            self.execute("DELETE FROM user_stats")
            self.execute("DELETE FROM traffic_log")
            self.execute("DELETE FROM users")
            # Не удаляем администраторов!

            self.commit()
            logger.info("Все пользователи удалены из БД")
            return True

        except Exception as e:
            logger.error(f"Ошибка очистки БД: {str(e)}")
            return False

    # === МЕТОДЫ ДЛЯ АДМИНИСТРАТОРОВ ===

    def is_admin(self, user_id):
        cursor = self.execute("SELECT id FROM admins WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

    def is_super_admin(self, user_id):
        return user_id == Config.SUPER_ADMIN_ID

    def get_all_admins(self):
        cursor = self.execute('''SELECT a.user_id, a.username, a.added_at, 
                               COALESCE(s.username, 'Супер-админ') as added_by_name
                               FROM admins a 
                               LEFT JOIN admins s ON a.added_by = s.user_id
                               ORDER BY a.added_at''')
        return cursor.fetchall()

    def add_admin(self, user_id, username, added_by):
        try:
            self.execute("INSERT INTO admins (user_id, username, added_by) VALUES (?, ?, ?)",
                         (user_id, username, added_by))
            self.commit()
            logger.info(f"Администратор {username} добавлен")
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Администратор {user_id} уже существует")
            return False

    def delete_admin(self, user_id):
        if user_id == Config.SUPER_ADMIN_ID:
            return False
        cursor = self.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        self.commit()
        return cursor.rowcount > 0

    # === МЕТОДЫ ДЛЯ СТАТИСТИКИ И ТРАФИКА ===

    def update_traffic(self, username, bytes_sent_diff, bytes_received_diff, connection_id=None):
        """Обновляет общий трафик пользователя"""
        try:
            # Обновляем общий трафик пользователя
            self.execute('''UPDATE users 
                         SET total_bytes_sent = total_bytes_sent + ?,
                             total_bytes_received = total_bytes_received + ?,
                             last_updated = CURRENT_TIMESTAMP
                         WHERE username = ?''',
                         (bytes_sent_diff, bytes_received_diff, username))

            # Обновляем ежедневную статистику
            today = datetime.now().date()
            self.execute('''INSERT OR REPLACE INTO traffic_log 
                         (username, log_date, bytes_sent, bytes_received, connections_count)
                         VALUES (?, ?, 
                                 COALESCE((SELECT bytes_sent FROM traffic_log WHERE username = ? AND log_date = ?), 0) + ?,
                                 COALESCE((SELECT bytes_received FROM traffic_log WHERE username = ? AND log_date = ?), 0) + ?,
                                 COALESCE((SELECT connections_count FROM traffic_log WHERE username = ? AND log_date = ?), 0)
                         )''',
                         (username, today, username, today, bytes_sent_diff,
                          username, today, bytes_received_diff, username, today))

            self.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления трафика для {username}: {str(e)}")
            return False

    def create_session_hash(self, username, connection_id, client_ip):
        """Создает уникальный хэш для сессии"""
        import hashlib
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        data = f"{username}_{connection_id}_{client_ip}_{timestamp}"
        return hashlib.md5(data.encode()).hexdigest()[:16]

    def update_active_session(self, username, connection_id, client_ip, current_sent, current_received):
        """Обновляет или создает активную сессию, возвращает разницу трафика"""
        try:
            # Создаем/получаем хэш сессии
            session_hash = self.create_session_hash(username, connection_id, client_ip)

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

                # Если хэш сессии изменился, значит это новая сессия
                if prev_hash != session_hash:
                    # Завершаем старую сессию
                    self.finalize_session(username, connection_id, prev_hash, "session_reconnected")
                    bytes_sent_diff = current_sent
                    bytes_received_diff = current_received
                else:
                    # Та же сессия - вычисляем разницу
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
                self.execute('''INSERT INTO user_stats 
                             (username, connection_start, client_ip, status, session_id)
                             VALUES (?, CURRENT_TIMESTAMP, ?, 'active', ?)''',
                             (username, client_ip, session_hash))

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

    def finalize_session(self, username, connection_id, session_hash, reason="normal_disconnect"):
        """Завершает сессию и создает резервную копию"""
        try:
            # Получаем данные сессии
            cursor = self.execute(
                "SELECT last_bytes_sent, last_bytes_received, client_ip, first_seen FROM active_sessions WHERE username = ? AND connection_id = ? AND session_hash = ?",
                (username, connection_id, session_hash)
            )
            session_data = cursor.fetchone()

            if not session_data:
                logger.warning(f"Сессия не найдена для завершения: {username}_{connection_id}_{session_hash}")
                return False

            sent, received, client_ip, first_seen = session_data

            # Создаем резервную копию сессии
            self.execute('''INSERT INTO session_backup 
                         (username, connection_id, session_hash, total_bytes_sent, total_bytes_received, start_time, backup_reason)
                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                         (username, connection_id, session_hash, sent, received, first_seen, reason))

            # Завершаем сессию в статистике
            cursor = self.execute('''SELECT id FROM user_stats 
                                  WHERE username = ? AND session_id = ? AND status = 'active' 
                                  ORDER BY connection_start DESC LIMIT 1''',
                                  (username, session_hash))
            stats_session = cursor.fetchone()

            if stats_session:
                session_id = stats_session[0]
                self.execute('''UPDATE user_stats 
                             SET connection_end = CURRENT_TIMESTAMP,
                                 duration_seconds = strftime('%s', 'now') - strftime('%s', connection_start),
                                 bytes_sent = ?,
                                 bytes_received = ?,
                                 status = 'completed'
                             WHERE id = ?''',
                             (sent, received, session_id))

            # Удаляем из активных сессий
            self.execute("DELETE FROM active_sessions WHERE username = ? AND connection_id = ? AND session_hash = ?",
                         (username, connection_id, session_hash))

            # Обновляем общий трафик
            self.update_traffic(username, sent, received, connection_id)

            # Помечаем как неактивного если больше нет активных сессий
            cursor = self.execute("SELECT COUNT(*) FROM active_sessions WHERE username = ?", (username,))
            active_count = cursor.fetchone()[0]
            if active_count == 0:
                self.execute("UPDATE users SET is_active = 0 WHERE username = ?", (username,))

            self.commit()
            logger.info(f"Сессия завершена {username}: sent={sent}, received={received}, reason={reason}")
            return True

        except Exception as e:
            logger.error(f"Ошибка завершения сессии {username}: {str(e)}")
            return False

    def cleanup_old_sessions(self, active_usernames):
        """Очищает старые сессии с резервным копированием"""
        try:
            if not active_usernames:
                # Если нет активных пользователей, завершаем все сессии
                cursor = self.execute("SELECT username, connection_id, session_hash FROM active_sessions")
                all_sessions = cursor.fetchall()

                for username, connection_id, session_hash in all_sessions:
                    self.finalize_session(username, connection_id, session_hash, "cleanup_no_active")

                self.execute("UPDATE users SET is_active = 0")
                logger.info("Завершены все сессии (нет активных пользователей)")
                return True

            # Получаем сессии неактивных пользователей
            placeholders = ','.join(['?'] * len(active_usernames))

            cursor = self.execute(f'''SELECT username, connection_id, session_hash 
                                   FROM active_sessions 
                                   WHERE username NOT IN ({placeholders})''',
                                  active_usernames)
            old_sessions = cursor.fetchall()

            # Завершаем каждую сессию с резервированием
            for username, connection_id, session_hash in old_sessions:
                self.finalize_session(username, connection_id, session_hash, "cleanup_inactive")

            # Помечаем неактивных пользователей
            self.execute(f'''UPDATE users 
                         SET is_active = 0 
                         WHERE username NOT IN ({placeholders})''',
                         active_usernames)

            self.commit()

            if old_sessions:
                logger.info(f"Очищено {len(old_sessions)} старых сессий с резервным копированием")

            return True

        except Exception as e:
            logger.error(f"Ошибка очистки сессий: {str(e)}")
            return False

    def backup_user_data(self, username, reason):
        """Создает резервную копию данных пользователя перед удалением"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = self.backup_dir / f"user_{username}_{timestamp}.json"

            # Собираем все данные пользователя
            user_data = {
                "username": username,
                "backup_time": datetime.now().isoformat(),
                "backup_reason": reason,
                "user_info": None,
                "stats": [],
                "traffic_logs": [],
                "session_backups": []
            }

            # Данные пользователя
            cursor = self.execute("SELECT * FROM users WHERE username = ?", (username,))
            user_row = cursor.fetchone()
            if user_row:
                columns = [description[0] for description in cursor.description]
                user_data["user_info"] = dict(zip(columns, user_row))

            # Статистика подключений
            cursor = self.execute("SELECT * FROM user_stats WHERE username = ? ORDER BY connection_start", (username,))
            for row in cursor.fetchall():
                columns = [description[0] for description in cursor.description]
                user_data["stats"].append(dict(zip(columns, row)))

            # Ежедневный трафик
            cursor = self.execute("SELECT * FROM traffic_log WHERE username = ? ORDER BY log_date", (username,))
            for row in cursor.fetchall():
                columns = [description[0] for description in cursor.description]
                user_data["traffic_logs"].append(dict(zip(columns, row)))

            # Резервные копии сессий
            cursor = self.execute("SELECT * FROM session_backup WHERE username = ? ORDER BY backup_time", (username,))
            for row in cursor.fetchall():
                columns = [description[0] for description in cursor.description]
                user_data["session_backups"].append(dict(zip(columns, row)))

            # Сохраняем в файл
            with open(backup_file, 'w', encoding='utf-8') as f:
                json.dump(user_data, f, ensure_ascii=False, indent=2, default=str)

            logger.info(f"Создана резервная копия данных пользователя {username}: {backup_file}")
            return str(backup_file)

        except Exception as e:
            logger.error(f"Ошибка создания резервной копии для {username}: {str(e)}")
            return None

    def create_full_backup(self, reason="manual"):
        """Создает полную резервную копию базы данных"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = self.backup_dir / f"full_backup_{timestamp}.db"

            # Копируем файл базы данных
            shutil.copy2(self.db_path, backup_file)

            # Также создаем JSON backup для читаемости
            json_file = self.backup_dir / f"full_backup_{timestamp}.json"

            backup_data = {
                "backup_time": datetime.now().isoformat(),
                "backup_reason": reason,
                "database_file": str(backup_file),
                "summary": {
                    "total_users": 0,
                    "total_admins": 0,
                    "total_sessions": 0,
                    "total_traffic_logs": 0
                }
            }

            # Получаем статистику
            cursor = self.execute("SELECT COUNT(*) FROM users")
            backup_data["summary"]["total_users"] = cursor.fetchone()[0]

            cursor = self.execute("SELECT COUNT(*) FROM admins")
            backup_data["summary"]["total_admins"] = cursor.fetchone()[0]

            cursor = self.execute("SELECT COUNT(*) FROM user_stats")
            backup_data["summary"]["total_sessions"] = cursor.fetchone()[0]

            cursor = self.execute("SELECT COUNT(*) FROM traffic_log")
            backup_data["summary"]["total_traffic_logs"] = cursor.fetchone()[0]

            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(backup_data, f, ensure_ascii=False, indent=2, default=str)

            # Очищаем старые бэкапы
            self.cleanup_old_backups()

            logger.info(f"Создана полная резервная копия: {backup_file}")
            return str(backup_file)

        except Exception as e:
            logger.error(f"Ошибка создания полной резервной копии: {str(e)}")
            return None

    def cleanup_old_backups(self):
        """Удаляет старые резервные копии"""
        try:
            cutoff_date = datetime.now() - timedelta(days=Config.BACKUP_RETENTION_DAYS)

            for backup_file in self.backup_dir.glob("*"):
                if backup_file.is_file():
                    file_time = datetime.fromtimestamp(backup_file.stat().st_mtime)
                    if file_time < cutoff_date:
                        backup_file.unlink()
                        logger.info(f"Удален старый бэкап: {backup_file.name}")

            return True
        except Exception as e:
            logger.error(f"Ошибка очистки старых бэкапов: {str(e)}")
            return False

    def get_user_statistics(self, username):
        """Получает статистику пользователя"""
        try:
            # Основная статистика
            cursor = self.execute(
                "SELECT total_connections, last_connected, total_bytes_sent, total_bytes_received, is_active FROM users WHERE username = ?",
                (username,)
            )
            user_stats = cursor.fetchone()

            if not user_stats:
                return None

            # Статистика за 30 дней
            cursor = self.execute(
                "SELECT SUM(bytes_sent), SUM(bytes_received), SUM(connections_count) FROM traffic_log WHERE username = ? AND log_date >= date('now', '-30 days')",
                (username,)
            )
            monthly_stats = cursor.fetchone()

            # Активные сессии
            cursor = self.execute(
                "SELECT COUNT(*) FROM active_sessions WHERE username = ?",
                (username,)
            )
            active_sessions = cursor.fetchone()[0] or 0

            return {
                'total_connections': user_stats[0] or 0,
                'last_connected': user_stats[1],
                'total_bytes_sent': user_stats[2] or 0,
                'total_bytes_received': user_stats[3] or 0,
                'is_active': bool(user_stats[4]),
                'active_sessions': active_sessions,
                'monthly_sent': monthly_stats[0] or 0 if monthly_stats else 0,
                'monthly_received': monthly_stats[1] or 0 if monthly_stats else 0,
                'monthly_connections': monthly_stats[2] or 0 if monthly_stats else 0
            }
        except Exception as e:
            logger.error(f"Ошибка получения статистики пользователя {username}: {str(e)}")
            return None

    def get_active_users_count(self):
        cursor = self.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
        return cursor.fetchone()[0] or 0

    def get_user_count(self):
        cursor = self.execute("SELECT COUNT(*) FROM users")
        return cursor.fetchone()[0] or 0

    def get_database_size(self):
        """Возвращает размер базы данных в байтах"""
        try:
            return self.db_path.stat().st_size
        except Exception as e:
            logger.error(f"Ошибка получения размера БД: {str(e)}")
            return 0

    def get_backup_info(self):
        """Возвращает информацию о резервных копиях"""
        try:
            backups = []
            total_size = 0

            for backup_file in sorted(self.backup_dir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
                if backup_file.is_file():
                    file_info = {
                        "name": backup_file.name,
                        "size": backup_file.stat().st_size,
                        "modified": datetime.fromtimestamp(backup_file.stat().st_mtime).isoformat(),
                        "path": str(backup_file)
                    }
                    backups.append(file_info)
                    total_size += file_info["size"]

            return {
                "total_backups": len(backups),
                "total_size": total_size,
                "backups": backups[:10]  # Последние 10 бэкапов
            }
        except Exception as e:
            logger.error(f"Ошибка получения информации о бэкапах: {str(e)}")
            return {"total_backups": 0, "total_size": 0, "backups": []}


# Глобальный экземпляр БД
db = Database()