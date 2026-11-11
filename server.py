from dotenv import load_dotenv
load_dotenv()
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse


ROOT = Path(__file__).resolve().parent
MAX_BODY_SIZE = 8_192
MAX_AUTH_AGE_SECONDS = 86_400   


def verify_telegram_init_data(init_data, bot_token):
    if not init_data or not bot_token:
        return None

    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", "")
    if not received_hash:
        return None

    auth_date = fields.get("auth_date")
    try:
        auth_timestamp = int(auth_date)
        if auth_timestamp > int(time.time()) or int(time.time()) - auth_timestamp > MAX_AUTH_AGE_SECONDS:
            return None
    except (TypeError, ValueError):
        return None

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        return None

    try:
        user = json.loads(fields["user"])
    except (KeyError, json.JSONDecodeError):
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            self.send_json(200, {
                "telegramAuthConfigured": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
                "makeConfigured": bool(os.environ.get("MAKE_WEBHOOK_URL")),
            })
            return
        public_files = {"/": "index.html", "/index.html": "index.html", "/styles.css": "styles.css", "/app.js": "app.js"}
        if path not in public_files:
            self.send_json(404, {"error": "Страница не найдена."})
            return
        self.path = "/" + public_files[path]
        super().do_GET()

    def do_HEAD(self):
        self.send_error(405)

    def do_POST(self):
        if urlparse(self.path).path != "/api/events":
            self.send_json(404, {"error": "Неизвестный API-маршрут."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Некорректный размер запроса."})
            return
        if content_length <= 0 or content_length > MAX_BODY_SIZE:
            self.send_json(413, {"error": "Размер запроса должен быть до 8 КБ."})
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Ожидается корректный JSON."})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "Некорректный формат события."})
            return

        user = verify_telegram_init_data(
            self.headers.get("X-Telegram-Init-Data", ""),
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        )
        if not user:
            self.send_json(401, {"error": "Открой Mini App через Telegram, чтобы синхронизировать привычки."})
            return

        event_name = payload.get("event")
        habit = payload.get("habit")
        if event_name not in {
            "habit.created",
            "habit.completed",
            "habit.uncompleted",
            "habit.deleted",
        } or not isinstance(habit, dict):
            self.send_json(400, {"error": "Неподдерживаемое событие."})
            return
        habit_id = habit.get("id")
        title = habit.get("title")
        icon = habit.get("icon", "")
        if not isinstance(habit_id, str) or not 1 <= len(habit_id) <= 80:
            self.send_json(400, {"error": "Некорректный идентификатор привычки."})
            return
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 60:
            self.send_json(400, {"error": "Название привычки должно содержать от 1 до 60 символов."})
            return
        if not isinstance(icon, str) or len(icon) > 8:
            self.send_json(400, {"error": "Некорректный символ привычки."})
            return

        webhook_url = os.environ.get("MAKE_WEBHOOK_URL", "").strip()
        if not webhook_url:
            self.send_json(503, {"error": "Вебхук Make.com ещё не настроен."})
            return

        event = {
            "event": event_name,
            "user": {
                "id": user["id"],
                "first_name": str(user.get("first_name", ""))[:64],
            },
            "habit": {
                "id": habit_id,
                "title": title.strip(),
                "icon": icon,
            },
            "occurred_at": int(time.time()),
        }
        try:
            request = urllib.request.Request(
                webhook_url,
                data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                if not 200 <= response.status < 300:
                    self.send_json(502, {"error": "Make.com не принял событие. Попробуй позже."})
                    return
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"Make.com webhook request failed: {error}")
            self.send_json(502, {"error": "Не удалось связаться с Make.com. Попробуй позже."})
            return

        self.send_json(202, {"ok": True})

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main():
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), AppHandler)
    print(f"Ритм запущен: http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
