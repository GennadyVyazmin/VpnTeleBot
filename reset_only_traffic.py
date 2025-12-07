#!/usr/bin/env python3
import sqlite3
import sys
from pathlib import Path
from datetime import datetime


def reset_only_traffic():
    """Обнуляет только статистику трафика, сохраняя пользователей"""
    db_path = Path(__file__).parent / 'users.db'

    if not db_path.exists():
        print(f"❌ Файл базы данных не найден: {db_path}")
        return False

    try:
        # Создаем резервную копию
        backup_file = f"users.db.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        import shutil
        shutil.copy2(db_path, backup_file)
        print(f"✅ Создана резервная копия: {backup_file}")

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        print("🔄 Обнуляем статистику трафика...")

        # 1. Обнуляем трафик пользователей
        cursor.execute('''UPDATE users 
                        SET total_bytes_sent = 0,
                            total_bytes_received = 0,
                            total_connections = 0,
                            last_connected = NULL,
                            last_updated = CURRENT_TIMESTAMP''')
        print(f"✅ Обнулен трафик {cursor.rowcount} пользователей")

        # 2. Очищаем таблицу ежедневной статистики
        cursor.execute("DELETE FROM traffic_log")
        print("✅ Очищена таблица traffic_log")

        # 3. Очищаем таблицу детальной статистики
        cursor.execute("DELETE FROM user_stats")
        print("✅ Очищена таблица user_stats")

        # 4. Очищаем таблицу активных сессий
        cursor.execute("DELETE FROM active_sessions")
        print("✅ Очищена таблица active_sessions")

        # 5. Очищаем таблицу резервных копий сессий
        cursor.execute("DELETE FROM session_backup")
        print("✅ Очищена таблица session_backup")

        # 6. Сбрасываем статус активности
        cursor.execute("UPDATE users SET is_active = 0")
        print("✅ Сброшен статус активности")

        conn.commit()
        conn.close()

        print("\n✅ Вся статистика трафика обнулена!")
        print("📊 Пользователи и администраторы сохранены.")
        print("🚀 Теперь статистика будет считаться заново с 0.")

        return True

    except Exception as e:
        print(f"❌ Ошибка: {str(e)}")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("🔄 СКРИПТ ОБНУЛЕНИЯ СТАТИСТИКИ ТРАФИКА")
    print("=" * 60)
    print("\nЧто будет сделано:")
    print("• Обнулен трафик всех пользователей")
    print("• Удалена ежедневная статистика")
    print("• Удалена история подключений")
    print("• Сброшены активные сессии")
    print("• СОХРАНЕНЫ все пользователи и администраторы")
    print("\n⚠️  Это действие необратимо!")

    confirm = input("\nПродолжить? (yes/NO): ")
    if confirm.lower() == 'yes':
        if reset_only_traffic():
            sys.exit(0)
        else:
            sys.exit(1)
    else:
        print("❌ Отменено")
        sys.exit(0)