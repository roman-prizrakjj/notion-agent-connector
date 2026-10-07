from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from connector_api import ConnectorAPI, normalize_messages
from notion_client import NotionError, failure_details
from server import make_server
from session_pool import SessionPool


THREAD = "33333333-3333-4333-8333-333333333333"


class ProfileClient:
    def __init__(self, config):
        self.space_id, self.user_id = config["space_id"], config["user_id"]
        self.http = Mock(headers={"Cookie": config["cookie"]})
        self.mode, self.calls = "ok", []

    def validate_session(self):
        return {"thread_ids": [THREAD], "has_more": False}

    def complete_messages(self, template_id, messages):
        self.calls.append((template_id, messages))
        yield {"type": "started", "thread_id": THREAD, "trace_id": "trace"}
        if self.mode == "quota":
            raise NotionError("Quota", status=429, details=failure_details({"clientData": {"type": "credit_limit_reached"}}, 429))
        if self.mode == "rate_limit":
            raise NotionError("Rate limit", status=429, details=failure_details({"clientData": {"type": "rate_limited"}}, 429))
        if self.mode == "timeout":
            raise NotionError("Network outcome unknown")
        if self.mode == "tool_quota":
            yield {"type": "tool", "name": "create-page", "state": "applied"}
            raise NotionError("Quota", status=429, details=failure_details({}, 429))
        yield {"type": "text", "text": "Hello"}
        if self.mode == "partial_quota":
            raise NotionError("Quota", status=429, details=failure_details({}, 429))
        yield {"type": "text", "text": "Hello world"}
        yield {"type": "done", "text": "Hello world", "thread_id": THREAD, "trace_id": "trace", "thread": {"id": THREAD, "title": "Fixture", "messages": [], "step_count": 1}, "completed": True}

    def list_threads(self, **_kwargs):
        return {"transcripts": [{"id": THREAD, "title": "Fixture"}], "threadIds": [THREAD], "hasMore": False, "recordMap": {}}

    def load_thread(self, thread_id):
        return {"thread": {"id": thread_id}, "steps": [{"type": "config", "value": {"type": "workflow"}}], "messages": []}

    def public_thread(self, data):
        return {"id": data["thread"]["id"], "messages": [], "step_count": 1}


