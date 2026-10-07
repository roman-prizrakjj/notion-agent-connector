"""Ranked, persistent Notion sessions with failover on explicit refusals."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from notion_client import DEFAULT_SESSION_PATH, NotionAgentClient, NotionError, failure_details
from session_manager import parse_curl


class SessionPool:
    def __init__(self, path=DEFAULT_SESSION_PATH, *, client_factory=NotionAgentClient, clock=time.time):
        self.path, self.factory, self.clock = Path(path), client_factory, clock
        self.lock = threading.RLock()
        self.profiles = {}
        self.selected_id = None
        self.api_key = secrets.token_urlsafe(36)
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            saved = {}
        self.api_key = saved.get("api_key") or self.api_key
        self.selected_id = saved.get("selected_id")
        entries = saved.get("sessions", [])
        if saved.get("cookie"):
            entries = [{"id": str(uuid.uuid4()), "name": "Основная сессия", "priority": 1, "enabled": True, "config": saved}]
        for entry in entries:
            try:
                profile = dict(entry)
                profile["client"] = self.factory(profile["config"])
                profile.setdefault("name", "Notion")
                profile.setdefault("priority", 1)
                profile.setdefault("enabled", True)
                profile.setdefault("thread_id", None)
                profile.setdefault("cooldown_until", None)
                profile.setdefault("state", "unchecked")
                profile.setdefault("checked_at", None)
                profile.setdefault("message", "Сессия ещё не проверена.")
                # Active sessions must be revalidated on each startup.
                if profile["state"] == "active":
                    profile["state"] = "unchecked"
                self.profiles[profile["id"]] = profile
            except (KeyError, ValueError, TypeError, NotionError):
                continue
        if self.selected_id not in self.profiles:
            self.selected_id = next(iter(self.profiles), None)
        self._persist()

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sessions = []
        for profile in self.profiles.values():
            saved = {key: value for key, value in profile.items() if key != "client"}
            saved["config"] = dict(profile["config"])
            saved["config"]["cookie"] = profile["client"].http.headers["Cookie"]
            sessions.append(saved)
        value = {"version": 2, "api_key": self.api_key, "selected_id": self.selected_id, "sessions": sessions}
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".pool-", suffix=".tmp", delete=False) as temporary:
                temporary_name = temporary.name
                json.dump(value, temporary, ensure_ascii=False, indent=2)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if temporary_name and Path(temporary_name).exists():
                Path(temporary_name).unlink()

    def _profile(self, session_id=None):
        session_id = session_id or self.selected_id
        if session_id not in self.profiles:
            raise NotionError("Добавьте сессию Notion через cURL.", status=401)
        return self.profiles[session_id]

    def _wake(self, profile):
        until = profile.get("cooldown_until")
        if profile["state"] == "cooldown" and until is not None and until <= self.clock():
            profile["state"], profile["cooldown_until"] = "unchecked", None
            profile["message"] = "Пауза завершена; сессия будет проверена перед запросом."

    def public_profiles(self):
        with self.lock:
            result = []
            for profile in sorted(self.profiles.values(), key=lambda item: (item["priority"], item["id"])):
                self._wake(profile)
                result.append({key: profile.get(key) for key in ("id", "name", "priority", "enabled", "thread_id", "state", "message", "checked_at", "cooldown_until", "success_count", "failure_count")})
            return result

    def status(self):
        with self.lock:
            profile = self.profiles.get(self.selected_id)
            state = profile["state"] if profile else "missing"
            return {"state": state, "message": profile["message"] if profile else "Добавьте сессию Notion через cURL.", "checked_at": profile.get("checked_at") if profile else None, "space_id": profile["client"].space_id if profile else None, "user_id": profile["client"].user_id if profile else None, "needs_session": state in ("missing", "expired"), "selected_id": self.selected_id, "sessions": self.public_profiles()}

    def require_client(self, session_id=None):
        profile = self._profile(session_id)
        if profile["state"] == "expired":
            raise NotionError(profile["message"], status=401)
        return profile["client"]

    def _validated(self, profile):
        metadata = profile["client"].validate_session()
        if not profile.get("thread_id") and metadata.get("has_more") is False and len(metadata.get("thread_ids", [])) == 1:
            profile["thread_id"] = metadata["thread_ids"][0]
        profile["state"], profile["message"] = "active", "Сессия проверена."
        profile["cooldown_until"] = None
        profile["checked_at"] = datetime.now(timezone.utc).isoformat()
        return metadata

    def check(self, session_id=None, *, all_profiles=False):
        with self.lock:
            profiles = list(self.profiles.values()) if all_profiles else [self._profile(session_id)] if self.profiles else []
            for profile in profiles:
                previous, until = profile["state"], profile.get("cooldown_until")
                try:
                    self._validated(profile)
                    # Authentication checks cannot prove that credits were replenished.
                    if previous in ("limited", "cooldown") and (until is None or until > self.clock()):
                        profile["state"], profile["cooldown_until"] = previous, until
                        profile["message"] = "Вход работает; лимит сохраняется. Используйте «Повторить после лимита», когда он восстановится."
                except NotionError as error:
                    profile["state"], profile["message"] = "unavailable", str(error)
                    self.note_failure(error, profile["id"], persist=False)
                profile["checked_at"] = datetime.now(timezone.utc).isoformat()
            self._persist()
            return self.status()

    def update_from_curl(self, text, *, name=None, priority=None, session_id=None, add=False):
        config = parse_curl(text)
        with self.lock:
            if session_id and not add and session_id not in self.profiles:
                raise NotionError("Профиль не найден.", status=400)
            candidate = self.factory(config)
            try:
                metadata = candidate.validate_session()
            except Exception:
                candidate.http.close()
                raise
            existing = self.profiles.get(session_id or self.selected_id) if not add else None
            profile_id = existing["id"] if existing else str(uuid.uuid4())
            if priority is not None:
                priority = int(priority)
                if not 1 <= priority <= 1000:
                    candidate.http.close()
                    raise NotionError("Приоритет должен быть от 1 до 1000.")
            profile = {
                "id": profile_id, "name": str(name or (existing["name"] if existing else "Сессия " + str(len(self.profiles) + 1)))[:100],
                "priority": priority if priority is not None else existing["priority"] if existing else len(self.profiles) + 1,
                "enabled": existing["enabled"] if existing else True, "config": config, "client": candidate,
                "thread_id": existing.get("thread_id") if existing and existing["config"]["space_id"] == config["space_id"] and existing["config"]["user_id"] == config["user_id"] else None,
                "state": "active", "message": "Сессия обновлена и проверена.", "checked_at": datetime.now(timezone.utc).isoformat(), "cooldown_until": None,
            }
            same_identity = existing and existing["config"]["space_id"] == config["space_id"] and existing["config"]["user_id"] == config["user_id"]
            if same_identity and existing["state"] in ("limited", "cooldown") and (existing.get("cooldown_until") is None or existing["cooldown_until"] > self.clock()):
                profile["state"], profile["cooldown_until"] = existing["state"], existing.get("cooldown_until")
                profile["message"] = "Сессия обновлена; подтверждённый лимит этого аккаунта сохраняется."
            if not profile["thread_id"] and metadata.get("has_more") is False and len(metadata.get("thread_ids", [])) == 1:
                profile["thread_id"] = metadata["thread_ids"][0]
            previous_selected = self.selected_id
            self.profiles[profile_id], self.selected_id = profile, profile_id
            try:
                self._persist()
            except Exception:
                if existing:
                    self.profiles[profile_id] = existing
                else:
                    del self.profiles[profile_id]
                self.selected_id = previous_selected
                candidate.http.close()
                raise
            if existing:
                existing["client"].http.close()
            return self.status()

    def configure(self, session_id, **changes):
        with self.lock:
            profile = self._profile(session_id)
            updates = {}
            if "name" in changes:
                updates["name"] = str(changes["name"]).strip()[:100] or profile["name"]
            if "priority" in changes:
                value = int(changes["priority"])
                if not 1 <= value <= 1000:
                    raise NotionError("Приоритет должен быть от 1 до 1000.")
                updates["priority"] = value
            if "enabled" in changes:
                if not isinstance(changes["enabled"], bool):
                    raise NotionError("enabled должен быть boolean.")
                updates["enabled"] = changes["enabled"]
            if "thread_id" in changes:
                thread_id = str(uuid.UUID(changes["thread_id"]))
                data = profile["client"].load_thread(thread_id)
                if not any(step.get("type") == "config" and step.get("value", {}).get("type") == "workflow" for step in data["steps"]):
                    raise NotionError("Выберите чат агента типа workflow.")
                updates["thread_id"] = thread_id
            profile.update(updates)
            if changes.get("retry_after_limit") is True:
                profile["state"], profile["cooldown_until"] = "unchecked", None
                profile["message"] = "Лимит будет проверен следующей попыткой запроса."
            self._persist()
            return self.status()

    def select(self, session_id):
        with self.lock:
            self._profile(session_id)
            self.selected_id = session_id
            self._persist()
            return self.status()

    def note_failure(self, error, session_id=None, *, persist=True):
        with self.lock:
            profile = self.profiles.get(session_id or self.selected_id)
            if not profile:
                return
            details = error.details or failure_details({}, error.status)
            category = details.get("category")
            profile["failure_count"] = profile.get("failure_count", 0) + 1
            if error.status in (401, 403):
                profile["state"], profile["message"] = "expired", "Обновите сессию через свежий cURL."
            elif category in ("rate_limit", "quota"):
                reset = details.get("retry_at")
                # A time-based pause is used only when supplied by the server.
                profile["state"] = "cooldown" if reset is not None else "limited"
                profile["cooldown_until"] = reset
                profile["message"] = "Notion сообщил об ограничении: " + str(error)
                # Multiple cookies for the same account/workspace do not imply
                # independent budgets. Don't immediately retry that identity.
                for peer in self.profiles.values():
                    if category == "quota" and peer is not profile and peer["config"]["user_id"] == profile["config"]["user_id"] and peer["config"]["space_id"] == profile["config"]["space_id"]:
                        peer["state"], peer["cooldown_until"] = profile["state"], reset
                        peer["message"] = "Ограничение другой сессии того же аккаунта и workspace."
            else:
                profile["message"] = str(error)
            if persist:
                self._persist()

    def candidates(self, *, session_id=None, preferred_id=None):
        with self.lock:
            ordered = sorted(self.profiles.values(), key=lambda profile: (profile["priority"], profile["id"]))
            if session_id:
                ordered = [self._profile(session_id)]
            elif preferred_id:
                ordered.sort(key=lambda profile: (profile["id"] != preferred_id, profile["priority"], profile["id"]))
            for profile in ordered:
                self._wake(profile)
            return [profile for profile in ordered if profile["enabled"] and profile.get("thread_id") and profile["state"] not in ("expired", "limited", "cooldown")]

    def ask(self, messages, *, session_id=None, preferred_id=None):
        """Retry only explicit refusals before any answer/tool result was emitted."""
        with self.lock:
            candidates = self.candidates(session_id=session_id, preferred_id=preferred_id)
            attempted = []
            for profile in candidates:
                if profile["state"] in ("limited", "cooldown", "expired"):
                    continue
                attempted.append(profile["id"])
                visible, tool_seen, started = False, False, False
                try:
                    if profile["state"] != "active":
                        self._validated(profile)
                    yield {"type": "session", "session_id": profile["id"], "name": profile["name"], "attempt": len(attempted)}
                    for event in profile["client"].complete_messages(profile["thread_id"], messages):
                        if event["type"] == "started":
                            started = True
                        if event["type"] == "text" and event.get("text"):
                            visible = True
                        if event["type"] == "tool":
                            tool_seen = True
                        event = {**event, "session_id": profile["id"]}
                        if event["type"] == "done":
                            profile["success_count"] = profile.get("success_count", 0) + 1
                            self._persist()
                        yield event
                    return
                except NotionError as error:
                    self.note_failure(error, profile["id"])
                    details = error.details or failure_details({}, error.status)
                    safe = details.get("safe_to_failover") and not visible and not tool_seen
                    if session_id or not safe:
                        error.details.update({"session_id": profile["id"], "attempted_sessions": attempted, "partial_answer": visible, "tools_started": tool_seen, "uncertain_outcome": started and not details.get("safe_to_failover")})
                        raise
                    yield {"type": "failover", "session_id": profile["id"], "name": profile["name"], "reason": details.get("category"), "retry_at": details.get("retry_at")}
            raise NotionError("Нет доступных сессий: добавьте профиль, выберите его агентный чат или дождитесь восстановления лимита.", status=429, details={"attempted_sessions": attempted, "category": "pool_unavailable"})

    def send_message(self, thread_id, text, *, session_id=None, allow_failover=True):
        """Continue the selected thread, or carry its visible context to a backup."""
        with self.lock:
            profile = self._profile(session_id)
            client = self.require_client(profile["id"])
            history = client.load_thread(thread_id)
            context = [{"role": item["role"], "content": item["text"]} for item in history["messages"] if item["role"] in ("user", "assistant")]
            context.append({"role": "user", "content": text})
            visible, tool_seen = False, False
            refusal = None
            if profile["state"] not in ("limited", "cooldown"):
                try:
                    for event in client.send_message(thread_id, text):
                        if event["type"] == "messages" and any(item.get("text") for item in event.get("messages", []) if item.get("role") == "assistant"):
                            visible = True
                        if event["type"] == "tool":
                            tool_seen = True
                        if event["type"] == "error":
                            refusal = NotionError(event["message"], status=event.get("status"), details=event.get("details"))
                            break
                        yield {**event, "session_id": profile["id"]}
                    if refusal is None:
                        return
                except NotionError as error:
                    refusal = error
                self.note_failure(refusal, profile["id"])
                details = refusal.details or failure_details({}, refusal.status)
                if not allow_failover or not details.get("safe_to_failover") or visible or tool_seen:
                    yield {"type": "error", "message": str(refusal), "status": refusal.status, "session_id": profile["id"], "session": self.status(), "code": "session_expired" if refusal.status in (401, 403) else "request_failed"}
                    return
            elif not allow_failover:
                yield {"type": "error", "message": profile["message"], "status": 429, "session": self.status()}
                return
            try:
                for event in self.ask(context):
                    if event["type"] == "session":
                        yield {"type": "session_switched", "session_id": event["session_id"], "name": event["name"]}
                    elif event["type"] == "text":
                        yield {"type": "messages", "messages": [{"id": "backup-answer", "role": "assistant", "text": event["text"]}], "session_id": event["session_id"]}
                    elif event["type"] == "done":
                        self.selected_id = event["session_id"]
                        self._persist()
                        yield {**event, "pending": False, "session": self.status()}
                    elif event["type"] == "tool":
                        yield event
            except NotionError as error:
                yield {"type": "error", "message": str(error), "status": error.status, "details": error.details, "session": self.status()}
