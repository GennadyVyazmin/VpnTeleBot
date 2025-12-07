import subprocess
import re
import logging
import time
import threading
import signal
import sys
from datetime import datetime
from database import db
from config import Config

logger = logging.getLogger(__name__)


class TrafficMonitor:
    def __init__(self):
        self.update_interval = Config.STATS_UPDATE_INTERVAL
        self.cleanup_interval = Config.SESSION_CLEANUP_INTERVAL
        self.last_cleanup = time.time()
        self.last_update = time.time()
        self.running = True

        # Кэш базовых значений трафика (чтобы не запрашивать БД каждый раз)
        self.base_traffic_cache = {}  # {username: {'sent': X, 'received': Y}}

        signal.signal(signal.SIGINT, self.graceful_shutdown)
        signal.signal(signal.SIGTERM, self.graceful_shutdown)

    def graceful_shutdown(self, signum=None, frame=None):
        logger.info("Получен сигнал завершения, фиксируем трафик...")
        self.running = False
        self.finalize_all_sessions()
        sys.exit(0)

    def parse_ipsec_status(self):
        """Парсит вывод ipsec trafficstatus - ТОЛЬКО ЧТЕНИЕ"""
        try:
            result = subprocess.run(['ipsec', 'trafficstatus'],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                logger.error(f"Ошибка ipsec trafficstatus: {result.stderr}")
                return {}

            traffic_data = {}
            lines = result.stdout.split('\n')

            for line in lines:
                line = line.strip()
                if not line or 'CN=' not in line:
                    continue

                # Извлекаем username
                cn_match = re.search(r"CN=([^,]+)", line)
                if not cn_match:
                    continue
                username = cn_match.group(1).strip()

                # Извлекаем connection_id
                connection_id = "unknown"
                id_match = re.search(r'#(\d+):', line)
                if id_match:
                    connection_id = id_match.group(1)

                # Извлекаем АБСОЛЮТНЫЕ значения трафика (не разницу!)
                current_sent = 0
                current_received = 0

                in_match = re.search(r'inBytes=(\d+)', line)
                out_match = re.search(r'outBytes=(\d+)', line)

                if in_match:
                    current_received = int(in_match.group(1))  # Получено клиентом
                if out_match:
                    current_sent = int(out_match.group(1))  # Отправлено клиентом

                # Извлекаем IP
                ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                client_ip = ip_match.group(1) if ip_match else "unknown"

                traffic_data[username] = {
                    'connection_id': connection_id,
                    'absolute_sent': current_sent,  # АБСОЛЮТНОЕ значение
                    'absolute_received': current_received,  # АБСОЛЮТНОЕ значение
                    'client_ip': client_ip,
                    'raw_line': line
                }

            logger.debug(f"Спарсено {len(traffic_data)} записей из trafficstatus")
            return traffic_data

        except subprocess.TimeoutExpired:
            logger.error("Таймаут выполнения ipsec trafficstatus")
            return {}
        except Exception as e:
            logger.error(f"Ошибка парсинга trafficstatus: {str(e)}")
            return {}

    def get_base_traffic(self, username):
        """Получает базовые значения трафика для пользователя"""
        try:
            # Проверяем кэш
            if username in self.base_traffic_cache:
                return self.base_traffic_cache[username]

            # Получаем из active_sessions (последняя запись)
            cursor = db.execute(
                "SELECT last_bytes_sent, last_bytes_received FROM active_sessions WHERE username = ? ORDER BY last_updated DESC LIMIT 1",
                (username,)
            )
            result = cursor.fetchone()

            if result:
                base_sent, base_received = result
                self.base_traffic_cache[username] = {
                    'sent': base_sent,
                    'received': base_received
                }
                return self.base_traffic_cache[username]

            # Если нет в active_sessions, берем из последней завершенной сессии
            cursor = db.execute(
                "SELECT total_bytes_sent, total_bytes_received FROM users WHERE username = ?",
                (username,)
            )
            result = cursor.fetchone()

            if result:
                total_sent, total_received = result
                self.base_traffic_cache[username] = {
                    'sent': total_sent or 0,
                    'received': total_received or 0
                }
            else:
                self.base_traffic_cache[username] = {
                    'sent': 0,
                    'received': 0
                }

            return self.base_traffic_cache[username]

        except Exception as e:
            logger.error(f"Ошибка получения базового трафика для {username}: {str(e)}")
            return {'sent': 0, 'received': 0}

    def update_base_traffic(self, username, absolute_sent, absolute_received):
        """Обновляет базовые значения трафика"""
        try:
            self.base_traffic_cache[username] = {
                'sent': absolute_sent,
                'received': absolute_received
            }
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления кэша для {username}: {str(e)}")
            return False

    def detect_disconnections(self, current_traffic_data):
        """Обнаруживает отключения"""
        try:
            current_usernames = set(current_traffic_data.keys())

            # Получаем активные сессии из БД
            cursor = db.execute("SELECT username, connection_id, session_hash FROM active_sessions")
            db_sessions = cursor.fetchall()

            disconnected = []

            for db_username, connection_id, session_hash in db_sessions:
                if db_username not in current_usernames:
                    # Пользователь отключился
                    disconnected.append((db_username, connection_id, session_hash))

            # Завершаем обнаруженные отключения
            for username, connection_id, session_hash in disconnected:
                db.finalize_session(username, connection_id, session_hash, "detected_disconnect")
                # Удаляем из кэша
                if username in self.base_traffic_cache:
                    del self.base_traffic_cache[username]

            return len(disconnected)

        except Exception as e:
            logger.error(f"Ошибка обнаружения отключений: {str(e)}")
            return 0

    def update_traffic_stats(self):
        """Основная функция обновления статистики с ПРАВИЛЬНЫМ подсчетом"""
        try:
            start_time = time.time()

            # 1. Получаем текущие АБСОЛЮТНЫЕ значения из ipsec
            traffic_data = self.parse_ipsec_status()

            # 2. Фиксируем отключения
            disconnected_count = self.detect_disconnections(traffic_data)

            active_usernames = list(traffic_data.keys())
            total_traffic_updated = 0
            total_sent_diff = 0
            total_received_diff = 0

            # 3. Для каждого активного пользователя
            for username, data in traffic_data.items():
                connection_id = data['connection_id']
                client_ip = data['client_ip']
                absolute_sent = data['absolute_sent']
                absolute_received = data['absolute_received']

                # 4. Получаем БАЗОВЫЕ значения
                base_traffic = self.get_base_traffic(username)
                base_sent = base_traffic['sent']
                base_received = base_traffic['received']

                # 5. Вычисляем РАЗНИЦУ (но защищаемся от сбросов/переполнений)
                sent_diff = 0
                received_diff = 0

                # Вариант 1: Абсолютные значения больше базовых (нормальный случай)
                if absolute_sent >= base_sent and absolute_received >= base_received:
                    sent_diff = absolute_sent - base_sent
                    received_diff = absolute_received - base_received

                # Вариант 2: Счетчик обнулился (переподключение или переполнение)
                elif absolute_sent < base_sent or absolute_received < base_received:
                    # Считаем что это новая сессия, весь трафик - новый
                    sent_diff = absolute_sent
                    received_diff = absolute_received

                    logger.info(f"Обнуление счетчика для {username}: "
                                f"было sent={base_sent}, стало {absolute_sent}, "
                                f"было received={base_received}, стало {absolute_received}")

                # 6. Если есть трафик - обновляем
                if sent_diff > 0 or received_diff > 0:
                    # Обновляем в БД
                    if db.update_traffic(username, sent_diff, received_diff, connection_id):
                        total_traffic_updated += 1
                        total_sent_diff += sent_diff
                        total_received_diff += received_diff

                        # Обновляем активную сессию
                        session_hash = self.update_active_session_in_db(
                            username, connection_id, client_ip, absolute_sent, absolute_received
                        )

                        # Обновляем базовые значения в кэше
                        self.update_base_traffic(username, absolute_sent, absolute_received)

                        logger.debug(f"Обновлен трафик {username}: "
                                     f"+{sent_diff / 1024 / 1024:.1f}MB sent, "
                                     f"+{received_diff / 1024 / 1024:.1f}MB received")

            # 7. Периодическая очистка
            current_time = time.time()
            if current_time - self.last_cleanup > self.cleanup_interval:
                cleanup_start = time.time()
                db.cleanup_old_sessions(active_usernames)
                cleanup_time = time.time() - cleanup_start
                self.last_cleanup = current_time
                logger.info(f"Очистка сессий за {cleanup_time:.2f} сек")

            # 8. Обновляем время
            self.last_update = current_time
            update_duration = time.time() - start_time

            # 9. Логируем результаты
            if active_usernames or disconnected_count > 0 or total_traffic_updated > 0:
                log_msg = (f"Обновление: {len(active_usernames)} активных, "
                           f"{disconnected_count} отключений, "
                           f"{total_traffic_updated} обновлений трафика")

                if total_sent_diff > 0 or total_received_diff > 0:
                    log_msg += (f", трафик: "
                                f"+{total_sent_diff / 1024 / 1024:.1f}MB/+{total_received_diff / 1024 / 1024:.1f}MB")

                log_msg += f", время: {update_duration:.2f} сек"
                logger.info(log_msg)

            return len(active_usernames), total_traffic_updated, disconnected_count

        except Exception as e:
            logger.error(f"Ошибка обновления статистики: {str(e)}")
            return 0, 0, 0

    def update_active_session_in_db(self, username, connection_id, client_ip, absolute_sent, absolute_received):
        """Обновляет активную сессию в БД"""
        try:
            # Создаем хэш сессии
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            import hashlib
            data = f"{username}_{connection_id}_{client_ip}_{timestamp}"
            session_hash = hashlib.md5(data.encode()).hexdigest()[:16]

            # Проверяем существующую сессию
            cursor = db.execute(
                "SELECT session_hash FROM active_sessions WHERE username = ? AND connection_id = ?",
                (username, connection_id)
            )
            existing = cursor.fetchone()

            if existing:
                # Обновляем существующую
                existing_hash = existing[0]
                db.execute('''UPDATE active_sessions 
                           SET last_bytes_sent = ?,
                               last_bytes_received = ?,
                               client_ip = ?,
                               last_updated = CURRENT_TIMESTAMP
                           WHERE username = ? AND connection_id = ? AND session_hash = ?''',
                           (absolute_sent, absolute_received, client_ip, username, connection_id, existing_hash))
                session_hash = existing_hash
            else:
                # Создаем новую сессию
                db.execute('''INSERT INTO active_sessions 
                           (username, connection_id, session_hash, last_bytes_sent, last_bytes_received, client_ip)
                           VALUES (?, ?, ?, ?, ?, ?)''',
                           (username, connection_id, session_hash, absolute_sent, absolute_received, client_ip))

                # Регистрируем начало подключения
                try:
                    db.execute('''INSERT INTO user_stats 
                               (username, connection_start, client_ip, status, session_id)
                               VALUES (?, CURRENT_TIMESTAMP, ?, 'active', ?)''',
                               (username, client_ip, session_hash))
                except Exception as e:
                    # Если нет колонки session_id
                    db.execute('''INSERT INTO user_stats 
                               (username, connection_start, client_ip, status)
                               VALUES (?, CURRENT_TIMESTAMP, ?, 'active')''',
                               (username, client_ip))

                # Увеличиваем счетчик подключений
                db.execute('''UPDATE users 
                           SET total_connections = total_connections + 1,
                               last_connected = CURRENT_TIMESTAMP,
                               is_active = 1
                           WHERE username = ?''',
                           (username,))

            db.commit()
            return session_hash

        except Exception as e:
            logger.error(f"Ошибка обновления сессии в БД {username}: {str(e)}")
            return None

    def finalize_all_sessions(self):
        """Завершает все активные сессии"""
        try:
            logger.info("Завершение всех активных сессий...")

            # Создаем резервную копию
            backup_file = db.create_full_backup("shutdown_backup")
            if backup_file:
                logger.info(f"Создана резервная копия: {backup_file}")

            # Получаем все активные сессии
            cursor = db.execute("SELECT username, connection_id, session_hash FROM active_sessions")
            active_sessions = cursor.fetchall()

            completed = 0
            for username, connection_id, session_hash in active_sessions:
                if db.finalize_session(username, connection_id, session_hash, "shutdown"):
                    completed += 1

            # Очищаем кэш
            self.base_traffic_cache.clear()

            logger.info(f"Завершено {completed} сессий")
            return completed

        except Exception as e:
            logger.error(f"Ошибка при завершении сессий: {str(e)}")
            return 0

    def get_monitor_status(self):
        """Возвращает статус монитора"""
        return {
            "running": self.running,
            "last_update": datetime.fromtimestamp(self.last_update).isoformat(),
            "update_interval": self.update_interval,
            "next_update_in": max(0, self.update_interval - (time.time() - self.last_update)),
            "cache_size": len(self.base_traffic_cache)
        }

    def reset_traffic_counter(self, username=None):
        """Сбрасывает счетчики трафика (для тестирования)"""
        try:
            if username:
                # Сбрасываем для конкретного пользователя
                if username in self.base_traffic_cache:
                    del self.base_traffic_cache[username]
                logger.info(f"Сброшен кэш трафика для {username}")
                return True
            else:
                # Сбрасываем все
                self.base_traffic_cache.clear()
                logger.info("Сброшен весь кэш трафика")
                return True
        except Exception as e:
            logger.error(f"Ошибка сброса счетчиков: {str(e)}")
            return False

    def start_monitoring(self):
        """Запускает мониторинг"""

        def monitor_loop():
            logger.info(f"Запуск мониторинга трафика (интервал: {self.update_interval} сек)")

            while self.running:
                try:
                    self.update_traffic_stats()

                    elapsed = time.time() - self.last_update
                    sleep_time = max(1, self.update_interval - elapsed)

                    if elapsed > self.update_interval * 2:
                        logger.warning(f"Обновление заняло {elapsed:.1f} сек")
                        sleep_time = 1

                    time.sleep(sleep_time)

                except Exception as e:
                    logger.error(f"Критическая ошибка в мониторе: {str(e)}")
                    time.sleep(30)

        monitor_thread = threading.Thread(target=monitor_loop, daemon=True, name="TrafficMonitor")
        monitor_thread.start()
        logger.info("Мониторинг трафика запущен")


# Глобальный экземпляр монитора
traffic_monitor = TrafficMonitor()