def curl_for(number):
    user = f"{number:08d}-2222-4222-8222-222222222222"
    space = f"{number:08d}-1111-4111-8111-111111111111"
    return f"curl https://app.notion.com/api/v3/getInferenceTranscriptsForUser -b 'token_v2=fixture-{number}; notion_user_id={user}' -H 'x-notion-space-id: {space}'"


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "session.json"
        self.pool = SessionPool(self.path, client_factory=ProfileClient)

    def add(self, number, priority):
        state = self.pool.update_from_curl(curl_for(number), name="Agent " + str(number), priority=priority, add=True)
        return state["selected_id"]

    def test_ranked_profiles_survive_restart(self):
        second, first = self.add(2, 2), self.add(1, 1)
        restarted = SessionPool(self.path, client_factory=ProfileClient)
        self.assertEqual([profile["id"] for profile in restarted.public_profiles()], [first, second])
        self.assertEqual(restarted.api_key, self.pool.api_key)
        self.assertEqual(restarted.status()["state"], "unchecked")
        self.assertNotIn("token_v2", json.dumps(restarted.status()))
        self.assertNotIn(restarted.api_key, json.dumps(restarted.status()))

    def test_explicit_quota_switches_before_any_output(self):
        first, second = self.add(1, 1), self.add(2, 2)
        self.pool.profiles[first]["client"].mode = "quota"
        messages = [{"role": "user", "content": "Question"}]
        events = list(self.pool.ask(messages))
        self.assertEqual([event["session_id"] for event in events if event["type"] == "session"], [first, second])
        self.assertEqual(events[-1]["session_id"], second)
        self.assertEqual(self.pool.profiles[first]["state"], "limited")
        self.assertEqual(self.pool.profiles[second]["client"].calls[0][1], messages)

    def test_same_identity_not_treated_as_independent_budget(self):
        first = self.add(1, 1)
        same_identity = self.add(1, 2)
        other = self.add(2, 3)
        self.pool.profiles[first]["client"].mode = "quota"
        events = list(self.pool.ask([{"role": "user", "content": "Question"}]))
        self.assertEqual(events[-1]["session_id"], other)
        self.assertEqual(self.pool.profiles[same_identity]["client"].calls, [])

    def test_partial_answer_never_replayed_on_backup(self):
        first, second = self.add(1, 1), self.add(2, 2)
        self.pool.profiles[first]["client"].mode = "partial_quota"
        with self.assertRaises(NotionError) as caught:
            list(self.pool.ask([{"role": "user", "content": "Question"}]))
        self.assertTrue(caught.exception.details["partial_answer"])
        self.assertEqual(self.pool.profiles[second]["client"].calls, [])

    def test_generic_throttle_can_try_another_agent_same_identity(self):
        first, second = self.add(1, 1), self.add(1, 2)
        self.pool.profiles[first]["client"].mode = "rate_limit"
        events = list(self.pool.ask([{"role": "user", "content": "Question"}]))
        self.assertEqual(events[-1]["session_id"], second)

    def test_tool_side_effect_blocks_replay(self):
        first, second = self.add(1, 1), self.add(2, 2)
        self.pool.profiles[first]["client"].mode = "tool_quota"
        with self.assertRaises(NotionError) as caught:
            list(self.pool.ask([{"role": "user", "content": "Question"}]))
        self.assertTrue(caught.exception.details["tools_started"])
        self.assertEqual(self.pool.profiles[second]["client"].calls, [])

    def test_ambiguous_network_failure_never_replayed(self):
        first, second = self.add(1, 1), self.add(2, 2)
        self.pool.profiles[first]["client"].mode = "timeout"
        with self.assertRaises(NotionError) as caught:
            list(self.pool.ask([{"role": "user", "content": "Question"}]))
        self.assertTrue(caught.exception.details["uncertain_outcome"])
        self.assertEqual(self.pool.profiles[second]["client"].calls, [])

    def test_fixed_session_disables_fallback(self):
        first, second = self.add(1, 1), self.add(2, 2)
        self.pool.profiles[first]["client"].mode = "quota"
        with self.assertRaises(NotionError):
            list(self.pool.ask([{"role": "user", "content": "Question"}], session_id=first))
        self.assertEqual(self.pool.profiles[second]["client"].calls, [])

    def test_all_exhausted_attempted_once_each(self):
        ids = [self.add(number, number) for number in range(1, 4)]
        for profile in self.pool.profiles.values():
            profile["client"].mode = "quota"
        with self.assertRaises(NotionError) as caught:
            list(self.pool.ask([{"role": "user", "content": "Question"}]))
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.details["attempted_sessions"], ids)
        self.assertTrue(all(len(profile["client"].calls) == 1 for profile in self.pool.profiles.values()))

    def test_explicit_retry_after_and_credits_check(self):
        identifier = self.add(1, 1)
        self.pool.clock = lambda: 100
        self.pool.note_failure(NotionError("Limit", status=429, details={"category": "rate_limit", "retry_at": 200}), identifier)
        self.assertEqual(self.pool.candidates(), [])
        self.pool.check(identifier)
        self.assertEqual(self.pool.profiles[identifier]["state"], "cooldown")
        self.pool.clock = lambda: 201
        self.assertEqual(self.pool.candidates()[0]["id"], identifier)

    def test_disabled_profiles_skipped(self):
        first, second = self.add(1, 1), self.add(2, 2)
        self.pool.configure(first, enabled=False)
        self.assertEqual(list(self.pool.ask([{"role": "user", "content": "Question"}]))[-1]["session_id"], second)

    def test_cookie_refresh_does_not_reset_known_quota(self):
        identifier = self.add(1, 1)
        self.pool.note_failure(NotionError("Quota", status=429), identifier)
        state = self.pool.update_from_curl(curl_for(1), session_id=identifier)
        self.assertEqual(state["state"], "limited")

    def test_legacy_session_migration_keeps_credentials(self):
        legacy = {"space_id": "11111111-1111-4111-8111-111111111111", "user_id": "22222222-2222-4222-8222-222222222222", "cookie": "token_v2=legacy"}
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        migrated = SessionPool(self.path, client_factory=ProfileClient)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["sessions"][0]["config"]["cookie"], legacy["cookie"])
        self.assertEqual(len(migrated.profiles), 1)


