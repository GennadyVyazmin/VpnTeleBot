#!/usr/bin/env python3
import sqlite3
import sys
from pathlib import Path


def migrate_database():
    """Мигрирует существующую базу данных к новой структуре"""
    db_path = Path(__file__).parent / 'users.db'

    if not db_path.exists():
        print(f"❌ Файл базы данных не найден: {db_path}")
        return False

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Проверяем структуру таблицы users
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        print(f"Существующие колонки: {columns}")

        # Добавляем недостающие колонки
        if 'last_updated' not in columns:
            print("Добавляем колонку last_updated...")
            cursor.execute("ALTER TABLE users ADD COLUMN last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

        # Создаем новые таблицы если их нет
        print("Создаем таблицу active_sessions...")
        cursor.execute('''CREATE TABLE IF NOT EXISTS active_sessions (
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

        print("Создаем таблицу session_backup...")
        cursor.execute('''CREATE TABLE IF NOT EXISTS session_backup (
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

        conn.commit()
        print("✅ Миграция завершена успешно!")
        return True

    except Exception as e:
        print(f"❌ Ошибка миграции: {str(e)}")
        return False
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    print("Миграция базы данных...")
    if migrate_database():
        print("✅ Готово! Теперь можно запускать бота.")
        sys.exit(0)
    else:
        print("❌ Миграция не удалась")
        sys.exit(1)