import subprocess
import os
import re
import logging
from config import Config

logger = logging.getLogger(__name__)


class VPNManager:
    def __init__(self):
        self.script_path = Config.IKEV2_SCRIPT_PATH
        self.profiles_path = Config.VPN_PROFILES_PATH

        if not os.path.exists(self.script_path):
            logger.error(f"Скрипт {self.script_path} не найден!")
            raise FileNotFoundError(f"ikev2.sh не найден")

        if not os.access(self.script_path, os.X_OK):
            logger.error(f"Скрипт {self.script_path} не исполняемый!")
            raise PermissionError(f"Нет прав на выполнение {self.script_path}")

    def create_user(self, username):
        """Создает VPN пользователя"""
        safe_username = re.sub(r'[^a-zA-Z0-9_-]', '', username)

        try:
            command = [self.script_path, '--addclient', safe_username]
            logger.info(f"Выполнение: {' '.join(command)}")

            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                logger.info(f"VPN пользователь {safe_username} создан")

                # Проверяем созданные файлы
                expected_files = [
                    f"{self.profiles_path}{safe_username}.mobileconfig",
                    f"{self.profiles_path}{safe_username}.p12",
                    f"{self.profiles_path}{safe_username}.sswan"
                ]

                created_files = []
                for file_path in expected_files:
                    if os.path.exists(file_path):
                        created_files.append(os.path.basename(file_path))

                if created_files:
                    logger.info(f"Созданные файлы: {', '.join(created_files)}")
                else:
                    logger.warning(f"Файлы конфигурации не найдены")

                return True, "Пользователь VPN создан успешно"
            else:
                error_msg = f"Ошибка создания VPN: {result.stderr}"
                logger.error(error_msg)
                return False, error_msg

        except subprocess.TimeoutExpired:
            error_msg = "Таймаут выполнения команды"
            logger.error(error_msg)
            return False, error_msg
        except Exception as e:
            error_msg = f"Неожиданная ошибка: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def delete_user(self, username):
        """Удаляет VPN пользователя"""
        safe_username = re.sub(r'[^a-zA-Z0-9_-]', '', username)

        try:
            # Отзываем сертификат
            revoke_command = [self.script_path, '--revokeclient', safe_username]
            logger.info(f"Выполнение отзыва: {' '.join(revoke_command)}")

            revoke_result = subprocess.run(
                revoke_command,
                capture_output=True,
                text=True,
                timeout=30,
                input='y\n'
            )

            if revoke_result.returncode == 0:
                logger.info(f"VPN пользователь {safe_username} отозван")

                # Удаляем файлы конфигурации
                config_files = [
                    f"{self.profiles_path}{safe_username}.mobileconfig",
                    f"{self.profiles_path}{safe_username}.p12",
                    f"{self.profiles_path}{safe_username}.sswan"
                ]

                for file_path in config_files:
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                            logger.info(f"Файл удален: {os.path.basename(file_path)}")
                        except Exception as e:
                            logger.warning(f"Не удалось удалить файл {file_path}: {e}")

                return True, "Пользователь VPN удален"
            else:
                error_msg = f"Ошибка удаления: {revoke_result.stderr}"
                logger.error(error_msg)
                return False, error_msg

        except Exception as e:
            error_msg = f"Ошибка при удалении: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def get_profile_path(self, username, profile_type):
        """Возвращает путь к файлу конфигурации"""
        safe_username = re.sub(r'[^a-zA-Z0-9_-]', '', username)

        extensions = {
            'ios': '.mobileconfig',
            'android': '.p12',
            'sswan': '.sswan',
            'macos': '.mobileconfig',
            'win': '.p12'
        }

        if profile_type not in extensions:
            return None

        file_path = f"{self.profiles_path}{safe_username}{extensions[profile_type]}"
        return file_path if os.path.exists(file_path) else None


# Глобальный экземпляр VPN менеджера
vpn_manager = VPNManager()