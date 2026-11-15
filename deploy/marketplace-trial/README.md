# Тестовая сборка ru-marketplace-mcp

Используется upstream 2.9.3, commit `f019e9ac41555c8509d50777cbafa0ab37fec8c5` из `Vladimir-Human/ru-marketplace-mcp`. Его отслеживаемые файлы сохраняются в исходном виде. Эти установщики находятся отдельно от upstream.

Установленные административные файлы находятся в `/opt/marketplace-trial-admin` на Pi и VPS. Команды ниже запускаются из этой папки. Лимит навигации Chromium настроен на 45 секунд, запуск MCP ожидает loopback-порты до 60 секунд.

## Размещение

- Исходники и отдельное Python-окружение находятся в `/opt/ru-marketplace-mcp` на Pi.
- Пользователь `marketplace-trial` имеет собственный профиль Chromium в `/var/lib/marketplace-trial/chrome`.
- HTTP MCP слушает `127.0.0.1:8790`, Chromium CDP слушает `127.0.0.1:9223`.
- Конфигурация и секрет авторизации находятся в `/etc/marketplace-trial/server.env`, доступ 0640.
- Отдельная таблица `inet marketplace_trial` и правило приоритета 51 направляют трафик этого UID через домашнего провайдера. Проверен внешний IP со страной RU.
- Обратное SSH-соединение публикует MCP на `127.0.0.1:8791` VPS. Оно использует отдельный ключ, фиксированный host key и пользователя с разрешением на этот порт.
- Существующий Secure MCP Tunnel Research направлен на новый endpoint. Bearer-секрет на VPS передаётся службе через зашифрованный systemd credential. Включена проверка tenant.

Включены Wildberries, Ozon, Яндекс Маркет, Avito, AliExpress, DNS, Ситилинк, Детский мир, Lamoda, Мегамаркет и общее сравнение. Опубликованы 35 инструментов. Выбор источников задаётся `MARKETPLACE_SOURCES`.

## Установка на проверенную инфраструктуру

Сценарий рассчитан на исследованный Pi с Debian ARM64, Python 3.13, Chromium, Xvfb, xauth, Git, nftables и работающим sing-box с bypass-маркером `0x2024`. При первом запуске создаются резервные снимки маршрутизации и контрольные суммы VPN-конфигураций. Установщик требует свободный приоритет маршрута 51 и выбранные loopback-порты.

Скопируйте эту папку на Pi и выполните установку.

```sh
sudo python3 install.py
sudo python3 link.py pi-export
```

Передайте `/tmp/marketplace-link.pub` и `/tmp/marketplace-bearer` с Pi на VPS через SSH/SCP, сохраняя bearer-файл приватным. На VPS выполните подготовку.

```sh
sudo python3 link.py vps-prepare
```

Передайте полученный публичный `/tmp/marketplace-vps-host.pub` с доверенного VPS на Pi, затем настройте обратное соединение.

```sh
sudo python3 link.py pi-link
```

После проверки listener `127.0.0.1:8791` на VPS переключите Research.

```sh
sudo python3 link.py vps-activate
```

Удалите временные bearer-файлы на обеих машинах и локальном компьютере. В ChatGPT выполните Refresh подключения Research для загрузки новых инструментов. Протокол MCP, авторизация, список 35 инструментов и readiness туннеля проверены через VPS. Список в текущем чате может сохраняться до обновления подключения.

## Проверки

Из папки upstream на Pi выполните штатные тесты.

```sh
sudo -u marketplace-trial env HOME=/var/lib/marketplace-trial HYPOTHESIS_STORAGE_DIRECTORY=/var/lib/marketplace-trial/hypothesis /opt/ru-marketplace-mcp/.venv/bin/python -m pytest -q -m 'not live and not cdp' --basetemp /var/lib/marketplace-trial/test-tmp --tb=short -o cache_dir=/var/lib/marketplace-trial/pytest-cache
```

Для проверки MCP используйте установленное окружение upstream.

```sh
sudo -u marketplace-trial /opt/ru-marketplace-mcp/.venv/bin/python probe.py --inventory
sudo -u marketplace-trial /opt/ru-marketplace-mcp/.venv/bin/python probe.py --cases smoke-cases.json --output /var/lib/marketplace-trial/smoke.json
sudo -u marketplace-trial /opt/ru-marketplace-mcp/.venv/bin/python probe.py --cases card-cases.json --output /var/lib/marketplace-trial/cards.json
```

`compare_prices` вызывается с `in_stock_only=true`. Ответ содержит также исходные предложения и предупреждения, поэтому для покупки следует использовать предложения с подтверждённым наличием и ценой. Наблюдавшееся поле `cheapest_comparable` включило ToGo Pro при запросе Vali 1 Pro. Совпадение модели необходимо проверять по названию и карточке. Ozon отдельно возвращает обычную и банковскую цену. AliExpress вернул цены поисковых карточек, при чтении отдельных карточек модуль цены остался пустым.

## Управление и откат

```sh
systemctl status marketplace-trial marketplace-trial-browser marketplace-trial-egress marketplace-trial-link
journalctl -u marketplace-trial -u marketplace-trial-browser -n 40
```

Для возврата подключения Research к прежнему серверу выполните на VPS следующую команду и обновите инструменты подключения.

```sh
sudo python3 link.py vps-rollback
```

Для остановки тестовой сборки на Pi выполните следующую команду.

```sh
sudo systemctl disable --now marketplace-trial-link marketplace-trial marketplace-trial-browser marketplace-trial-egress
```
