from __future__ import annotations

import copy
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from notion_client import NotionAgentClient, NotionError, visible_messages
from server import make_server
from session_manager import SessionManager, parse_curl


SPACE = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
THREAD = "33333333-3333-4333-8333-333333333333"
MESSAGE_A = "44444444-4444-4444-8444-444444444444"
MESSAGE_B = "55555555-5555-4555-8555-555555555555"
CONFIG = {"space_id": SPACE, "user_id": USER, "cookie": "token_v2=fixture; notion_user_id=" + USER}
CURL = f"curl --url 'https://app.notion.com/api/v3/getInferenceTranscriptsForUser' \\\n  -b 'token_v2=fixture; notion_user_id={USER}' \\\n  -H 'x-notion-space-id: {SPACE}' \\\n  --data-raw '{{\"limit\":50}}'"


def envelope(record):
    return {"value": {"value": record, "role": "editor"}}


class FakeClient:
    failure = None

    def __init__(self, config):
        self.space_id, self.user_id = config["space_id"], config["user_id"]
        self.http = Mock(headers={"Cookie": config["cookie"]})

    def validate_session(self):
        if self.failure:
            raise self.failure
        return {"space_id": self.space_id, "user_id": self.user_id}


class ParserTests(unittest.TestCase):
    def test_bash_cookie_and_user_cookie_fallback(self):
        config = parse_curl(CURL)
        self.assertEqual(config["space_id"], SPACE)
        self.assertEqual(config["user_id"], USER)
        self.assertIn("token_v2=fixture", config["cookie"])

    def test_cookie_header_body_space_pointer(self):
        command = f"curl 'https://app.notion.com/api/v3/syncRecordValuesSpaceInitial' -H 'Cookie: token_v2=fixture' -H 'x-notion-active-user-header: {USER}' --data-raw '{{\"spacePointer\":{{\"table\":\"space\",\"id\":\"{SPACE}\"}}}}'"
        self.assertEqual(parse_curl(command)["space_id"], SPACE)

    def test_cmd_escaped_json_and_line_continuations(self):
        command = f'curl "https://app.notion.com/api/v3/getInferenceTranscriptsUnreadCount" ^\n -b "token_v2=fixture; notion_user_id={USER}" ^\n --data-raw "{{^"spaceId^":^"{SPACE}^"}}"'
        self.assertEqual(parse_curl(command)["space_id"], SPACE)

    def test_foreign_host_rejected(self):
        with self.assertRaisesRegex(NotionError, "app.notion.com"):
            parse_curl(CURL.replace("app.notion.com", "translate-pa.googleapis.com"))

    def test_missing_auth_rejected(self):
        with self.assertRaisesRegex(NotionError, "token_v2"):
            parse_curl(CURL.replace("token_v2=fixture", "other=fixture"))

    def test_multiple_commands_rejected(self):
        with self.assertRaises(NotionError):
            parse_curl(CURL + " && curl https://example.com/")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "session.json"
        FakeClient.failure = None
        self.addCleanup(setattr, FakeClient, "failure", None)

    def test_missing_session_can_be_connected(self):
        manager = SessionManager(self.path, client_factory=FakeClient)
        self.assertEqual(manager.check()["state"], "missing")
        self.assertEqual(manager.update_from_curl(CURL)["state"], "active")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["space_id"], SPACE)
        self.assertNotIn("curl", self.path.read_text(encoding="utf-8"))

    def test_failed_candidate_preserves_client_and_file(self):
        manager = SessionManager(self.path, client_factory=FakeClient)
        manager.update_from_curl(CURL)
        old_client, old_bytes = manager.client, self.path.read_bytes()
        FakeClient.failure = NotionError("Unauthorized", status=401)
        with self.assertRaises(NotionError):
            manager.update_from_curl(CURL.replace("token_v2=fixture", "token_v2=invalid"))
        self.assertIs(manager.client, old_client)
        self.assertEqual(self.path.read_bytes(), old_bytes)
        self.assertEqual(manager.status()["state"], "active")

    def test_startup_detects_expired_session_and_network_failure_separately(self):
        manager = SessionManager(self.path, client_factory=FakeClient)
        manager.update_from_curl(CURL)
        restarted = SessionManager(self.path, client_factory=FakeClient)
        FakeClient.failure = NotionError("Unauthorized", status=401)
        self.assertEqual(restarted.check()["state"], "expired")
        FakeClient.failure = NotionError("Timeout")
        self.assertEqual(restarted.check()["state"], "unavailable")
        self.assertTrue(self.path.exists())


