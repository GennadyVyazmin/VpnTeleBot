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
        self.update_interval = Config.STATS_UPDATE_INTERVAL  # 30 секунд
        self.cleanup_interval = Config.SESSION_CLEANUP_INTERVAL
        self.last_cleanup = time.time()
        self.last_update = time.time()
        self.running = True

        # Регистрируем обработчик сигналов
        signal.signal(signal.SIGINT, self.graceful_shutdown)
        signal.signal(signal.SIGTERM, self.graceful_shutdown)

    def graceful_shutdown(self, signum=None, frame=None):
        """Graceful shutdown с фиксацией всего трафика"""
        logger.info("Получен сигнал завершения, фиксируем трафик...")
        self.running = False
        self.finalize_all_sessions()
        sys.exit(0)

    def parse_ipsec_status(self):
        """Парсит вывод ipsec trafficstatus"""
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

                # Извлекаем текущие значения трафика
                current_sent = 0
                current_received = 0

                in_match = re.search(r'inBytes=(\d+)', line)
                out_match = re.search(r'outBytes=(\d+)', line)

                if in_match:
                    current_received = int(in_match.group(1))
                if out_match:
                    current_sent = int(out_match.group(1))

                # Извлекаем IP
                ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                client_ip = ip_match.group(1) if ip_match else "unknown"

                traffic_data[username] = {
                    'connection_id': connection_id,
                    'current_sent': current_sent,
                    'current_received': current_received,
                    'client_ip': client_ip,
                    'raw_line': line  # Сохраняем для отладки
                }

            return traffic_data

        except subprocess.TimeoutExpired:
            logger.error("Таймаут выполнения ipsec trafficstatus")
            return {}
        except Exception as e:
            logger.error(f"Ошибка парсинга trafficstatus: {str(e)}")
            return {}

    def detect_disconnections(self, current_traffic_data):
        """Обнаруживает отключения и немедленно фиксирует трафик"""
        try:
            current_usernames = set(current_traffic_data.keys())

            # Получаем все активные сессии из БД
            cursor = db.execute("SELECT username, connection_id, session_hash FROM active_sessions")
            db_sessions = cursor.fetchall()

            disconnected = []

            for db_username, connection_id, session_hash in db_sessions:
                if db_username not in current_usernames:
                    # Пользователь отключился - завершаем все его сессии
                    disconnected.append((db_username, connection_id, session_hash))

            # Завершаем обнаруженные отключения
            for username, connection_id, session_hash in disconnected:
                db.finalize_session(username, connection_id, session_hash, "detected_disconnect")

            return len(disconnected)

        except Exception as e:
            logger.error(f"Ошибка обнаружения отключений: {str(e)}")
            return 0

    def update_traffic_stats(self):
        """Основная функция обновления статистики"""
        try:
            start_time = time.time()

            # Получаем текущие данные
            traffic_data = self.parse_ipsec_status()

            # Немедленно фиксируем отключения
            disconnected_count = self.detect_disconnections(traffic_data)
            if disconnected_count > 0:
                logger.info(f"Обнаружено отключений: {disconnected_count}")

            active_usernames = list(traffic_data.keys())
            total_traffic_updated = 0
            total_traffic_sent = 0
            total_traffic_received = 0

            # Обновляем трафик для каждого активного пользователя
            for username, data in traffic_data.items():
                connection_id = data['connection_id']
                client_ip = data['client_ip']
                current_sent = data['current_sent']
                current_received = data['current_received']

                # Обновляем сессию и получаем разницу трафика
                sent_diff, received_diff, session_hash = db.update_active_session(
                    username, connection_id, client_ip, current_sent, current_received
                )

                # Обновляем общий трафик если есть разница
                if sent_diff > 0 or received_diff > 0:
                    db.update_traffic(username, sent_diff, received_diff, connection_id)
                    total_traffic_updated += 1
                    total_traffic_sent += sent_diff
                    total_traffic_received += received_diff

            # Периодическая очистка старых сессий
            current_time = time.time()
            if current_time - self.last_cleanup > self.cleanup_interval:
                cleanup_start = time.time()
                db.cleanup_old_sessions(active_usernames)
                cleanup_time = time.time() - cleanup_start
                self.last_cleanup = current_time
                logger.info(f"Периодическая очистка выполнена за {cleanup_time:.2f} сек")

            # Обновляем время последнего обновления
            self.last_update = current_time
            update_duration = time.time() - start_time

            if active_usernames or disconnected_count > 0 or total_traffic_updated > 0:
                logger.info(f"Обновление статистики: {len(active_usernames)} активных, "
                            f"{disconnected_count} отключений, "
                            f"{total_traffic_updated} обновлений трафика, "
                            f"трафик +{total_traffic_sent / 1024 / 1024:.1f}MB/+{total_traffic_received / 1024 / 1024:.1f}MB, "
                            f"время: {update_duration:.2f} сек")

            return len(active_usernames), total_traffic_updated, disconnected_count

        except Exception as e:
            logger.error(f"Ошибка обновления статистики: {str(e)}")
            return 0, 0, 0

    def finalize_all_sessions(self):
        """Завершает все активные сессии при завершении работы"""
        try:
            logger.info("Завершение всех активных сессий...")

            # Создаем резервную копию перед завершением
            backup_file = db.create_full_backup("shutdown_backup")
            if backup_file:
                logger.info(f"Создана резервная копия перед завершением: {backup_file}")

            # Получаем все активные сессии
            cursor = db.execute("SELECT username, connection_id, session_hash FROM active_sessions")
            active_sessions = cursor.fetchall()

            completed = 0
            for username, connection_id, session_hash in active_sessions:
                if db.finalize_session(username, connection_id, session_hash, "shutdown"):
                    completed += 1

            logger.info(f"Завершено {completed} сессий при завершении работы")
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
            "next_update_in": max(0, self.update_interval - (time.time() - self.last_update))
        }

    def start_monitoring(self):
        """Запускает мониторинг в отдельном потоке"""

        def monitor_loop():
            logger.info(f"Запуск мониторинга трафика (интервал: {self.update_interval} сек)")

            while self.running:
                try:
                    self.update_traffic_stats()

                    # Рассчитываем время сна с учетом времени выполнения
                    elapsed = time.time() - self.last_update
                    sleep_time = max(1, self.update_interval - elapsed)

                    # Корректируем интервал если обновление заняло слишком много времени
                    if elapsed > self.update_interval * 2:
                        logger.warning(
                            f"Обновление заняло {elapsed:.1f} сек, больше чем интервал {self.update_interval} сек")
                        sleep_time = 1  # Минимальная задержка

                    time.sleep(sleep_time)

                except Exception as e:
                    logger.error(f"Критическая ошибка в мониторе: {str(e)}")
                    time.sleep(30)  # Большая задержка при ошибке

        monitor_thread = threading.Thread(target=monitor_loop, daemon=True, name="TrafficMonitor")
        monitor_thread.start()
        logger.info("Мониторинг трафика запущен")


# Глобальный экземпляр монитора
traffic_monitor = TrafficMonitor()