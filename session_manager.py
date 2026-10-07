"""Parse cURL as data, verify a candidate, and atomically replace the session."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from notion_client import DEFAULT_SESSION_PATH, NotionAgentClient, NotionError


def parse_curl(text: str) -> dict:
    if not isinstance(text, str) or not text.strip() or len(text) > 500_000:
        raise NotionError("Вставьте один запрос Copy as cURL из Network в Notion.")
    normalized = text.strip().replace("\\\r\n", " ").replace("\\\n", " ").replace("^\r\n", " ").replace("^\n", " ").replace("`\r\n", " ").replace("`\n", " ")
    if '^"' in normalized:
        normalized = normalized.replace('^"', '\\"').replace('^^', '^')
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError as error:
        raise NotionError("Не удалось разобрать кавычки cURL. Используйте Copy as cURL (bash).") from error
    if not tokens or Path(tokens[0]).name.lower() not in ("curl", "curl.exe"):
        raise NotionError("Ожидается команда curl / curl.exe.")
    headers, cookies, urls, bodies = {}, [], [], []
    index = 1
    valued = {"-H", "--header", "-b", "--cookie", "--url", "--data", "--data-raw", "--data-binary", "-d", "-A", "--user-agent", "-e", "--referer", "-X", "--request"}
    while index < len(tokens):
        token = tokens[index]
        option, value = token, None
        if token in valued:
            index += 1
            if index >= len(tokens):
                raise NotionError("В cURL пропущено значение параметра.")
            value = tokens[index]
        elif token.startswith("--") and "=" in token:
            option, value = token.split("=", 1)
        elif token.startswith("-H") and len(token) > 2:
            option, value = "-H", token[2:]
        elif token.startswith("-b") and len(token) > 2:
            option, value = "-b", token[2:]
        elif token.startswith(("https://", "http://")):
            urls.append(token)
        elif token.lower() in ("curl", "curl.exe") or token in (";", "&&", "||", "|"):
            raise NotionError("Вставьте один cURL, без других команд.")
        if option in ("-H", "--header") and value is not None:
            name, separator, header_value = value.partition(":")
            if not separator:
                raise NotionError("В cURL найден некорректный заголовок.")
            headers[name.strip().lower()] = header_value.strip()
        elif option in ("-b", "--cookie") and value is not None:
            if "=" not in value:
                raise NotionError("Нужны сами cookies, а не путь к cookie-файлу.")
            cookies.append(value)
        elif option == "--url" and value is not None:
            urls.append(value)
        elif option in ("--data", "--data-raw", "--data-binary", "-d") and value is not None:
            bodies.append(value)
        elif option in ("-A", "--user-agent") and value is not None:
            headers["user-agent"] = value
        index += 1
    if len(urls) != 1:
        raise NotionError("Нужен ровно один URL запроса Notion.")
    url = urlsplit(urls[0])
    try:
        valid = url.scheme == "https" and url.hostname == "app.notion.com" and url.port in (None, 443) and not url.username and not url.password and url.path.startswith("/api/v3/")
    except ValueError:
        valid = False
    if not valid:
        raise NotionError("Скопируйте запрос https://app.notion.com/api/v3/… из вкладки Notion.")
    if headers.get("cookie"):
        cookies.append(headers["cookie"])
    cookie = "; ".join(cookies)
    parsed = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
    if not parsed.get("token_v2"):
        raise NotionError("В cURL нет cookie token_v2. Скопируйте авторизованный запрос Notion.")
    body = {}
    if bodies:
        try:
            decoded = json.loads(bodies[-1])
            body = decoded if isinstance(decoded, dict) else {}
        except ValueError:
            pass
    parent = body.get("threadParentPointer") or body.get("spacePointer") or {}
    space_id = headers.get("x-notion-space-id") or body.get("spaceId")
    if not space_id and isinstance(parent, dict):
        space_id = parent.get("spaceId") or (parent.get("id") if parent.get("table") == "space" else None)
    if not space_id:
        for request in body.get("requests", []):
            if isinstance(request, dict) and isinstance(request.get("pointer"), dict):
                space_id = request["pointer"].get("spaceId")
                if space_id:
                    break
    user_id = headers.get("x-notion-active-user-header") or unquote(parsed.get("notion_user_id", ""))
    try:
        space_id, user_id = str(uuid.UUID(space_id)), str(uuid.UUID(user_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise NotionError("Не найдены user ID и space ID. Скопируйте getInferenceTranscriptsForUser или syncRecordValuesSpaceInitial.") from error
    config = {"cookie": cookie, "space_id": space_id, "user_id": user_id}
    for header, key in (("notion-client-version", "client_version"), ("x-notion-cell-hint", "cell_hint"), ("user-agent", "user_agent")):
        if headers.get(header):
            config[key] = headers[header]
    if any("\r" in value or "\n" in value for value in config.values()):
        raise NotionError("Некорректный перенос строки в данных сессии.")
    return config


class SessionManager:
    def __init__(self, path: Path = DEFAULT_SESSION_PATH, *, client_factory=NotionAgentClient):
        self.path, self.client_factory = Path(path), client_factory
        self.lock = threading.RLock()
        self.client = None
        self.state = "missing"
        self.message = "Вставьте cURL авторизованного запроса Notion."
        self.checked_at = None
        try:
            self.client = self.client_factory(json.loads(self.path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError, KeyError, TypeError, NotionError):
            pass

    def status(self) -> dict:
        with self.lock:
            return {"state": self.state, "message": self.message, "checked_at": self.checked_at, "space_id": self.client.space_id if self.client else None, "user_id": self.client.user_id if self.client else None, "needs_session": self.state in ("missing", "expired")}

    def note_failure(self, error: NotionError):
        if error.status in (401, 403):
            self.state, self.message = "expired", "Notion отклонил сессию. Вставьте свежий cURL авторизованного запроса."

    def check(self) -> dict:
        with self.lock:
            if self.client is None:
                return self.status()
            try:
                self.client.validate_session()
                self.state, self.message = "active", "Сессия проверена. Можно открыть чат."
            except NotionError as error:
                self.state, self.message = "unavailable", str(error)
                self.note_failure(error)
            self.checked_at = datetime.now(timezone.utc).isoformat()
            return self.status()

    def require_client(self):
        if self.client is None or self.state in ("missing", "expired"):
            raise NotionError(self.message, status=401)
        return self.client

    def update_from_curl(self, text: str) -> dict:
        config = parse_curl(text)
        with self.lock:
            candidate = self.client_factory(config)
            try:
                candidate.validate_session()
                config["cookie"] = candidate.http.headers["Cookie"]
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp_name = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".session-", suffix=".tmp", delete=False) as temporary:
                        temp_name = temporary.name
                        json.dump(config, temporary, ensure_ascii=False, indent=2)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                    os.replace(temp_name, self.path)
                finally:
                    if temp_name and Path(temp_name).exists():
                        Path(temp_name).unlink()
            except Exception:
                candidate.http.close()
                raise
            old, self.client = self.client, candidate
            self.state, self.message = "active", "Сессия обновлена и проверена."
            self.checked_at = datetime.now(timezone.utc).isoformat()
            if old:
                old.http.close()
            return self.status()
