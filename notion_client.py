"""Notion's captured web API. Credentials are supplied only at runtime.

Read endpoints verified against live Notion. The send protocol is reconstructed
from the October 7, 2026 web bundle and is intentionally never auto-retried.
"""

from __future__ import annotations

import json
import copy
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator

import requests


DEFAULT_SESSION_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NotionAgentConnector" / "session.json"


class NotionError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


def failure_details(data: Any, status: int | None = None, headers: Any = None) -> dict:
    """Classify machine-readable refusals, not arbitrary error-message substrings."""
    tags = set()
    def scan(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("type", "code", "errorCode", "disabledReason") and isinstance(item, str):
                    tags.add(item)
                elif isinstance(item, (dict, list)):
                    scan(item)
        elif isinstance(value, list):
            for item in value:
                scan(item)
    scan(data)
    quota_codes = {"credit_limit_reached", "ai_credits_exhausted", "quota_exceeded", "usage_limit_reached"}
    category = "quota" if tags & quota_codes else "rate_limit" if status == 429 or "rate_limited" in tags else "authentication" if status == 401 else "error"
    retry_at = None
    retry_header = headers.get("Retry-After") if headers is not None else None
    if isinstance(retry_header, str):
        try:
            retry_at = time.time() + max(1, float(retry_header))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_header).timestamp()
            except (ValueError, TypeError, OverflowError):
                pass
    client_data = data.get("clientData", {}) if isinstance(data, dict) else {}
    if isinstance(client_data, dict):
        reset = client_data.get("resumesAtMs") or client_data.get("rateLimitBlockEndsAtMs")
        if isinstance(reset, (int, float)):
            retry_at = reset / 1000
    return {"category": category, "retry_at": retry_at, "safe_to_failover": category in ("rate_limit", "quota", "authentication")}


def unwrap_record(record: dict) -> dict:
    """The captured v3 recordMap has a value.value envelope; older maps differ."""
    current = record
    for _ in range(4):
        if not isinstance(current, dict):
            break
        if "id" in current:
            return current
        if "value" not in current:
            break
        current = current["value"]
    raise NotionError("Не удалось прочитать запись из recordMap.")


def rich_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(part[0] for part in value if isinstance(part, list) and part and isinstance(part[0], str))


def visible_messages(steps: list[dict]) -> list[dict]:
    """Expose visible text only, not thinking, signatures or execution context."""
    messages = []
    for step in steps:
        kind = step.get("type")
        text, role = "", "assistant"
        if kind == "user":
            text, role = rich_text(step.get("displayValue", step.get("value"))), "user"
        elif kind == "user-injected":
            # The frontend hides injected steps without displayMessage.
            if "displayMessage" not in step:
                continue
            text, role = rich_text(step["displayMessage"]), "user"
        elif kind == "agent-inference":
            value = step.get("value", [])
            text = value if isinstance(value, str) else "".join(
                item.get("content", "") for item in value
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("content"), str)
            )
        elif kind in ("markdown-chat", "fast-researcher-chat"):
            text = step.get("value", "")
        elif kind in ("error", "agent-debug-error"):
            text, role = step.get("message", "Ошибка агента"), "error"
        if isinstance(text, str) and text.strip():
            messages.append({"id": step.get("id"), "role": role, "text": text, "created_at": step.get("createdAt")})
    return messages