class APITests(unittest.TestCase):
    add = PoolTests.add

    def setUp(self):
        PoolTests.setUp(self)
        self.add(1, 1)
        self.api = ConnectorAPI(self.pool)

    def test_stream_delta_is_not_repeated_snapshot(self):
        chunks = list(self.api.chunks({"model": "notion-auto", "messages": [{"role": "user", "content": "Question"}]}))
        self.assertEqual("".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks), "Hello world")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

    def test_mcp_initialize_and_tools(self):
        initialized = self.api.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
        result = self.api.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(result["result"]["tools"]), 5)
        self.assertIsNone(self.api.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertTrue(self.api.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ask_notion_agent", "arguments": {"prompt": "Question"}}})["result"]["structuredContent"]["text"])

    def test_unsupported_local_tools_rejected_before_notions_call(self):
        with self.assertRaises(NotionError):
            normalize_messages({"messages": [{"role": "user", "content": "Question"}], "tools": [{"type": "function"}]})
        self.assertTrue(all(not profile["client"].calls for profile in self.pool.profiles.values()))

    def test_http_json_sse_mcp_and_stdio(self):
        server, _ = make_server(0, manager=self.pool)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        headers = {"Authorization": "Bearer " + self.pool.api_key}
        try:
            self.assertEqual(requests.get(base + "/v1/models", timeout=3).status_code, 401)
            models = requests.get(base + "/v1/models", headers=headers, timeout=3).json()
            self.assertEqual(models["data"][0]["id"], "notion-auto")
            body = {"model": "notion-auto", "messages": [{"role": "user", "content": "Question"}]}
            completion = requests.post(base + "/v1/chat/completions", headers=headers, json=body, timeout=3).json()
            self.assertEqual(completion["choices"][0]["message"]["content"], "Hello world")
            # If the official SDK is installed, verify its actual parsing too.
            try:
                from openai import OpenAI
            except ImportError:
                OpenAI = None
            if OpenAI:
                with OpenAI(api_key=self.pool.api_key, base_url=base + "/v1", max_retries=0) as sdk:
                    self.assertEqual(sdk.models.list().data[0].id, "notion-auto")
                    response = sdk.chat.completions.create(model="notion-auto", messages=[{"role": "user", "content": "Question"}])
                    self.assertEqual(response.choices[0].message.content, "Hello world")
                    chunks = list(sdk.chat.completions.create(model="notion-auto", messages=[{"role": "user", "content": "Question"}], stream=True))
                    self.assertEqual("".join(chunk.choices[0].delta.content or "" for chunk in chunks), "Hello world")
            response = requests.post(base + "/v1/chat/completions", headers=headers, json={**body, "stream": True}, timeout=3)
            self.assertEqual(response.headers["Content-Type"], "text/event-stream; charset=utf-8")
            self.assertIn("data: [DONE]", response.text)
            self.assertEqual(requests.post(base + "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=3).status_code, 202)
            self.assertEqual(requests.get(base + "/mcp", headers=headers, timeout=3).status_code, 405)
            self.assertEqual(requests.get(base + "/openapi.json", timeout=3).json()["openapi"], "3.1.0")
            messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
            result = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve().parents[1] / "mcp_stdio.py"), "--url", base + "/mcp", "--session", str(self.path)], input="\n".join(json.dumps(item) for item in messages) + "\n", capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([reply["id"] for reply in replies], [1, 2])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
