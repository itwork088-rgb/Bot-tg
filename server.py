from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv
import hashlib
import hmac
import json
import os
import re
import sqlite3

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # SQLite remains usable when PostgreSQL support is not installed.
    psycopg = None
    dict_row = None
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse


load_dotenv()

ROOT = Path(__file__).resolve().parent
MAX_BODY_SIZE = 8_192
MAX_MCP_BODY_SIZE = 65_536
MAX_BOOTSTRAP_BODY_SIZE = 1_048_576
MAX_AUTH_AGE_SECONDS = 86_400
MAX_HABITS_PER_USER = 100
SUPPORTED_MCP_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}

DATABASE_ERRORS = (sqlite3.Error,) + ((psycopg.Error,) if psycopg else ())
INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + ((psycopg.IntegrityError,) if psycopg else ())
DELIVERY_ERRORS = (urllib.error.URLError, TimeoutError, ValueError) + DATABASE_ERRORS


def verify_telegram_init_data(init_data, bot_token):
    if not init_data or len(init_data) > 4_096 or not bot_token:
        return None

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    if len({key for key, _ in pairs}) != len(pairs):
        return None
    fields = dict(pairs)
    received_hash = fields.pop("hash", "")
    if not received_hash:
        return None

    try:
        auth_timestamp = int(fields["auth_date"])
    except (KeyError, TypeError, ValueError):
        return None
    now = int(time.time())
    if auth_timestamp > now or now - auth_timestamp > MAX_AUTH_AGE_SECONDS:
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
    if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
        return None
    return user


def parse_date(value, default=None):
    if value is None and default is not None:
        return default
    if not isinstance(value, str):
        raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД.") from error
    if parsed.isoformat() != value:
        raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД.")
    return value


def validate_habit(habit):
    if not isinstance(habit, dict):
        raise ValueError("Некорректный формат привычки.")
    habit_id = habit.get("id")
    title = habit.get("title")
    icon = habit.get("icon", "")
    label = habit.get("time", "Маленький шаг")
    if not isinstance(habit_id, str) or not 1 <= len(habit_id) <= 80:
        raise ValueError("Некорректный идентификатор привычки.")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 60:
        raise ValueError("Название привычки должно содержать от 1 до 60 символов.")
    if not isinstance(icon, str) or len(icon) > 8:
        raise ValueError("Некорректный символ привычки.")
    if not isinstance(label, str) or len(label.strip()) > 60:
        raise ValueError("Некорректное описание привычки.")
    return {
        "id": habit_id,
        "title": title.strip(),
        "icon": icon,
        "time": label.strip() or "Маленький шаг",
    }


