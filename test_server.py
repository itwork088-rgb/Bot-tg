import hashlib
import hmac
import json
import os
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

import server


USER_ID = 123456789
TODAY = date.today().isoformat()


def signed_init_data(bot_token, user_id=USER_ID, auth_date=None):
    fields = {
        "auth_date": str(auth_date or int(server.time.time())),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class HabitStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = server.HabitStore(Path(self.temp_dir.name) / "habits.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_bootstrap_imports_only_once(self):
        habits = [{"id": "water", "title": "Вода", "icon": "◌", "time": "Утро"}]
        history = {TODAY: ["water"]}
        self.assertTrue(self.store.bootstrap(USER_ID, habits, history))
        self.assertFalse(self.store.bootstrap(USER_ID, [], {}))
        state = self.store.get_state(USER_ID, TODAY)
        self.assertEqual(["water"], state["done"])
        self.assertEqual("Вода", state["habits"][0]["title"])

    def test_events_are_persisted_idempotently_and_queued_for_make(self):
        habit = {"id": "water", "title": "Вода", "icon": "◌"}
        self.store.apply_event(USER_ID, "habit.created", habit, None, "Test")
        self.assertTrue(self.store.apply_event(USER_ID, "habit.completed", habit, TODAY))
        self.assertFalse(self.store.apply_event(USER_ID, "habit.completed", habit, TODAY))
        state = self.store.get_state(USER_ID, TODAY)
        self.assertEqual(["water"], state["done"])

        queued = self.store.next_outbox_events(int(server.time.time()))
        self.assertEqual(2, len(queued))
        completion = json.loads(queued[1]["payload"])
        self.assertEqual("Test", completion["user"]["first_name"])
        self.assertEqual(TODAY, completion["date"])
        self.assertIn("event_id", completion)

    def test_deleting_habit_preserves_completion_history(self):
        habit = {"id": "read", "title": "Читать", "icon": "✳"}
        self.store.apply_event(USER_ID, "habit.created", habit, None)
        self.store.apply_event(USER_ID, "habit.completed", habit, TODAY)
        self.store.apply_event(USER_ID, "habit.deleted", habit, None)
        state = self.store.get_state(USER_ID, TODAY)
        self.assertEqual([], state["habits"])
        self.assertEqual(["read"], state["done"])

    def test_invalid_date_and_unknown_habit_are_rejected(self):
        with self.assertRaises(ValueError):
            server.parse_date("2026-2-3")
        with self.assertRaises(LookupError):
            self.store.apply_event(
                USER_ID, "habit.completed",
                {"id": "missing", "title": "Нет", "icon": ""},
                TODAY,
            )

    def test_telegram_init_data_signature_and_age(self):
        token = "test-token"
        signed = signed_init_data(token)
        self.assertEqual(USER_ID, server.verify_telegram_init_data(signed, token)["id"])
        self.assertIsNone(server.verify_telegram_init_data(signed, "wrong-token"))
        expired = signed_init_data(token, auth_date=int(server.time.time()) - server.MAX_AUTH_AGE_SECONDS - 1)
        self.assertIsNone(server.verify_telegram_init_data(expired, token))
        self.assertIsNone(server.verify_telegram_init_data(f"{signed}&auth_date=1", token))


class McpHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_store = server.STORE
        server.STORE = server.HabitStore(Path(self.temp_dir.name) / "habits.sqlite3")
        self.environment = patch.dict(os.environ, {
            "MCP_AUTH_TOKEN": "test-secret",
            "MCP_ALLOWED_TELEGRAM_USER_IDS": str(USER_ID),
            "TELEGRAM_BOT_TOKEN": "test-bot-token",
        })
        self.environment.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.environment.stop()
        server.STORE = self.previous_store
        self.temp_dir.cleanup()

    def post_mcp(self, body, token="test-secret"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            f"{self.base_url}/mcp",
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        return urlopen(request)

    def test_telegram_api_bootstraps_and_persists_state(self):
        init_data = signed_init_data("test-bot-token")
        request = Request(
            f"{self.base_url}/api/habits?date={TODAY}",
            headers={"X-Telegram-Init-Data": init_data},
        )
        state = json.load(urlopen(request))
        self.assertFalse(state["initialized"])

        payload = {
            "habits": [{"id": "water", "title": "Вода", "icon": "◌", "time": "Утро"}],
            "history": {TODAY: ["water"]},
        }
        bootstrap = Request(
            f"{self.base_url}/api/bootstrap",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Init-Data": init_data,
            },
            method="POST",
        )
        self.assertTrue(json.load(urlopen(bootstrap))["imported"])

        event = Request(
            f"{self.base_url}/api/events",
            data=json.dumps({
                "event": "habit.created",
                "habit": {"id": "walk", "title": "Прогулка", "icon": "↗"},
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Init-Data": init_data,
            },
            method="POST",
        )
        self.assertTrue(json.load(urlopen(event))["ok"])
        refreshed = json.load(urlopen(request))
        self.assertEqual({"water", "walk"}, {habit["id"] for habit in refreshed["habits"]})
        self.assertEqual(["water"], refreshed["done"])

    def test_mcp_requires_bearer_auth_and_lists_tools(self):
        request = Request(
            f"{self.base_url}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(401, error.exception.code)
        error.exception.close()

        initialized = self.post_mcp({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        })
        self.assertEqual("2025-03-26", initialized.headers["MCP-Protocol-Version"])
        self.assertEqual("ritm-habits", json.load(initialized)["result"]["serverInfo"]["name"])

        listed = self.post_mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in json.load(listed)["result"]["tools"]}
        self.assertIn("create_habit", names)
        self.assertIn("complete_habit", names)

    def test_mcp_tools_manage_habits_for_allowlisted_user(self):
        created = self.post_mcp({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "create_habit", "arguments": {"title": "Вода"}},
        })
        created_result = json.load(created)["result"]["structuredContent"]
        habit_id = created_result["habit"]["id"]
        self.assertTrue(created_result["make_event_queued"])

        completed = self.post_mcp({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "complete_habit",
                "arguments": {"habit_id": habit_id, "date": TODAY},
            },
        })
        self.assertTrue(json.load(completed)["result"]["structuredContent"]["changed"])

        forbidden = self.post_mcp({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "list_habits",
                "arguments": {"user_id": "987654321"},
            },
        })
        result = json.load(forbidden)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("не разрешён", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