class ClientTests(unittest.TestCase):
    def test_visible_output_does_not_include_thinking_or_hidden_injected_text(self):
        steps = [
            {"id": "1", "type": "user", "value": [["Hello", [["b"]]], [" world"]]},
            {"id": "2", "type": "user-injected", "actualMessage": [["hidden"]]},
            {"id": "3", "type": "agent-inference", "value": [{"type": "thinking", "content": "hidden reasoning"}, {"type": "text", "content": "Answer"}, {"type": "follow_ups", "content": "hidden followups"}]},
        ]
        self.assertEqual([message["text"] for message in visible_messages(steps)], ["Hello world", "Answer"])

    def test_load_history_uses_thread_order_not_record_map_order(self):
        client = NotionAgentClient(CONFIG)
        client._post = Mock(side_effect=[
            {"recordMap": {"thread": {THREAD: envelope({"id": THREAD, "messages": [MESSAGE_B, MESSAGE_A]})}}},
            {"recordMap": {"thread_message": {
                MESSAGE_A: envelope({"id": MESSAGE_A, "step": {"id": MESSAGE_A, "type": "user", "value": [["A"]]}}),
                MESSAGE_B: envelope({"id": MESSAGE_B, "step": {"id": MESSAGE_B, "type": "user", "value": [["B"]]}}),
            }}},
        ])
        self.assertEqual([message["text"] for message in client.load_thread(THREAD)["messages"]], ["B", "A"])
        payload = client._post.call_args_list[1].args[1]
        self.assertEqual(payload["requests"][0]["pointer"]["table"], "thread_message")

    def test_post_401_not_retried(self):
        http = Mock(headers={})
        response = Mock(status_code=401, cookies={}, json=Mock(return_value={"message": "Unauthorized"}))
        http.post.return_value = response
        client = NotionAgentClient(CONFIG, http=http)
        with self.assertRaises(NotionError) as error:
            client.list_threads()
        self.assertEqual(error.exception.status, 401)
        self.assertEqual(http.post.call_count, 1)

    def sending_client(self):
        client = NotionAgentClient(CONFIG)
        data = {"thread": {"id": THREAD, "messages": [], "data": {}, "created_source": "workflows"}, "steps": [{"type": "config", "value": {"type": "workflow"}}], "messages": []}
        client.load_thread = Mock(return_value=copy.deepcopy(data))
        return client

    def test_save_failure_never_runs_inference(self):
        client = self.sending_client()
        client._post = Mock(side_effect=NotionError("Save failed", status=401))
        with self.assertRaises(NotionError):
            list(client.send_message(THREAD, "Hello"))
        self.assertEqual(client._post.call_count, 1)
        self.assertEqual(client._post.call_args.args[0], "saveTransactionsFanout")

    def test_inference_failure_preserves_message_id_and_never_retries(self):
        client = self.sending_client()
        client._post = Mock(side_effect=[{}, NotionError("Unauthorized", status=401)])
        events = list(client.send_message(THREAD, "Hello"))
        self.assertEqual([event["type"] for event in events], ["submitted", "error"])
        self.assertEqual(events[0]["message_id"], events[1]["message_id"])
        self.assertEqual(events[1]["status"], 401)
        self.assertEqual(client._post.call_count, 2)
        saved = client._post.call_args_list[0].args[1]
        operations = saved["transactions"][0]["operations"]
        self.assertEqual(operations[0]["pointer"]["table"], "thread_message")
        self.assertEqual(operations[1]["command"], "listAfterMulti")
        self.assertEqual(operations[1]["args"]["ids"], [events[0]["message_id"]])

    def test_full_step_stream_is_replacement_not_repeated_text(self):
        client = self.sending_client()
        response = Mock()
        response.iter_lines.return_value = [
            json.dumps({"id": MESSAGE_A, "type": "agent-inference", "value": [{"type": "text", "content": "A"}]}),
            json.dumps({"id": MESSAGE_A, "type": "agent-inference", "value": [{"type": "text", "content": "AB"}]}),
        ]
        client._post = Mock(side_effect=[{}, response])
        events = list(client.send_message(THREAD, "Hello"))
        text_events = [event for event in events if event["type"] == "messages"]
        self.assertEqual(text_events[-1]["messages"][0]["text"], "AB")
        self.assertEqual(len(text_events[-1]["messages"]), 1)


class ServerTests(unittest.TestCase):
    def test_missing_session_ui_and_api_are_available(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = SessionManager(Path(folder) / "session.json", client_factory=FakeClient)
            server, _ = make_server(0, manager=manager)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                response = requests.get(base, timeout=3)
                self.assertEqual(response.status_code, 200)
                self.assertIn("getInferenceTranscriptsForUser", response.text)
                token = re.search("const TOKEN='([^']+)'", response.text).group(1)
                headers = {"X-Local-Token": token}
                self.assertEqual(requests.get(base + "/api/session", headers=headers, timeout=3).json()["state"], "missing")
                self.assertEqual(requests.get(base + "/api/threads", headers=headers, timeout=3).json()["code"], "session_expired")
                self.assertEqual(requests.post(base + "/api/session/refresh", headers=headers, json={"curl": CURL}, timeout=3).status_code, 200)
                self.assertEqual(requests.get(base + "/api/session", timeout=3).status_code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
