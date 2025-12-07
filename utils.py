import re
import logging
from datetime import datetime
from config import Config

logger = logging.getLogger(__name__)


def validate_username(username):
    """Валидация имени пользователя"""
    if not username:
        return False, "Имя не может быть пустым"

    username = username.strip()

    if len(username) < Config.MIN_USERNAME_LENGTH:
        return False, f"Имя должно быть не менее {Config.MIN_USERNAME_LENGTH} символов"

    if len(username) > Config.MAX_USERNAME_LENGTH:
        return False, f"Имя должно быть не менее {Config.MIN_USERNAME_LENGTH} символов"

    if not re.match(Config.USERNAME_PATTERN, username):
        return False, "Только латиница, цифры, _ и - без пробелов"

    return True, "OK"


def format_bytes(bytes_size):
    """Форматирует байты в читаемый вид"""
    if bytes_size is None:
        return "0 B"

    try:
        bytes_size = float(bytes_size)
    except (ValueError, TypeError):
        return "0 B"

    if bytes_size < 1024:
        return f"{bytes_size:.0f} B"
    elif bytes_size < 1024 ** 2:
        return f"{bytes_size / 1024:.2f} KB"
    elif bytes_size < 1024 ** 3:
        return f"{bytes_size / (1024 ** 2):.2f} MB"
    elif bytes_size < 1024 ** 4:
        return f"{bytes_size / (1024 ** 3):.2f} GB"
    else:
        return f"{bytes_size / (1024 ** 4):.2f} TB"


def format_traffic_stats(stats):
    """Форматирует статистику трафика для отображения"""
    if not stats:
        return "Статистика не найдена"

    total_traffic = (stats.get('total_bytes_sent', 0) or 0) + (stats.get('total_bytes_received', 0) or 0)
    monthly_traffic = (stats.get('monthly_sent', 0) or 0) + (stats.get('monthly_received', 0) or 0)

    return f"""📊 Статистика пользователя:

📈 Общий трафик: {format_bytes(total_traffic)}
├─ Отправлено: {format_bytes(stats.get('total_bytes_sent', 0))}
└─ Получено: {format_bytes(stats.get('total_bytes_received', 0))}

📅 За последние 30 дней: {format_bytes(monthly_traffic)}
├─ Отправлено: {format_bytes(stats.get('monthly_sent', 0))}
├─ Получено: {format_bytes(stats.get('monthly_received', 0))}
└─ Подключений: {stats.get('monthly_connections', 0)}

🔢 Всего подключений: {stats.get('total_connections', 0)}
{'🟢 Активных сессий: ' + str(stats.get('active_sessions', 0)) if stats.get('active_sessions', 0) > 0 else '⚪ Нет активных сессий'}
{'📅 Последнее подключение: ' + stats.get('last_connected', '')[:19] if stats.get('last_connected') else '📅 Никогда не подключался'}"""


def format_time_delta(seconds):
    """Форматирует разницу времени"""
    if seconds < 60:
        return f"{seconds:.0f} сек"
    elif seconds < 3600:
        return f"{seconds / 60:.1f} мин"
    elif seconds < 86400:
        return f"{seconds / 3600:.1f} час"
    else:
        return f"{seconds / 86400:.1f} дней"


def get_backup_info_text(backup_info):
    """Форматирует информацию о бэкапах"""
    if not backup_info or backup_info.get("total_backups", 0) == 0:
        return "📭 Резервных копий нет"

    text = f"💾 Резервные копии ({backup_info.get('total_backups', 0)} шт., {format_bytes(backup_info.get('total_size', 0))}):\n\n"

    for i, backup in enumerate(backup_info.get("backups", [])[:10], 1):
        text += f"{i}. {backup.get('name', 'unknown')}\n"
        text += f"   📏 Размер: {format_bytes(backup.get('size', 0))}\n"
        text += f"   🕐 Создан: {backup.get('modified', '')[:19] if backup.get('modified') else 'Неизвестно'}\n\n"

    return text


def format_database_info():
    """Форматирует информацию о базе данных"""
    from database import db

    user_count = db.get_user_count()
    active_count = db.get_active_users_count()
    db_size = db.get_database_size()
    backup_info = db.get_backup_info()

    text = f"""🗄️ Информация о базе данных:

👥 Пользователей: {user_count} ({active_count} активных)
📏 Размер БД: {format_bytes(db_size)}
💾 Резервных копий: {backup_info.get('total_backups', 0)} ({format_bytes(backup_info.get('total_size', 0))})

📁 Директория бэкапов: {Config.BACKUP_DIR}"""

    return text