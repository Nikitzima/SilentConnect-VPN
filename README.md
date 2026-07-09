# 🛡️ SilentConnect VPN (Backend & Bot)

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-Web%20Framework-lightgrey.svg)](https://flask.palletsprojects.com/)
[![Aiogram](https://img.shields.io/badge/aiogram-Telegram%20Bot-blue.svg)](https://docs.aiogram.dev/)
[![Xray](https://img.shields.io/badge/Xray--core-VLESS%20%7C%20REALITY-purple)](https://github.com/XTLS/Xray-core)

**SilentConnect** — это коммерческий сервис предоставления отказоустойчивого VPN-доступа. Данный репозиторий содержит исходный код backend-микросервисов и Telegram-бота, написанных на Python. 

> **Примечание:** В целях безопасности из этого публичного репозитория удалены все `.env` файлы с ключами API, токены, базы данных клиентов и специфические конфигурации серверов. Код представлен в качестве портфолио.

## 🏗 Архитектура проекта

Инфраструктура построена на базе выделенных серверов (Hetzner, Aeza) под управлением Ubuntu/Debian. Сервис обеспечивает обход DPI (Deep Packet Inspection) за счет современных протоколов.

### Основные компоненты:
1. **Telegram-бот (`vpn-shop`)** — Написан на `aiogram`. Обрабатывает регистрацию пользователей, выдачу триал-периодов, интеграцию с платежными шлюзами и генерацию персональных подписок.
2. **Микросервис подписок (`subjson-service`)** — Написан на `Flask`. Динамически генерирует JSON-конфигурации для клиентов.
3. **Маршрутизация и Web-сервер** — Используется `Caddy` в качестве reverse-proxy. Caddy принимает HTTPS-трафик на порт 4430 и распределяет его на микросервисы.
4. **Ядро VPN** — `Xray-core` + `3X-UI`. Мультиплексирование портов настроено через `dokodemo-door`.
5. **Автоматизация инфраструктуры (IaC)** — PowerShell-скрипты для автоматического развертывания серверов в Yandex Cloud (VPC, Security Groups, статические IP, Ubuntu VM) через `yc cli` и их первичной настройки.

## 🚀 Протоколы и профили подключения

Для каждого клиента генерируется 5 профилей, обеспечивающих отказоустойчивость:
* **🛡️ Классический** — VLESS + TCP + REALITY
* **⚡ Быстрый** — VLESS + TCP + REALITY (с альтернативным SNI для обхода локальных блокировок)
* **🚀 Скоростной** — Hysteria2 (UDP/QUIC) для максимальной скорости потокового видео
* **🔐 Запасной** — VLESS + gRPC + REALITY (защита от блокировки TCP/TLS паттернов)
* **🌊 Незаметный** — VLESS + XHTTP (HTTP/3)

## 💡 AI-Driven Development

В разработке данного проекта, дебаггинге сетевых маршрутов и написании системных `systemd` демонов активно применялись агентные AI-системы (**Antigravity**, **Claude Code**, **Codex**). Это позволило ускорить процесс написания микросервисов, быстро анализировать TCP-дампы (`tcpdump`) и автоматизировать рутинные задачи администрирования серверов Linux.

## 🛠 Технологический стек
* **Языки:** Python, Bash, SQL
* **Фреймворки:** Flask, aiogram
* **БД:** SQLite, PostgreSQL (на проде)
* **Инфраструктура:** Linux (Ubuntu), systemd, Caddy, Xray-core, Hysteria2
* **Инструменты:** Git, VS Code, Cursor, AI Agents
