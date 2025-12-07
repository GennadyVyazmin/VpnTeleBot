#!/usr/bin/env python3
import sqlite3
import os
import sys
import re
import subprocess
from pathlib import Path
from datetime import datetime


def get_real_vpn_users():
    """Получает реальных VPN пользователей (исправленная версия)"""
    vpn_users = []
    config_dir = '/root/'

    print("\n🔍 Ищем VPN пользователей по конфигурационным файлам...")

    if os.path.exists(config_dir):
        for filename in os.listdir(config_dir):
            # Ищем только файлы конфигурации VPN
            if filename.endswith('.mobileconfig') or filename.endswith('.p12') or filename.endswith('.sswan'):
                # Извлекаем имя пользователя (убираем расширение)
                username = filename.rsplit('.', 1)[0]

                # Проверяем что это действительно имя пользователя (не системный файл)
                if (len(username) >= 3 and
                        re.match(r'^[a-zA-Z0-9_-]+$', username) and
                        username.lower() not in ['readme', 'license', 'config'] and
                        username not in vpn_users):
                    vpn_users.append(username)
                    print(f"✅ Найден в конфигах: {username}")

    # Также проверяем через ipsec trafficstatus
    print("\n🔍 Проверяем активные подключения...")
    try:
        result = subprocess.run(['ipsec', 'trafficstatus'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'CN=' in line:
                    match = re.search(r"CN=([^,]+)", line)
                    if match:
                        username = match.group(1).strip()
                        if username not in vpn_users:
                            vpn_users.append(username)
                            print(f"✅ Активен в ipsec: {username}")
    except Exception as e:
        print(f"⚠️ Не удалось проверить ipsec: {e}")

    print(f"\n📋 Найдено реальных VPN пользователей: {len(vpn_users)}")
    return sorted(vpn_users)


def clean_up_bad_users():
    """Удаляет неправильно добавленных пользователей"""
    db_path = Path(__file__).parent / 'users.db'

    if not db_path.exists():
        return

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Получаем всех пользователей из БД
        cursor.execute("SELECT username FROM users")
        db_users = [row[0] for row in cursor.fetchall()]

        # Список "плохих" пользователей для удаления
        bad_users = []

        for username in db_users:
            # Проверяем критерии "плохого" пользователя
            is_bad = False

            # 1. Слишком короткое имя
            if len(username) < 2:
                is_bad = True

            # 2. Содержит пробелы
            elif ' ' in username:
                is_bad = True

            # 3. Это служебные слова
            elif username.lower() in ['checking', 'for', 'existing', 'ikev2', 'client',
                                      'name', 'certificate', 'status', 'total', 'clients',
                                      'vpn', 'server', 'admin', 'system']:
                is_bad = True

            # 4. Начинается с цифры или содержит только цифры
            elif username.isdigit():
                is_bad = True

            if is_bad and username not in bad_users:
                bad_users.append(username)

        # Удаляем плохих пользователей
        if bad_users:
            print(f"\n🗑️  Удаляем {len(bad_users)} неправильных пользователей:")
            for username in bad_users:
                try:
                    # Удаляем связанные данные
                    cursor.execute("DELETE FROM active_sessions WHERE username = ?", (username,))
                    cursor.execute("DELETE FROM user_stats WHERE username = ?", (username,))
                    cursor.execute("DELETE FROM traffic_log WHERE username = ?", (username,))
                    cursor.execute("DELETE FROM session_backup WHERE username = ?", (username,))
                    cursor.execute("DELETE FROM users WHERE username = ?", (username,))
                    print(f"  ✅ Удален: {username}")
                except Exception as e:
                    print(f"  ⚠️  Ошибка удаления {username}: {e}")

            conn.commit()
            print(f"\n✅ Удалено {len(bad_users)} неправильных пользователей")
        else:
            print("\n✅ Неправильных пользователей не найдено")

        conn.close()

    except Exception as e:
        print(f"❌ Ошибка очистки: {str(e)}")


def add_missing_vpn_users():
    """Добавляет отсутствующих VPN пользователей в БД"""
    db_path = Path(__file__).parent / 'users.db'

    if not db_path.exists():
        print(f"❌ Файл базы данных не найден: {db_path}")
        return False

    try:
        # 1. Очищаем неправильных пользователей
        clean_up_bad_users()

        # 2. Получаем реальных VPN пользователей
        real_vpn_users = get_real_vpn_users()

        if not real_vpn_users:
            print("❌ VPN пользователи не найдены")
            return False

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # 3. Получаем существующих пользователей из БД
        cursor.execute("SELECT username FROM users")
        existing_users = [row[0] for row in cursor.fetchall()]

        # 4. Находим отсутствующих
        missing_users = [user for user in real_vpn_users if user not in existing_users]

        if not missing_users:
            print("\n✅ Все VPN пользователи уже в БД!")
            return True

        # 5. Добавляем отсутствующих
        print(f"\n📝 Добавляем {len(missing_users)} отсутствующих пользователей:")
        added_count = 0

        for username in missing_users:
            try:
                cursor.execute(
                    "INSERT INTO users (username, created_by, created_by_username) VALUES (?, ?, ?)",
                    (username, 149999149, "Система")
                )
                added_count += 1
                print(f"  ✅ Добавлен: {username}")
            except sqlite3.IntegrityError:
                print(f"  ⚠️  Уже существует: {username}")
            except Exception as e:
                print(f"  ❌ Ошибка добавления {username}: {str(e)}")

        conn.commit()
        conn.close()

        print(f"\n🎯 Всего добавлено {added_count} пользователей")

        # 6. Показываем итог
        print("\n📊 ИТОГОВАЯ СТАТИСТИКА:")
        print(f"👥 VPN пользователей в системе: {len(real_vpn_users)}")
        print(f"🗄️  Пользователей в БД (после добавления): {len(existing_users) + added_count}")

        return True

    except Exception as e:
        print(f"❌ Ошибка: {str(e)}")
        return False


def check_database_structure():
    """Проверяет структуру базы данных"""
    db_path = Path(__file__).parent / 'users.db'

    if not db_path.exists():
        print(f"❌ Файл базы данных не найден: {db_path}")
        return False

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        print("\n🔧 Проверка структуры БД:")

        # Проверяем все таблицы
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()

        print(f"Найдено таблиц: {len(tables)}")
        for table in tables:
            table_name = table[0]
            print(f"\n📋 Таблица: {table_name}")

            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = cursor.fetchall()

            print(f"  Колонки ({len(columns)}):")
            for col in columns:
                print(f"    • {col[1]} ({col[2]})")

        conn.close()
        return True

    except Exception as e:
        print(f"❌ Ошибка проверки структуры: {str(e)}")
        return False


def main():
    print("=" * 60)
    print("🛠️  СКРИПТ ВОССТАНОВЛЕНИЯ VPN ПОЛЬЗОВАТЕЛЕЙ")
    print("=" * 60)

    # 1. Проверяем структуру
    check_database_structure()

    # 2. Добавляем отсутствующих пользователей
    add_missing_vpn_users()

    print("\n" + "=" * 60)
    print("✅ ВОССТАНОВЛЕНИЕ ЗАВЕРШЕНО!")
    print("=" * 60)


if __name__ == "__main__":
    main()