class NotionAgentClient:
    BASE_URL = "https://app.notion.com/api/v3"

    def __init__(self, config: dict, *, http: requests.Session | None = None):
        self.space_id = str(uuid.UUID(config["space_id"]))
        self.user_id = str(uuid.UUID(config["user_id"]))
        cookie = config.get("cookie", "").strip()
        if not cookie or "\n" in cookie or "\r" in cookie:
            raise NotionError("Нужна строка Cookie из авторизованного запроса Notion.")
        self.http = http or requests.Session()
        self.http.headers.update({
            "Cookie": cookie,
            "Content-Type": "application/json",
            "Accept": "application/json, application/x-ndjson",
            "Origin": "https://app.notion.com",
            "Referer": "https://app.notion.com/",
            "notion-audit-log-platform": "web",
            "notion-client-version": config.get("client_version", "23.13.20261007.0124"),
            "x-notion-active-user-header": self.user_id,
            "x-notion-space-id": self.space_id,
            "User-Agent": config.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"),
        })
        if config.get("cell_hint"):
            self.http.headers.update({"x-notion-cell-hint": config["cell_hint"], "x-notion-cell-hint-source": "space-id-mapping"})
        self.lock = threading.RLock()

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_SESSION_PATH) -> "NotionAgentClient":
        try:
            config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise NotionError(f"Не удалось загрузить конфигурацию сессии: {type(error).__name__}") from error
        if isinstance(config.get("sessions"), list):
            profiles = config["sessions"]
            chosen = next((profile for profile in profiles if profile["id"] == config.get("selected_id")), profiles[0] if profiles else None)
            if chosen is None:
                raise NotionError("В пуле нет сохранённых сессий.")
            config = chosen["config"]
        return cls(config)

    def _post(self, endpoint: str, payload: dict, *, stream: bool = False) -> Any:
        # No retries for POSTs, including transport failures and 401/429.
        try:
            response = self.http.post(
                f"{self.BASE_URL}/{endpoint}", json=payload,
                timeout=(15, 180 if stream else 35), stream=stream, allow_redirects=False,
            )
        except requests.RequestException as error:
            raise NotionError(f"Сетевая ошибка Notion: {type(error).__name__}. Запрос не повторён.") from error
        if response.cookies:
            cookies = dict(part.strip().split("=", 1) for part in self.http.headers["Cookie"].split(";") if "=" in part)
            cookies.update({cookie.name: cookie.value for cookie in response.cookies})
            self.http.headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
        if not 200 <= response.status_code < 300:
            status = response.status_code
            try:
                data = response.json()
            except ValueError:
                data = {}
            message = data.get("message", "") if isinstance(data, dict) else ""
            response.close()
            advice = " Обновите Cookie сессии." if status in (401, 403) else ""
            raise NotionError(f"Notion HTTP {status}.{advice} {str(message)[:400]}", status=status, details=failure_details(data, status, response.headers))
        if stream:
            response.encoding = "utf-8"
            return response
        try:
            data = response.json()
        except ValueError as error:
            raise NotionError("Notion вернул ответ, который не является JSON.") from error
        finally:
            response.close()
        if not isinstance(data, dict):
            raise NotionError("Неожиданный формат ответа Notion.")
        return data

    def list_threads(self, *, limit: int = 50, cursor: Any = None, parent: dict | None = None) -> dict:
        payload = {
            "threadParentPointer": parent or {"table": "space", "id": self.space_id, "spaceId": self.space_id},
            "limit": max(1, min(limit, 100)), "includeWriterChats": False,
        }
        if cursor is not None:
            payload["cursor"] = cursor
        with self.lock:
            return self._post("getInferenceTranscriptsForUser", payload)

    def unread_count(self) -> int:
        with self.lock:
            return self._post("getInferenceTranscriptsUnreadCount", {"spaceId": self.space_id, "threadParentId": self.space_id})["count"]

    def available_models(self) -> dict:
        with self.lock:
            return self._post("getAvailableModels", {"spaceId": self.space_id})

    def validate_session(self) -> dict:
        data = self.list_threads(limit=1)
        if not isinstance(data.get("threadIds"), list) or not isinstance(data.get("recordMap"), dict):
            raise NotionError("Notion не подтвердил доступ к списку чатов.")
        return {"space_id": self.space_id, "user_id": self.user_id, "thread_count": len(data["threadIds"]), "thread_ids": data["threadIds"], "has_more": data.get("hasMore")}

    def sync_records(self, table: str, ids: list[str]) -> dict:
        merged: dict = {}
        with self.lock:
            for offset in range(0, len(ids), 50):
                payload = {
                    "requests": [{"pointer": {"table": table, "id": str(uuid.UUID(record_id)), "spaceId": self.space_id}, "version": -1} for record_id in ids[offset:offset + 50]],
                    "spacePointer": {"table": "space", "id": self.space_id},
                }
                data = self._post("syncRecordValuesSpaceInitial", payload)
                merged.update(data.get("recordMap", {}).get(table, {}))
        return merged

    def load_thread(self, thread_id: str) -> dict:
        thread_id = str(uuid.UUID(thread_id))
        with self.lock:
            thread_records = self.sync_records("thread", [thread_id])
            if thread_id not in thread_records:
                raise NotionError("Тред не найден или недоступен этой сессии.")
            thread = unwrap_record(thread_records[thread_id])
            ids = thread.get("messages", [])
            records = self.sync_records("thread_message", ids)
            missing = [message_id for message_id in ids if message_id not in records]
            if missing:
                raise NotionError(f"Не удалось загрузить {len(missing)} шагов истории.")
            steps = [unwrap_record(records[message_id])["step"] for message_id in ids]
            return {"thread": thread, "steps": steps, "messages": visible_messages(steps)}

    @staticmethod
    def public_thread(data: dict) -> dict:
        thread = data["thread"]
        return {
            "id": thread["id"], "title": thread.get("data", {}).get("title", "Без названия"),
            "messages": data["messages"], "step_count": len(data["steps"]),
            "last_turn_outcome": thread.get("data", {}).get("last_turn_outcome"),
            "tool_events": [{"id": step.get("id"), "name": step.get("toolName", step.get("toolType", "tool")), "state": step.get("state"), "duration_ms": step.get("durationMs")} for step in data["steps"] if step.get("type") == "agent-tool-result"],
        }

    def _save_user_step(self, thread: dict, step: dict) -> None:
        now_ms = int(time.time() * 1000)
        pointer = {"table": "thread", "id": thread["id"], "spaceId": self.space_id}
        record = {
            "id": step["id"], "version": 0, "space_id": self.space_id, "step": step,
            "parent_id": thread["id"], "parent_table": "thread",
            "created_time": now_ms, "created_by_id": self.user_id, "created_by_table": "notion_user",
        }
        operations = [
            {"pointer": {"table": "thread_message", "id": step["id"], "spaceId": self.space_id}, "path": [], "command": "set", "args": record},
            {"pointer": pointer, "path": ["messages"], "command": "listAfterMulti", "args": {"ids": [step["id"]]}},
            {"pointer": pointer, "path": [], "command": "update", "args": {"latest_user_or_trigger_time": now_ms}},
        ]
        transactions = [
            {"id": str(uuid.uuid4()), "spaceId": self.space_id, "debug": {"userAction": "WorkflowActions.addStepsToExistingThreadAndRun", "clientCommitTimeMs": now_ms}, "operations": operations},
            {"id": str(uuid.uuid4()), "spaceId": self.space_id, "debug": {"userAction": "inferenceTranscriptActions.updateThreadUpdatedTime", "clientCommitTimeMs": now_ms}, "operations": [
                {"pointer": pointer, "path": [], "command": "update", "args": {"updated_time": now_ms, "updated_by_id": self.user_id, "updated_by_table": "notion_user"}}
            ]},
        ]
        self._post("saveTransactionsFanout", {"requestId": str(uuid.uuid4()), "transactions": transactions})

    def send_message(self, thread_id: str, text: str) -> Iterator[dict]:
        """Persist exactly one user message and yield visible streaming events.

        Calling this method writes to Notion. Never retry a failed turn blindly:
        use message_id / trace_id and reload history to see what was persisted.
        """
        if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
            raise NotionError("Сообщение должно содержать от 1 до 100 000 символов.")
        with self.lock:
            data = self.load_thread(thread_id)
            thread = data["thread"]
            if thread.get("current_inference_id") and thread.get("current_inference_lease_expiration", 0) > time.time() * 1000:
                raise NotionError("В этом чате ещё выполняется ответ агента. Обновите историю позже.")
            config = next((step["value"] for step in data["steps"] if step.get("type") == "config"), None)
            if not config or config.get("type") != "workflow":
                raise NotionError("Отправка поддерживается для чатов типа workflow.")
            step = {"id": str(uuid.uuid4()), "type": "user", "value": [[text]], "userId": self.user_id, "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
            trace_id = str(uuid.uuid4())
            self._save_user_step(thread, step)
            yield {"type": "submitted", "message_id": step["id"], "trace_id": trace_id}
            request = {
                "expectedFundingRoute": "ordinary", "submittedUserStepId": step["id"],
                "traceId": trace_id, "spaceId": self.space_id, "threadId": thread["id"],
                "transcript": [], "createThread": False, "generateTitle": False,
                "saveAllThreadOperations": True, "setUnreadState": True,
                "createdSource": thread.get("created_source", "workflows"), "threadType": "workflow",
                "isPartialTranscript": True, "asPatchResponse": False,
                "debugOverrides": {"cachedInferences": {}, "annotationInferences": {}, "emitInferences": False},
                "isUserInAnySalesAssistedSpace": False, "isSpaceSalesAssisted": False,
                "supportsCustomAgentNudgeTranscriptStep": True,
            }
            # Existing-thread requests load their saved transcript server-side.
            # asPatchResponse=False is supported by the captured web bundle and
            # returns full step updates; do not concatenate arbitrary patches.
            steps: list[dict] = []
            response = None
            handed_off = False
            try:
                response = self._post("runInferenceTranscript", request, stream=True)
                for line in response.iter_lines(chunk_size=1024, decode_unicode=True):
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line)
                    except ValueError as error:
                        raise NotionError("Поток Notion содержит некорректный NDJSON.") from error
                    kind = frame.get("type")
                    if kind == "error":
                        raise NotionError(str(frame.get("message", "Ошибка агента"))[:500], details=failure_details(frame))
                    if kind in ("patch", "patch-start", "patch-sync"):
                        raise NotionError("Сервер вернул патчи вопреки asPatchResponse=false. Сообщение сохранено; обновите историю.")
                    if kind in ("queue-handoff", "reenqueue-with-delay"):
                        handed_off = True
                        yield {"type": "status", "message": "Запуск передан в очередь. После завершения обновите историю."}
                    elif kind == "inference-connection-lifecycle":
                        handed_off = handed_off or frame.get("reason") == "container_shutdown"
                    elif kind == "record-map":
                        # Server persists these records; the read API is the source of truth.
                        continue
                    elif kind == "agent-tool-result":
                        yield {"type": "tool", "id": frame.get("id"), "name": frame.get("toolName", frame.get("toolType", "tool")), "state": frame.get("state")}
                    if frame.get("id") and kind not in ("record-map", "inference", "agent-search-extracted-results"):
                        index = next((index for index, old in enumerate(steps) if old.get("id") == frame["id"]), None)
                        if index is None:
                            steps.append(frame)
                        else:
                            steps[index] = frame
                        if kind == "agent-inference":
                            yield {"type": "messages", "messages": visible_messages(steps)}
            except (NotionError, requests.RequestException) as error:
                yield {"type": "error", "message": str(error) if isinstance(error, NotionError) else "Соединение прервано. Обновите историю перед повторной отправкой.", "status": error.status if isinstance(error, NotionError) else None, "details": error.details if isinstance(error, NotionError) else {}, "message_id": step["id"], "trace_id": trace_id}
                return
            finally:
                if response is not None:
                    response.close()
            try:
                latest = self.load_thread(thread_id)
                outcome = latest["thread"].get("data", {}).get("last_turn_outcome", {})
                completed = outcome.get("inference_id") == trace_id and outcome.get("status") == "completed"
                yield {"type": "done", "thread": self.public_thread(latest), "trace_id": trace_id, "completed": completed, "pending": handed_off or not completed}
            except NotionError as error:
                yield {"type": "error", "message": f"Поток завершён, но история пока не загрузилась: {error}", "message_id": step["id"], "trace_id": trace_id}

    def complete_messages(self, template_thread_id: str, messages: list[dict]) -> Iterator[dict]:
        """Create an isolated Notion thread using the configured agent's settings.

        Each completion gets a fresh thread, so unrelated API callers don't share
        conversation memory. Text history is carried by the caller's messages.
        """
        with self.lock:
            template = self.load_thread(template_thread_id)
            config = None
            context = None
            for step in template["steps"]:
                if step.get("type") == "config":
                    config = copy.deepcopy(step["value"])
                elif step.get("type") == "updated-config" and config is not None:
                    config.update(copy.deepcopy(step.get("value", {})))
                elif step.get("type") == "context":
                    context = copy.deepcopy(step.get("value", {}))
            if not config or config.get("type") != "workflow":
                raise NotionError("Выберите чат агента с типом workflow для этого профиля.")
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            context = context or {"timezone": "Europe/Moscow", "surface": "workflows"}
            context.update({"userId": self.user_id, "spaceId": self.space_id, "currentDatetime": now})
            rendered = "Внешний разговор через API. Ответь на последнее сообщение, учитывая переданную историю.\n\n" + json.dumps(messages, ensure_ascii=False)
            user_step = {"id": str(uuid.uuid4()), "type": "user", "value": [[rendered]], "userId": self.user_id, "createdAt": now}
            thread_id, trace_id = str(uuid.uuid4()), str(uuid.uuid4())
            request = {
                "expectedFundingRoute": "ordinary", "submittedUserStepId": user_step["id"],
                "traceId": trace_id, "spaceId": self.space_id, "threadId": thread_id,
                "threadParentPointer": {"table": "space", "id": self.space_id, "spaceId": self.space_id},
                "transcript": [{"id": str(uuid.uuid4()), "type": "config", "value": config}, {"id": str(uuid.uuid4()), "type": "context", "value": context}, user_step],
                "createThread": True, "generateTitle": True, "saveAllThreadOperations": True,
                "setUnreadState": True, "isPartialTranscript": False, "asPatchResponse": False,
                "threadType": "workflow", "createdSource": template["thread"].get("created_source", "workflows"),
                "debugOverrides": {"cachedInferences": {}, "annotationInferences": {}, "emitInferences": False},
                "isUserInAnySalesAssistedSpace": False, "isSpaceSalesAssisted": False,
                "supportsCustomAgentNudgeTranscriptStep": True,
            }
            workflow_id = template["thread"].get("workflow_id") or template["thread"].get("data", {}).get("workflow_id") or config.get("workflowId")
            if workflow_id:
                request["threadWorkflowId"] = workflow_id
            if template["thread"].get("parent_table") and template["thread"].get("parent_id"):
                request["threadParentPointer"] = {"table": template["thread"]["parent_table"], "id": template["thread"]["parent_id"], "spaceId": self.space_id}
            yield {"type": "started", "thread_id": thread_id, "trace_id": trace_id}
            response = self._post("runInferenceTranscript", request, stream=True)
            steps = []
            try:
                for line in response.iter_lines(chunk_size=1024, decode_unicode=True):
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line)
                    except ValueError as error:
                        raise NotionError("Некорректный NDJSON. Исход запроса неизвестен.") from error
                    kind = frame.get("type")
                    if kind == "error":
                        raise NotionError(str(frame.get("message", "Ошибка агента"))[:500], details=failure_details(frame))
                    if kind in ("patch", "patch-start", "patch-sync"):
                        raise NotionError("Notion вернул неподдерживаемые патчи. Исход запроса нужно проверить по истории.")
                    if kind == "agent-tool-result":
                        yield {"type": "tool", "name": frame.get("toolName", frame.get("toolType", "tool")), "state": frame.get("state")}
                    if kind in ("agent-inference", "markdown-chat", "fast-researcher-chat") and frame.get("id"):
                        index = next((i for i, previous in enumerate(steps) if previous.get("id") == frame["id"]), None)
                        if index is None:
                            steps.append(frame)
                        else:
                            steps[index] = frame
                        yield {"type": "text", "text": "\n\n".join(item["text"] for item in visible_messages(steps))}
            except requests.RequestException as error:
                raise NotionError("Поток прерван. Исход запроса неизвестен; автоматический повтор остановлен.") from error
            finally:
                response.close()
            latest = self.load_thread(thread_id)
            outcome = latest["thread"].get("data", {}).get("last_turn_outcome", {})
            if outcome.get("inference_id") != trace_id or outcome.get("status") != "completed":
                raise NotionError("Запуск ещё не подтверждён как завершённый. Проверьте историю треда.", details={"thread_id": thread_id, "trace_id": trace_id})
            answer = "\n\n".join(item["text"] for item in latest["messages"] if item["role"] == "assistant")
            yield {"type": "done", "text": answer, "thread": self.public_thread(latest), "thread_id": thread_id, "trace_id": trace_id, "completed": True}