class PostgresConnection:
    """Compatibility adapter allowing HabitStore to share SQLite-style queries."""

    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _prepare(sql):
        query = sql.strip()
        if query.upper() == "BEGIN IMMEDIATE":
            return None
        ignore_conflicts = bool(re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\b", query, re.IGNORECASE))
        if ignore_conflicts:
            query = re.sub(
                r"INSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO",
                query, count=1, flags=re.IGNORECASE,
            )
        query = query.replace("?", "%s")
        query = query.replace(
            "MAX(accounts.initialized, excluded.initialized)",
            "GREATEST(accounts.initialized, EXCLUDED.initialized)",
        )
        if ignore_conflicts:
            query = query.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return query

    def execute(self, sql, params=()):
        query = self._prepare(sql)
        if query is None:
            return self.connection.cursor()
        return self.connection.execute(query, params)

    def executemany(self, sql, seq_of_params):
        query = self._prepare(sql)
        if query is None:
            return self.connection.cursor()
        return self.connection.cursor().executemany(query, seq_of_params)

    def executescript(self, script):
        cursor = self.connection.cursor()
        for statement in script.split(";"):
            if statement.strip():
                cursor.execute(statement)
        return cursor

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


class HabitStore:
    def __init__(self, path, database_url=None):
        self.database_url = (database_url or "").strip()
        if self.database_url.startswith("postgres://"):
            self.database_url = "postgresql://" + self.database_url[len("postgres://"):]
        self.is_postgres = bool(self.database_url)

        if self.is_postgres:
            if psycopg is None:
                raise RuntimeError("Для PostgreSQL установи зависимость psycopg[binary].")
            with self.connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS accounts (
                        user_id TEXT PRIMARY KEY,
                        initialized INTEGER NOT NULL DEFAULT 0,
                        first_name TEXT NOT NULL DEFAULT '',
                        created_at BIGINT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS habits (
                        user_id TEXT NOT NULL,
                        id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        icon TEXT NOT NULL,
                        time_label TEXT NOT NULL,
                        created_at BIGINT NOT NULL,
                        deleted_at BIGINT,
                        PRIMARY KEY (user_id, id)
                    );
                    CREATE TABLE IF NOT EXISTS completions (
                        user_id TEXT NOT NULL,
                        habit_id TEXT NOT NULL,
                        completion_date TEXT NOT NULL,
                        completed_at BIGINT NOT NULL,
                        PRIMARY KEY (user_id, habit_id, completion_date)
                    );
                    CREATE TABLE IF NOT EXISTS make_outbox (
                        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                        event_id TEXT NOT NULL UNIQUE,
                        user_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at BIGINT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at BIGINT NOT NULL,
                        delivered_at BIGINT,
                        last_error TEXT
                    );
                    CREATE INDEX IF NOT EXISTS make_outbox_pending
                        ON make_outbox (delivered_at, next_attempt_at);
                    ALTER TABLE accounts ADD COLUMN IF NOT EXISTS
                        first_name TEXT NOT NULL DEFAULT '';
                    """
                )
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS accounts (
                        user_id TEXT PRIMARY KEY,
                        initialized INTEGER NOT NULL DEFAULT 0,
                        first_name TEXT NOT NULL DEFAULT '',
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS habits (
                        user_id TEXT NOT NULL,
                        id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        icon TEXT NOT NULL,
                        time_label TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        deleted_at INTEGER,
                        PRIMARY KEY (user_id, id)
                    );
                    CREATE TABLE IF NOT EXISTS completions (
                        user_id TEXT NOT NULL,
                        habit_id TEXT NOT NULL,
                        completion_date TEXT NOT NULL,
                        completed_at INTEGER NOT NULL,
                        PRIMARY KEY (user_id, habit_id, completion_date)
                    );
                    CREATE TABLE IF NOT EXISTS make_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        user_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at INTEGER NOT NULL,
                        delivered_at INTEGER,
                        last_error TEXT
                    );
                    CREATE INDEX IF NOT EXISTS make_outbox_pending
                        ON make_outbox (delivered_at, next_attempt_at);
                    """
                )
                account_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(accounts)")
                }
                if "first_name" not in account_columns:
                    connection.execute(
                        "ALTER TABLE accounts ADD COLUMN first_name TEXT NOT NULL DEFAULT ''"
                    )

    @contextmanager
    def connect(self):
        if self.is_postgres:
            raw_connection = psycopg.connect(
                self.database_url,
                row_factory=dict_row,
                connect_timeout=10,
            )
            connection = PostgresConnection(raw_connection)
        else:
            raw_connection = sqlite3.connect(self.path, timeout=30)
            raw_connection.row_factory = sqlite3.Row
            raw_connection.execute("PRAGMA busy_timeout = 30000")
            connection = raw_connection
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_account(self, connection, user_id, initialized=False, first_name=""):
        connection.execute(
            """
            INSERT INTO accounts (user_id, initialized, first_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                initialized = MAX(accounts.initialized, excluded.initialized),
                first_name = CASE
                    WHEN excluded.first_name != '' THEN excluded.first_name
                    ELSE accounts.first_name
                END
            """,
            (str(user_id), int(initialized), first_name[:64], int(time.time())),
        )

    def get_state(self, user_id, today, first_name=""):
        with self.connect() as connection:
            self.ensure_account(connection, user_id, first_name=first_name)
            account = connection.execute(
                "SELECT initialized FROM accounts WHERE user_id = ?", (str(user_id),)
            ).fetchone()
            habits = connection.execute(
                """
                SELECT id, title, icon, time_label AS time
                FROM habits
                WHERE user_id = ? AND deleted_at IS NULL
                ORDER BY created_at, id
                """,
                (str(user_id),),
            ).fetchall()
            cutoff = (date.fromisoformat(today) - timedelta(days=59)).isoformat()
            rows = connection.execute(
                """
                SELECT completion_date, habit_id
                FROM completions
                WHERE user_id = ? AND completion_date >= ? AND completion_date <= ?
                ORDER BY completion_date
                """,
                (str(user_id), cutoff, today),
            ).fetchall()
        history = {}
        for row in rows:
            history.setdefault(row["completion_date"], []).append(row["habit_id"])
        return {
            "initialized": bool(account["initialized"]),
            "date": today,
            "habits": [dict(row) for row in habits],
            "done": history.get(today, []),
            "history": history,
        }

    def bootstrap(self, user_id, habits, history, first_name=""):
        validated_habits = [validate_habit(habit) for habit in habits]
        if len(validated_habits) > MAX_HABITS_PER_USER:
            raise ValueError(f"Можно сохранить не больше {MAX_HABITS_PER_USER} привычек.")
        if len({habit["id"] for habit in validated_habits}) != len(validated_habits):
            raise ValueError("Список содержит повторяющиеся идентификаторы привычек.")
        if not isinstance(history, dict) or len(history) > 60:
            raise ValueError("История должна содержать не больше 60 дней.")
        validated_history = []
        habit_ids = {habit["id"] for habit in validated_habits}
        for day, ids in history.items():
            day = parse_date(day)
            if not isinstance(ids, list) or len(ids) > MAX_HABITS_PER_USER:
                raise ValueError("Некорректная запись истории.")
            if any(not isinstance(item, str) or len(item) > 80 for item in ids):
                raise ValueError("Некорректный идентификатор в истории.")
            validated_history.extend((day, habit_id) for habit_id in set(ids))

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.ensure_account(connection, user_id, first_name=first_name)
            account = connection.execute(
                "SELECT initialized FROM accounts WHERE user_id = ?", (str(user_id),)
            ).fetchone()
            if account["initialized"]:
                return False
            now = int(time.time())
            connection.executemany(
                """
                INSERT INTO habits (user_id, id, title, icon, time_label, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (str(user_id), h["id"], h["title"], h["icon"], h["time"], now)
                    for h in validated_habits
                ],
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO completions
                    (user_id, habit_id, completion_date, completed_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (str(user_id), habit_id, day, now)
                    for day, habit_id in validated_history
                ],
            )
            connection.execute(
                "UPDATE accounts SET initialized = 1 WHERE user_id = ?", (str(user_id),)
            )
            return True

    def _enqueue(self, connection, user_id, event_name, habit, completion_date=None, first_name=""):
        event_id = str(uuid.uuid4())
        account = connection.execute(
            "SELECT first_name FROM accounts WHERE user_id = ?", (str(user_id),)
        ).fetchone()
        payload = {
            "event": event_name,
            "event_id": event_id,
            "user": {
                "id": int(user_id),
                "first_name": (first_name or account["first_name"] if account else first_name)[:64],
            },
            "habit": {
                "id": habit["id"],
                "title": habit["title"],
                "icon": habit["icon"],
            },
            "occurred_at": int(time.time()),
        }
        if completion_date:
            payload["date"] = completion_date
        now = int(time.time())
        connection.execute(
            """
            INSERT INTO make_outbox (event_id, user_id, payload, created_at, next_attempt_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, str(user_id), json.dumps(payload, ensure_ascii=False), now, now),
        )

    def apply_event(self, user_id, event_name, habit, completion_date, first_name=""):
        habit = validate_habit(habit)
        if event_name not in {
            "habit.created",
            "habit.completed",
            "habit.uncompleted",
            "habit.deleted",
        }:
            raise ValueError("Неподдерживаемое событие.")
        changed = False
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.ensure_account(
                connection, user_id, initialized=True, first_name=first_name
            )
            now = int(time.time())
            if event_name == "habit.created":
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM habits WHERE user_id = ? AND deleted_at IS NULL",
                    (str(user_id),),
                ).fetchone()["count"]
                if count >= MAX_HABITS_PER_USER:
                    raise ValueError(f"Можно сохранить не больше {MAX_HABITS_PER_USER} привычек.")
                connection.execute(
                    """
                    INSERT INTO habits (user_id, id, title, icon, time_label, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(user_id), habit["id"], habit["title"], habit["icon"], habit["time"], now,
                    ),
                )
                changed = True
            elif event_name == "habit.deleted":
                result = connection.execute(
                    """
                    UPDATE habits SET deleted_at = ?
                    WHERE user_id = ? AND id = ? AND deleted_at IS NULL
                    """,
                    (now, str(user_id), habit["id"]),
                )
                if result.rowcount == 0:
                    raise LookupError("Привычка не найдена.")
                changed = True
            else:
                exists = connection.execute(
                    """
                    SELECT 1 FROM habits
                    WHERE user_id = ? AND id = ? AND deleted_at IS NULL
                    """,
                    (str(user_id), habit["id"]),
                ).fetchone()
                if not exists:
                    raise LookupError("Привычка не найдена.")
                if event_name == "habit.completed":
                    result = connection.execute(
                        """
                        INSERT OR IGNORE INTO completions
                            (user_id, habit_id, completion_date, completed_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (str(user_id), habit["id"], completion_date, now),
                    )
                else:
                    result = connection.execute(
                        """
                        DELETE FROM completions
                        WHERE user_id = ? AND habit_id = ? AND completion_date = ?
                        """,
                        (str(user_id), habit["id"], completion_date),
                    )
                changed = result.rowcount > 0
            if changed:
                self._enqueue(
                    connection, user_id, event_name, habit,
                    completion_date if event_name in {"habit.completed", "habit.uncompleted"} else None,
                    first_name,
                )
        return changed

    def next_outbox_events(self, now, limit=20):
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, event_id, payload, attempts
                    FROM make_outbox
                    WHERE delivered_at IS NULL AND next_attempt_at <= ?
                    ORDER BY id
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
            ]

    def mark_delivered(self, event_id):
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE make_outbox SET delivered_at = ?, last_error = NULL
                WHERE event_id = ? AND delivered_at IS NULL
                """,
                (int(time.time()), event_id),
            )

    def mark_failed(self, event_id, attempts, reason):
        retry_at = int(time.time()) + min(5 * (2 ** min(attempts, 8)), 900)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE make_outbox
                SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?
                WHERE event_id = ? AND delivered_at IS NULL
                """,
                (retry_at, reason[:300], event_id),
            )

    def sync_status(self, user_id):
        with self.connect() as connection:
            if user_id is None:
                rows = connection.execute(
                    """
                    SELECT COUNT(*) AS pending,
                           COALESCE(SUM(attempts), 0) AS attempts,
                           MAX(created_at) AS latest_event
                    FROM make_outbox
                    WHERE delivered_at IS NULL
                    """
                ).fetchone()
                return dict(rows)
            rows = connection.execute(
                """
                SELECT COUNT(*) AS pending,
                       COALESCE(SUM(attempts), 0) AS attempts,
                       MAX(created_at) AS latest_event
                FROM make_outbox
                WHERE user_id = ? AND delivered_at IS NULL
                """,
                (str(user_id),),
            ).fetchone()
        return dict(rows)


