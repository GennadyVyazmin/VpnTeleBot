#!/usr/bin/env python3
import sqlite3
import os
import sys
from pathlib import Path
from datetime import datetime


def fix_database_structure():
    """Исправляет структуру базы данных"""
    db_path = Path(__file__).parent / 'users.db'

    if not db_path.exists():
        print(f"❌ Файл базы данных не найден: {db_path}")
        return False

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        print("🔧 Проверяем и исправляем структуру БД...")

        # 1. Проверяем таблицу users
        print("\n📊 Таблица users:")
        cursor.execute("PRAGMA table_info(users)")
        users_columns = cursor.fetchall()
        print(f"Колонки: {[col[1] for col in users_columns]}")

        # Добавляем недостающие колонки в users
        users_columns_names = [col[1] for col in users_columns]
        if 'last_updated' not in users_columns_names:
            print("Добавляем last_updated...")
            cursor.execute("ALTER TABLE users ADD COLUMN last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

        # 2. Проверяем таблицу user_stats
        print("\n📈 Таблица user_stats:")
        cursor.execute("PRAGMA table_info(user_stats)")
        stats_columns = cursor.fetchall()
        print(f"Колонки: {[col[1] for col in stats_columns]}")

        # Добавляем session_id если нет
        stats_columns_names = [col[1] for col in stats_columns]
        if 'session_id' not in stats_columns_names:
            print("Добавляем session_id...")
            cursor.execute("ALTER TABLE user_stats ADD COLUMN session_id TEXT")

        # 3. Отключаем foreign keys временно
        cursor.execute("PRAGMA foreign_keys = OFF")

        # 4. Создаем таблицу active_sessions если нет
        print("\n🔗 Таблица active_sessions:")
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='active_sessions'")
        if not cursor.fetchone():
            print("Создаем active_sessions...")
            cursor.execute('''CREATE TABLE active_sessions (
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
                         )''')

        # 5. Создаем таблицу session_backup если нет
        print("\n💾 Таблица session_backup:")
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_backup'")
        if not cursor.fetchone():
            print("Создаем session_backup...")
            cursor.execute('''CREATE TABLE session_backup (
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

        # 6. Включаем foreign keys обратно
        cursor.execute("PRAGMA foreign_keys = ON")

        conn.commit()
        print("\n✅ Структура БД исправлена!")

        return True

    except Exception as e:
        print(f"❌ Ошибка: {str(e)}")
        return False
    finally:
        if conn:
            conn.close()


def get_vpn_users_from_system():
    """Получает список VPN пользователей из системы"""
    import subprocess
    import re

    print("\n🔍 Ищем VPN пользователей в системе...")

    vpn_users = []

    try:
        # 1. Проверяем директорию с конфигами
        config_dir = '/root/'
        if os.path.exists(config_dir):
            for filename in os.listdir(config_dir):
                # Ищем файлы с расширениями конфигов
                if filename.endswith('.mobileconfig') or filename.endswith('.p12') or filename.endswith('.sswan'):
                    username = filename.split('.')[0]
                    if username not in vpn_users:
                        vpn_users.append(username)
                        print(f"Найден в конфигах: {username}")

        # 2. Пробуем получить список из ipsec
        try:
            result = subprocess.run(['ipsec', 'trafficstatus'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                lines = result.stdout.split('\n')
                for line in lines:
                    if 'CN=' in line:
                        match = re.search(r"CN=([^,]+)", line)
                        if match:
                            username = match.group(1).strip()
                            if username not in vpn_users:
                                vpn_users.append(username)
                                print(f"Найден в ipsec: {username}")
        except:
            pass

        # 3. Проверяем через скрипт ikev2.sh (если есть)
        ikev2_script = '/usr/bin/ikev2.sh'
        if os.path.exists(ikev2_script):
            try:
                # Пробуем получить список сертификатов
                result = subprocess.run(['sudo', ikev2_script, '--listclients'],
                                        capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        line = line.strip()
                        if line and not line.startswith('Listing VPN') and 'client' in line.lower():
                            # Пытаемся извлечь имя пользователя
                            parts = line.split()
                            for part in parts:
                                if part and part not in ['VPN', 'client', 'certificate']:
                                    if part not in vpn_users:
                                        vpn_users.append(part)
                                        print(f"Найден в сертификатах: {part}")
            except:
                pass

        print(f"\n📋 Всего найдено VPN пользователей: {len(vpn_users)}")
        return vpn_users

    except Exception as e:
        print(f"Ошибка поиска пользователей: {str(e)}")
        return []


def add_missing_users_to_db(vpn_users):
    """Добавляет отсутствующих пользователей в БД"""
    db_path = Path(__file__).parent / 'users.db'

    if not db_path.exists():
        print(f"❌ Файл базы данных не найден: {db_path}")
        return False

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Получаем существующих пользователей из БД
        cursor.execute("SELECT username FROM users")
        existing_users = [row[0] for row in cursor.fetchall()]
        print(f"\n📊 В БД уже есть: {len(existing_users)} пользователей")

        # Находим отсутствующих
        missing_users = [user for user in vpn_users if user not in existing_users]
        print(f"📝 Отсутствуют в БД: {len(missing_users)} пользователей")

        if not missing_users:
            print("✅ Все пользователи уже в БД!")
            return True

        # Добавляем отсутствующих
        added_count = 0
        for username in missing_users:
            try:
                # Добавляем как созданных системой (супер-админом)
                cursor.execute(
                    "INSERT INTO users (username, created_by, created_by_username) VALUES (?, ?, ?)",
                    (username, 149999149, "Система (автодобавление)")
                )
                added_count += 1
                print(f"✅ Добавлен: {username}")
            except sqlite3.IntegrityError:
                print(f"⚠️ Уже существует: {username}")
            except Exception as e:
                print(f"❌ Ошибка добавления {username}: {str(e)}")

        conn.commit()
        print(f"\n🎯 Добавлено {added_count} пользователей в БД")

        return True

    except Exception as e:
        print(f"❌ Ошибка: {str(e)}")
        return False
    finally:
        if conn:
            conn.close()


def fix_foreign_keys():
    """Исправляет проблемы с foreign keys"""
    db_path = Path(__file__).parent / 'users.db'

    if not db_path.exists():
        print(f"❌ Файл базы данных не найден: {db_path}")
        return False

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        print("\n🔧 Исправляем foreign keys...")

        # 1. Проверяем записи в user_stats без соответствующих пользователей
        cursor.execute('''SELECT DISTINCT us.username 
                        FROM user_stats us 
                        LEFT JOIN users u ON us.username = u.username 
                        WHERE u.username IS NULL''')
        orphaned_stats = cursor.fetchall()

        if orphaned_stats:
            print(f"Найдено {len(orphaned_stats)} записей в user_stats без пользователей:")
            for orphan in orphaned_stats:
                print(f"  - {orphan[0]}")

            # Создаем пользователей для orphaned записей
            for orphan in orphaned_stats:
                username = orphan[0]
                try:
                    cursor.execute(
                        "INSERT OR IGNORE INTO users (username, created_by, created_by_username) VALUES (?, ?, ?)",
                        (username, 149999149, "Система (восстановление)")
                    )
                    print(f"  ✅ Создан пользователь для: {username}")
                except Exception as e:
                    print(f"  ❌ Ошибка создания {username}: {str(e)}")

        # 2. Проверяем записи в traffic_log без пользователей
        cursor.execute('''SELECT DISTINCT tl.username 
                        FROM traffic_log tl 
                        LEFT JOIN users u ON tl.username = u.username 
                        WHERE u.username IS NULL''')
        orphaned_logs = cursor.fetchall()

        if orphaned_logs:
            print(f"Найдено {len(orphaned_logs)} записей в traffic_log без пользователей:")
            for orphan in orphaned_logs:
                print(f"  - {orphan[0]}")

        conn.commit()
        print("✅ Foreign keys исправлены")

        return True

    except Exception as e:
        print(f"❌ Ошибка: {str(e)}")
        return False
    finally:
        if conn:
            conn.close()


def main():
    print("=" * 60)
    print("🛠️  СКРИПТ ВОССТАНОВЛЕНИЯ БАЗЫ ДАННЫХ VPN")
    print("=" * 60)

    # 1. Исправляем структуру
    if not fix_database_structure():
        print("❌ Не удалось исправить структуру БД")
        return

    # 2. Получаем VPN пользователей из системы
    vpn_users = get_vpn_users_from_system()

    if vpn_users:
        print(f"\n📋 Найдено {len(vpn_users)} VPN пользователей в системе")

        # 3. Добавляем отсутствующих в БД
        add_missing_users_to_db(vpn_users)
    else:
        print("\n⚠️ VPN пользователи не найдены в системе")

    # 4. Исправляем foreign keys
    fix_foreign_keys()

    print("\n" + "=" * 60)
    print("✅ ВОССТАНОВЛЕНИЕ ЗАВЕРШЕНО!")
    print("=" * 60)

    # Показываем итоговую статистику
    db_path = Path(__file__).parent / 'users.db'
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM user_stats")
        total_stats = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM traffic_log")
        total_logs = cursor.fetchone()[0]

        print(f"\n📊 ИТОГОВАЯ СТАТИСТИКА:")
        print(f"👥 Пользователей в БД: {total_users}")
        print(f"📈 Записей в статистике: {total_stats}")
        print(f"📅 Записей в логах трафика: {total_logs}")

        conn.close()


if __name__ == "__main__":
    main()