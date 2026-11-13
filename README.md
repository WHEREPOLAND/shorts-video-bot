# shorts-video-bot

Telegram-бот для групповых чатов: присылаете ссылку на **YouTube Shorts**, **Instagram Reels**
или **TikTok** — бот скачивает видео в лучшем доступном качестве и отправляет его в этот же чат.

## Что умеет

- YouTube Shorts, Instagram Reels, TikTok (включая короткие ссылки `vm.tiktok.com`).
- Выбирает лучшее качество до 1080p, предпочитая H.264/AAC в MP4 — такие файлы Telegram
  не пережимает и отдаёт как есть.
- Если исходник больше лимита Telegram (50 МБ), аккуратно сжимает: 1080p → 720p → 480p.
- Отправляет видео с превью, названием, автором, длительностью и ссылкой на оригинал.
- Понимает до 3 ссылок в одном сообщении, работает в группах и в личке.
- Не падает от одного плохого видео: ошибки объясняются человеческим языком.
- HTTP-эндпоинт `/ping` для UptimeRobot / Render Health Check.

## Быстрый локальный запуск

1. Создайте бота в [@BotFather](https://t.me/BotFather) и скопируйте токен.
2. Установите ffmpeg (Windows: `winget install Gyan.FFmpeg`, macOS: `brew install ffmpeg`,
   Linux: `apt install ffmpeg`).
3. Установите зависимости и задайте переменные окружения:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   $env:TELEGRAM_TOKEN = "123456:AA..."
   $env:ADMIN_IDS = "957264695"
   python -m bot.main
   ```
   Либо скопируйте `.env.example` в `.env` и используйте `run_local.ps1` (он подхватит `.env`).

## Деплой на Render (Docker)

Проект готов к деплою как **Web Service → Docker**:

- `Dockerfile` уже содержит ffmpeg и зависимости.
- `render.yaml` описывает сервис: план `free`, регион `frankfurt`, health check `/ping`.
- В дашборде Render нужно задать секреты `TELEGRAM_TOKEN` (и при желании `ADMIN_IDS`).
- Бесплатный инстанс засыпает без трафика — бот сам пингует свой `/ping` каждые 10 минут
  (Render передаёт `RENDER_EXTERNAL_URL` автоматически), поэтому в обычной работе не спит.
  Для надёжности можно дополнительно добавить UptimeRobot на `https://<service>.onrender.com/ping`
  каждые 5–10 минут.

Либо через Render Blueprint: **New → Blueprint** → выберите репозиторий.

## Переменные окружения

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `TELEGRAM_TOKEN` | — | Токен бота из BotFather (обязательно) |
| `ADMIN_IDS` | — | ID админов через запятую (доступ к `/stats`) |
| `ALLOWED_CHAT_IDS` | все чаты | Белый список chat_id (если нужно ограничить) |
| `MAX_UPLOAD_MB` | `48` | Лимит файла для Telegram, максимум 49 |
| `MAX_DOWNLOAD_MB` | `512` | Защита от гигантских исходников |
| `MAX_DURATION_SEC` | `600` | Максимальная длительность видео |
| `MAX_CONCURRENT_JOBS` | `1` | Параллельные задачи (на free-плане больше 1 не стоит) |
| `MAX_QUEUE` | `10` | Сколько ссылок может ждать в очереди |
| `MAX_LINKS_PER_MESSAGE` | `3` | Ссылок из одного сообщения |
| `FFMPEG_PRESET` | `veryfast` | На Render free лучше `ultrafast` |
| `PROXY_URL` | — | Прокси для yt-dlp (если платформа блокирует IP сервера) |
| `KEEPALIVE_URL` | `RENDER_EXTERNAL_URL` | Адрес self-ping; на Render задаётся сам |
| `KEEPALIVE_INTERVAL_SEC` | `600` | Интервал self-ping |
| `COOKIES_FILE` | — | Путь к cookies.txt (Netscape) |
| `COOKIES_B64` | — | Cookies.txt в base64 — удобно для Render |
| `ALLOWED_HOSTS` | youtube/youtu.be/instagram/tiktok | Какие домены реагировать |
| `WORK_DIR` | `/tmp/shorts-video-bot` | Временные файлы |
| `LOG_LEVEL` | `INFO` | Логи |

## Настройка группы

1. Добавьте бота в группу.
2. Откройте [@BotFather](https://t.me/BotFather) → `/setprivacy` → выберите бота → **Disable**.
   Иначе бот не увидит обычные сообщения со ссылками.
3. Удалите и добавьте бота в группу заново (или перезапустите сервис), чтобы Telegram применил настройку.
4. Бот отвечает в том чате, куда прислали ссылку — отдельный chat_id настраивать не нужно.

Права администратора боту не нужны — достаточно быть участником.

## Cookies и блокировки

- **Instagram** часто требует авторизацию: экспортируйте cookies из браузера (расширение
  «Get cookies.txt LOCALLY») и положите содержимое в `COOKIES_B64` (base64 от файла).
- **YouTube** может показывать «Sign in to confirm you're not a bot» на серверных IP.
  Помогает `COOKIES_B64` от аккаунта или `PROXY_URL` (резидентный/домашний прокси).
- **TikTok** обычно работает без cookies, но при региональной блокировке нужен `PROXY_URL`.

```powershell
# base64 от cookies.txt (PowerShell)
[Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt"))
```

## Команды

- `/start` — краткая инструкция.
- `/help` — возможности, лимиты, настройка группы.
- `/id` — chat_id и user_id (пригодится для `ALLOWED_CHAT_IDS`).
- `/ping` — проверка живости.
- `/stats` — статистика (только админы).

## Ограничения

- Облачный Telegram Bot API принимает файлы до **50 МБ**. Больше — только через локальный
  Bot API server, здесь он не используется.
- Free-план Render: 512 МБ RAM и слабый CPU, поэтому одновременно обрабатывается одно видео,
  а перекодировка может занять пару минут. Для 95% Shorts/Reels/TikTok перекодировка не нужна —
  файл уходит как есть.
- Скачивание идёт от имени сервера: приватные/региональные видео требуют cookies или прокси.

## Структура

```
bot/
  main.py        — запуск, polling, graceful shutdown
  config.py      — переменные окружения
  handlers.py    — команды и обработка ссылок, очередь задач
  downloader.py  — yt-dlp, выбор формата, понятные ошибки
  media.py       — ffprobe/ffmpeg: remux, превью, сжатие под лимит
  formatting.py  — подпись к видео
  urls.py        — поиск ссылок и проверка доменов
  health.py      — /ping для Render и UptimeRobot
tests/           — unit-тесты ссылок и подписи
```

## Тесты

```powershell
python -m unittest discover -s tests -v
```
