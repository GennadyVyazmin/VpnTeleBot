# VPN TeleBot

Telegram-бот для управления VPN пользователями и статистикой.

## Обязательное требование перед установкой бота

Сначала установите IKEv2 VPN сервер:

```bash
wget https://get.vpnsetup.net -O vpn.sh && sudo sh vpn.sh
```

Без этого шага бот не сможет создавать/удалять VPN пользователей.

## Установка бота (рекомендуемый способ)

### В одну строку

```bash
wget -O install.sh https://raw.githubusercontent.com/GennadyVyazmin/VpnTeleBot/master/install.sh && chmod +x install.sh && sudo ./install.sh
```

Скрипт `install.sh`:
- скачает проект целиком;
- установит зависимости;
- спросит `TELEGRAM_BOT_TOKEN` и `SUPER_ADMIN_ID`;
- создаст `.env`;
- создаст и включит systemd-сервис `vpn-telebot`.

## Проверка после установки

```bash
sudo systemctl status vpn-telebot --no-pager -l
sudo journalctl -u vpn-telebot -n 100 --no-pager
```

## Обновление

`<INSTALL_DIR>` — это путь установки, который вы указали в `install.sh` (по умолчанию `/opt/VpnTeleBot`).

```bash
cd <INSTALL_DIR>
sudo ./update.sh
```

## Удаление

```bash
cd <INSTALL_DIR>
sudo ./uninstall.sh
```
