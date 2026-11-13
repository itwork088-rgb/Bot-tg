# Ритм — Telegram Mini App для привычек

Ритм — адаптивный трекер привычек, который открывается внутри Telegram. Данные и история выполнений хранятся на сервере в SQLite, а браузер сохраняет локальную копию для быстрого открытия и первичного переноса старых данных. Изменения добавляются в надёжную очередь и отправляются в Make.com.

## Локальный запуск

Нужен Python 3.10+.

```powershell
python -m pip install -r requirements.txt
python server.py
```

Открой `http://localhost:8000`. В обычном браузере интерфейс работает локально; облачная синхронизация требует запуска Mini App из Telegram с настроенным `TELEGRAM_BOT_TOKEN`.

Для локальной постоянной базы можно задать `DATABASE_PATH`. По умолчанию база создаётся в `data/ritm.sqlite3`.

## Запуск через Docker Compose

Требуются Docker Engine/Desktop и Docker Compose v2.

```powershell
docker compose up -d --build
```

Приложение будет доступно на `http://localhost:8000`. Проверить состояние можно через `http://localhost:8000/api/status`.

Секреты задаются через переменные окружения, а не в образе. Compose читает локальный `.env`; если такого файла ещё нет, создай его из безопасного примера и заполни только необходимые значения:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Compose также запускается без `.env`: значения секретов по умолчанию пустые, поэтому Telegram-аутентификация, MCP и Make.com будут не настроены, пока ты не добавишь соответствующие значения. Не перезаписывай существующий `.env` и не публикуй его.

Остановка, просмотр логов и проверка контейнера:

```powershell
docker compose down
docker compose logs -f
docker compose ps
```

SQLite хранится в именованном Docker volume `ritm-data`, смонтированном в `/data`; по умолчанию `DATABASE_PATH=/data/ritm.sqlite3`. Обычная команда `docker compose down` сохраняет volume и привычки. Не добавляй `-v`, если не хочешь удалить данные. При переопределении `DATABASE_PATH` укажи путь внутри `/data`, иначе база не будет находиться в постоянном volume.

Контейнер запускается от непривилегированного пользователя. `.env`, Git-данные, тесты, локальные базы и Python-кэши исключены из build context; в образ копируются только файлы, необходимые для запуска приложения.

## Переменные окружения

Храни секреты только в локальном `.env` или в настройках окружения Render — не в JavaScript и не в Git.

| Переменная | Назначение |
|---|---|
| `PORT` | Порт веб-сервера; Render задаёт его автоматически. |
| `TELEGRAM_BOT_TOKEN` | Токен бота из BotFather для проверки Telegram `initData`. |
| `MAKE_WEBHOOK_URL` | URL Custom Webhook сценария Make.com, получающего события привычек. |
| `DATABASE_PATH` | Путь к SQLite. На Render укажи путь внутри подключённого persistent disk, например `/var/data/ritm.sqlite3`. |
| `MCP_AUTH_TOKEN` | Случайный секрет для Bearer-аутентификации нового MCP endpoint приложения. |
| `MCP_ALLOWED_TELEGRAM_USER_IDS` | Разрешённые числовые Telegram user ID через запятую. MCP не разрешает доступ, пока список пуст. |

Узнать Telegram user ID можно через `initDataUnsafe.user.id` внутри Mini App или временно залогировать проверенный `user["id"]` на сервере. Не логируй `initData` или токен бота.

## Render

1. Создай или используй существующий Web Service с командой запуска `python server.py`.
2. Подключи persistent disk и задай `DATABASE_PATH` на путь внутри точки монтирования. Без persistent disk данные SQLite могут потеряться при перезапуске или redeploy.
3. Задай `TELEGRAM_BOT_TOKEN` и `MAKE_WEBHOOK_URL`.
4. Для MCP задай `MCP_AUTH_TOKEN` (случайная строка не короче 32 байт) и `MCP_ALLOWED_TELEGRAM_USER_IDS`.
5. После redeploy проверь `https://<адрес-сервиса>/api/status`. Endpoint `/mcp` закрыт Bearer-токеном; не публикуй его без HTTPS.
6. В BotFather задай Menu Button / Web App URL как HTTPS-адрес этого сервиса.

## Make.com

Сценарий Make должен начинаться с **Webhooks → Custom webhook**. URL этого вебхука задаётся только как `MAKE_WEBHOOK_URL` в окружении сервера. События:

- `habit.created`
- `habit.completed`
- `habit.uncompleted`
- `habit.deleted`

Каждое событие содержит `event_id`, `event`, `user.id`, `user.first_name`, `habit.id`, `habit.title`, `habit.icon`, `occurred_at`; события выполнения также содержат `date`. Сервер хранит события в SQLite outbox и повторяет неудачные отправки с увеличивающейся задержкой. Для защиты от дублей используй `event_id` как ключ идемпотентности в Make Data Store или сценарии.

Состояние привычек хранится в приложении и доступно Mini App и MCP. Make получает события для автоматизаций; он не является источником состояния для Mini App.

## MCP в VS Code

Текущее подключение `make-habits` к `eu1.make.com` оставь без изменений: это MCP-коннектор Make.com. MCP endpoint приложения — отдельная служба на Render, поэтому добавь её в VS Code под отдельным именем, например `ritm-habits`.

Добавь в пользовательский `mcp.json` запись, подставив HTTPS-адрес Render:

```json
{
  "inputs": [
    {
      "id": "ritmHabitsToken",
      "type": "promptString",
      "description": "MCP token for Ритм",
      "password": true
    }
  ],
  "servers": {
    "ritm-habits": {
      "type": "http",
      "url": "https://<адрес-сервиса>/mcp",
      "headers": {
        "Authorization": "Bearer ${input:ritmHabitsToken}"
      }
    }
  }
}
```

Не затирай существующий объект `servers` или запись `make-habits`: добавь новый сервер и input к уже имеющимся. При первом подключении VS Code запросит `MCP_AUTH_TOKEN`. Перезапусти новый сервер через MCP Servers UI после сохранения конфигурации.

Инструменты `ritm-habits`:

- `list_habits` — список привычек и отметки на дату;
- `create_habit` — добавить привычку;
- `complete_habit` / `uncomplete_habit` — поставить или снять отметку за дату;
- `delete_habit` — удалить привычку, сохранив историю;
- `get_overview` — прогресс за день и неделю;
- `get_sync_status` — состояние очереди отправки в Make.com.

Если разрешён только один Telegram user ID, `user_id` можно не передавать. Для нескольких разрешённых пользователей его нужно указывать. Передавай `date` в локальном формате `YYYY-MM-DD`; без даты инструменты используют системную дату Render.

## Проверки

```powershell
python -m unittest -v
```

`/api/status` сообщает, настроены ли интеграции и сколько событий ожидают отправки. Сам endpoint не раскрывает секреты.