def configured_store():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    path = os.environ.get("DATABASE_PATH", str(ROOT / "data" / "ritm.sqlite3"))
    return HabitStore(path, database_url=database_url or None)


STORE = None
STORE_LOCK = threading.Lock()
OUTBOX_STOP = threading.Event()


def get_store():
    global STORE
    if STORE is None:
        with STORE_LOCK:
            if STORE is None:
                STORE = configured_store()
    return STORE


def send_make_event(payload):
    webhook_url = os.environ.get("MAKE_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise ValueError("MAKE_WEBHOOK_URL is not configured.")
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        if not 200 <= response.status < 300:
            raise urllib.error.HTTPError(
                webhook_url, response.status, "Make.com returned a non-success status", response.headers, None
            )


def make_delivery_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"Make.com returned HTTP {error.code}."
    if isinstance(error, urllib.error.URLError):
        return f"Make.com connection failed ({type(error.reason).__name__})."
    if isinstance(error, TimeoutError):
        return "Make.com request timed out."
    if isinstance(error, DATABASE_ERRORS):
        return "Local Make.com outbox database error."
    return "Make.com webhook is not configured."


def run_outbox():
    while not OUTBOX_STOP.wait(3):
        if not os.environ.get("MAKE_WEBHOOK_URL", "").strip():
            continue
        try:
            events = get_store().next_outbox_events(int(time.time()))
        except DATABASE_ERRORS as error:
            print(f"Make.com outbox read failed: {error}")
            continue
        for event in events:
            try:
                send_make_event(json.loads(event["payload"]))
                get_store().mark_delivered(event["event_id"])
            except DELIVERY_ERRORS as error:
                safe_error = make_delivery_error(error)
                print(f"Make.com event delivery failed: {safe_error}")
                try:
                    get_store().mark_failed(event["event_id"], event["attempts"] + 1, safe_error)
                except DATABASE_ERRORS as database_error:
                    print(f"Make.com outbox update failed: {database_error}")
                break


def allowed_mcp_users():
    values = os.environ.get("MCP_ALLOWED_TELEGRAM_USER_IDS", "")
    return {value.strip() for value in values.split(",") if value.strip().isdigit()}


def resolve_mcp_user(arguments):
    allowed = allowed_mcp_users()
    if not allowed:
        raise PermissionError("MCP_ALLOWED_TELEGRAM_USER_IDS is not configured.")
    requested = arguments.get("user_id")
    if requested is None and len(allowed) == 1:
        return next(iter(allowed))
    if not isinstance(requested, (str, int)) or not str(requested).isdigit():
        raise ValueError("Передай числовой user_id Telegram.")
    user_id = str(requested)
    if user_id not in allowed:
        raise PermissionError("Этот Telegram user_id не разрешён для MCP.")
    return user_id


def mcp_tool_definitions():
    user_id = {"type": "string", "description": "Числовой Telegram user_id; можно опустить при одном разрешённом пользователе."}
    return [
        {
            "name": "list_habits",
            "description": "Показать активные привычки и отметки выполнения за выбранную дату.",
            "inputSchema": {
                "type": "object",
                "properties": {"user_id": user_id, "date": {"type": "string", "description": "Дата YYYY-MM-DD."}},
                "additionalProperties": False,
            },
        },
        {
            "name": "create_habit",
            "description": "Добавить привычку и поставить событие в очередь синхронизации Make.com.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user_id": user_id,
                    "title": {"type": "string", "minLength": 1, "maxLength": 60},
                    "icon": {"type": "string", "maxLength": 8},
                    "time": {"type": "string", "maxLength": 60},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
        {
            "name": "complete_habit",
            "description": "Отметить привычку выполненной за указанную дату (по умолчанию сегодня на сервере).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user_id": user_id,
                    "habit_id": {"type": "string"},
                    "date": {"type": "string", "description": "Локальная дата YYYY-MM-DD."},
                },
                "required": ["habit_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "uncomplete_habit",
            "description": "Снять отметку выполнения привычки за указанную дату.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user_id": user_id,
                    "habit_id": {"type": "string"},
                    "date": {"type": "string", "description": "Локальная дата YYYY-MM-DD."},
                },
                "required": ["habit_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "delete_habit",
            "description": "Удалить привычку, сохранив её прошлую историю выполнения.",
            "inputSchema": {
                "type": "object",
                "properties": {"user_id": user_id, "habit_id": {"type": "string"}},
                "required": ["habit_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_overview",
            "description": "Показать прогресс привычек за дату и число выполнений за последние семь дней.",
            "inputSchema": {
                "type": "object",
                "properties": {"user_id": user_id, "date": {"type": "string", "description": "Дата YYYY-MM-DD."}},
                "additionalProperties": False,
            },
        },
        {
            "name": "get_sync_status",
            "description": "Проверить очередь отправки событий привычек в Make.com.",
            "inputSchema": {"type": "object", "properties": {"user_id": user_id}, "additionalProperties": False},
        },
    ]


def call_mcp_tool(name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("Аргументы инструмента должны быть объектом JSON.")
    store = get_store()
    user_id = resolve_mcp_user(arguments)
    today = parse_date(arguments.get("date"), date.today().isoformat())
    if name == "list_habits":
        return store.get_state(user_id, today)
    if name == "get_overview":
        state = store.get_state(user_id, today)
        week_start = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
        with store.connect() as connection:
            week_count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM completions
                WHERE user_id = ? AND completion_date >= ? AND completion_date <= ?
                """,
                (user_id, week_start, today),
            ).fetchone()["count"]
        return {
            "date": today,
            "habit_count": len(state["habits"]),
            "completed_today": len(state["done"]),
            "week_completions": week_count,
            "habits": state["habits"],
        }
    if name == "get_sync_status":
        return {
            **store.sync_status(user_id),
            "make_configured": bool(os.environ.get("MAKE_WEBHOOK_URL", "").strip()),
        }

    habit_id = arguments.get("habit_id")
    if name == "create_habit":
        title = arguments.get("title")
        habit = {
            "id": str(uuid.uuid4()),
            "title": title,
            "icon": arguments.get("icon", "✳"),
            "time": arguments.get("time", "Создано через Copilot"),
        }
        habit = validate_habit(habit)
        store.apply_event(user_id, "habit.created", habit, today)
        return {"ok": True, "habit": habit, "make_event_queued": True}
    if name in {"complete_habit", "uncomplete_habit", "delete_habit"}:
        if not isinstance(habit_id, str) or not 1 <= len(habit_id) <= 80:
            raise ValueError("Передай корректный habit_id.")
        state = store.get_state(user_id, today)
        habit = next((item for item in state["habits"] if item["id"] == habit_id), None)
        if not habit:
            raise LookupError("Привычка не найдена.")
        event_name = {
            "complete_habit": "habit.completed",
            "uncomplete_habit": "habit.uncompleted",
            "delete_habit": "habit.deleted",
        }[name]
        changed = store.apply_event(user_id, event_name, habit, today)
        return {"ok": True, "changed": changed, "habit_id": habit_id, "make_event_queued": changed}
    raise LookupError(f"Неизвестный MCP-инструмент: {name}.")


def handle_mcp_request(payload):
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params", {})
    if payload.get("jsonrpc") != "2.0" or not isinstance(method, str) or not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Invalid Request"}}, None
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_MCP_VERSIONS else "2025-03-26"
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ritm-habits", "version": "1.0.0"},
            "instructions": "Инструменты управляют привычками Telegram Mini App. Изменения синхронизируются с Make.com.",
        }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}, version
    if method == "notifications/initialized":
        return None, None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}, None
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": mcp_tool_definitions()},
        }, None
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        try:
            result = call_mcp_tool(tool_name, arguments)
            response = {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": result,
                "isError": False,
            }
        except (ValueError, PermissionError, LookupError) + DATABASE_ERRORS as error:
            response = {
                "content": [{"type": "text", "text": str(error)}],
                "isError": True,
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": response}, None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }, None


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/mcp":
            self.send_json(405, {"error": "Используй POST для MCP Streamable HTTP."}, {"Allow": "POST"})
            return
        if path == "/api/status":
            pending = get_store().sync_status(None)["pending"]
            self.send_json(200, {
                "telegramAuthConfigured": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
                "makeConfigured": bool(os.environ.get("MAKE_WEBHOOK_URL")),
                "mcpConfigured": bool(os.environ.get("MCP_AUTH_TOKEN")) and bool(allowed_mcp_users()),
                "pendingMakeEvents": pending,
            })
            return
        if path == "/api/habits":
            user = verify_telegram_init_data(
                self.headers.get("X-Telegram-Init-Data", ""),
                os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            )
            if not user:
                self.send_json(401, {"error": "Открой Mini App через Telegram, чтобы загрузить привычки."})
                return
            try:
                query = dict(parse_qsl(urlparse(self.path).query, keep_blank_values=True))
                requested_date = parse_date(query.get("date"), date.today().isoformat())
                self.send_json(200, get_store().get_state(
                    user["id"], requested_date, str(user.get("first_name", ""))
                ))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except DATABASE_ERRORS as error:
                print(f"Failed to load habits: {error}")
                self.send_json(500, {"error": "Не удалось загрузить привычки с сервера."})
            return

        public_files = {
            "/": "index.html",
            "/index.html": "index.html",
            "/styles.css": "styles.css",
            "/app.js": "app.js",
        }
        if path not in public_files:
            self.send_json(404, {"error": "Страница не найдена."})
            return
        self.path = "/" + public_files[path]
        super().do_GET()

    def do_HEAD(self):
        self.send_error(405)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/mcp":
            self.handle_mcp()
            return
        if path not in {"/api/events", "/api/bootstrap"}:
            self.send_json(404, {"error": "Неизвестный API-маршрут."})
            return

        payload = self.read_json(
            MAX_BOOTSTRAP_BODY_SIZE if path == "/api/bootstrap" else MAX_BODY_SIZE
        )
        if payload is None:
            return
        user = verify_telegram_init_data(
            self.headers.get("X-Telegram-Init-Data", ""),
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        )
        if not user:
            self.send_json(401, {"error": "Открой Mini App через Telegram, чтобы синхронизировать привычки."})
            return
        if path == "/api/bootstrap":
            self.handle_bootstrap(user, payload)
            return
        self.handle_event(user, payload)

    def read_json(self, max_size):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Некорректный размер запроса."})
            return None
        if content_length <= 0 or content_length > max_size:
            self.send_json(413, {"error": f"Размер запроса должен быть до {max_size // 1024} КБ."})
            return None
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Ожидается корректный JSON."})
            return None
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "Некорректный формат запроса."})
            return None
        return payload

    def handle_bootstrap(self, user, payload):
        try:
            imported = get_store().bootstrap(
                user["id"],
                payload.get("habits", []),
                payload.get("history", {}),
                str(user.get("first_name", "")),
            )
            self.send_json(200, {"ok": True, "imported": imported})
        except (ValueError,) + DATABASE_ERRORS as error:
            print(f"Failed to bootstrap habits: {error}")
            self.send_json(400 if isinstance(error, ValueError) else 500, {"error": str(error)})

    def handle_event(self, user, payload):
        event_name = payload.get("event")
        completion_date = None
        try:
            if event_name in {"habit.completed", "habit.uncompleted"}:
                completion_date = parse_date(payload.get("date"), date.today().isoformat())
            changed = get_store().apply_event(
                user["id"],
                event_name,
                payload.get("habit"),
                completion_date,
                str(user.get("first_name", "")),
            )
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        except LookupError as error:
            self.send_json(404, {"error": str(error)})
            return
        except INTEGRITY_ERRORS:
            self.send_json(409, {"error": "Привычка с таким идентификатором уже существует."})
            return
        except DATABASE_ERRORS as error:
            print(f"Failed to store habit event: {error}")
            self.send_json(500, {"error": "Не удалось сохранить изменение на сервере."})
            return
        self.send_json(202, {
            "ok": True,
            "changed": changed,
            "queuedForMake": changed,
            "makeConfigured": bool(os.environ.get("MAKE_WEBHOOK_URL", "").strip()),
        })

    def handle_mcp(self):
        token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
        if not token:
            self.send_json(503, {"error": "MCP_AUTH_TOKEN is not configured."})
            return
        authorization = self.headers.get("Authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.encode("utf-8"), supplied.strip().encode("utf-8")
        ):
            self.send_json(401, {"error": "Invalid MCP bearer token."}, {"WWW-Authenticate": "Bearer"})
            return
        payload = self.read_json(MAX_MCP_BODY_SIZE)
        if payload is None:
            return
        response, protocol_version = handle_mcp_request(payload)
        if response is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        headers = {}
        if protocol_version:
            headers["MCP-Protocol-Version"] = protocol_version
        self.send_json(200, response, headers)

    def send_json(self, status, payload, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)


def main():
    port = int(os.environ.get("PORT", "8000"))
    threading.Thread(target=run_outbox, name="make-outbox", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), AppHandler)
    print(f"Ритм запущен: http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
    finally:
        OUTBOX_STOP.set()
        server.server_close()


if __name__ == "__main__":
    main()
