# Состояние проекта (на 13.11.2026)

## Что сделано

- Новый бот: **@video_gnidabot** (токен лежит в локальном `.deploy.env`, в git не попадает).
- Репозиторий: https://github.com/WHEREPOLAND/shorts-video-bot (публичный).
- Render: https://dashboard.render.com/web/srv-dbrijl5ckfvc73ecqq5g
  - URL: https://shorts-video-bot.onrender.com
  - Free, Frankfurt, Docker (ffmpeg + yt-dlp), health check `/ping`.
  - Старый сервис `rostics_bot` / репозиторий `aliexpress-tech-bot` не изменялись.
- Self-ping каждые 10 минут через `RENDER_EXTERNAL_URL` — free-инстанс не засыпает.

## Как работает

1. Пользователь присылает ссылку (Shorts / Reels / TikTok).
2. `yt-dlp` выбирает **H.264/AAC** версию (Telegram её не пережимает).
3. `media.py` разбирает MP4-атомы напрямую (без ffprobe): если файл H.264/AAC и `moov`
   в начале — он уходит в Telegram **как есть**, без remux и перекодировки.
   Если `moov` в конце — быстрый remux. Если кодек не H.264 или файл больше 48 МБ —
   один проход x264 с целевым битрейтом.
4. Видео отправляется в тот же чат с превью, автором и ссылкой. В лог пишется разбивка
   времени: `download`, `prepare`, `upload`.

## Проверено

- Instagram Reels: ~10–12 сек, 720p H.264, без перекодирования.
- TikTok: ~30 сек (19 МБ 1080p H.264), без перекодирования.
- Раньше TikTok обрабатывался 11 минут — качалась HEVC-версия и перекодировалась дважды.
  Исправлено выбором формата.
- Теперь `prepare` = 0.01 с (было 2–3 с на remux): файл проверяется чтением атомов и
  отправляется без ffmpeg.

## Осталось: YouTube Shorts

Google блокирует IP Render проверкой «Sign in to confirm you're not a bot».
Пробовали клиенты `default`, `web_embedded`, `android_vr`, `tv_embedded` — все упираются
в ту же проверку. Нужен один из вариантов:

1. **Cookies YouTube** (проще всего): экспортировать `cookies.txt` из браузера, где выполнен
   вход в YouTube (расширение «Get cookies.txt LOCALLY»), затем:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt"))
   ```
   и положить результат в переменную Render `COOKIES_B64` (Environment → Add Environment Variable),
   после чего redeploy. Бот подхватит cookies автоматически.
2. **Прокси** с резидентным/домашним IP: задать `PROXY_URL` в Render.
3. Если включать cookies не хочется — YouTube можно временно отключить в `ALLOWED_HOSTS`,
   оставив TikTok и Reels.

## Обновление кода

```powershell
cd "C:\Users\dayatip\Documents\Проект по умолчанию\shorts-video-bot"
python tools\publish.py --message "что изменилось"
```

Пуш в `main` автоматически запускает сборку на Render (`autoDeploy: yes`).

## Локальные секреты

- `.deploy.env` — GitHub token, Render API key, Telegram token. В git игнорируется.
- Старый `.deploy.env` в `aliexpress-tech-bot` не изменялся.
