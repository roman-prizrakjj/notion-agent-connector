"""Local chat UI with startup session validation and cURL renewal."""

from __future__ import annotations

import argparse
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from notion_client import DEFAULT_SESSION_PATH, NotionError
from session_pool import SessionPool
from connector_api import ConnectorAPI


def make_server(port=8765, session_path=DEFAULT_SESSION_PATH, *, manager=None):
    manager = manager or SessionPool(session_path)
    connector = ConnectorAPI(manager)
    local_token = secrets.token_urlsafe(32)
    html = Path(__file__).with_name("index.html").read_text(encoding="utf-8").replace("__LOCAL_TOKEN__", local_token)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {"http://" + host for host in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def reply(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def check_request(self, *, api=True, external=False):
            if self.headers.get("Host") not in allowed_hosts:
                self.reply(403, {"error": "Некорректный Host."})
                return False
            if self.headers.get("Origin") and self.headers["Origin"] not in allowed_origins:
                self.reply(403, {"error": "Некорректный Origin."})
                return False
            if external:
                authorization = self.headers.get("Authorization", "")
                key = getattr(manager, "api_key", None)
                if not key or not secrets.compare_digest(authorization, "Bearer " + key):
                    self.reply(401, {"error": {"message": "Передайте ключ коннектора в Authorization: Bearer …", "type": "authentication_error", "code": "invalid_api_key"}})
                    return False
            elif api and not secrets.compare_digest(self.headers.get("X-Local-Token", ""), local_token):
                self.reply(403, {"error": "Откройте локальную страницу клиента."})
                return False
            return True

        def report_error(self, error):
            with manager.lock:
                expired = isinstance(error, NotionError) and error.status in (401, 403)
                if isinstance(error, NotionError):
                    manager.note_failure(error)
                self.reply(401 if expired else 502 if isinstance(error, (NotionError, OSError)) else 400, {"error": str(error), "code": "session_expired" if expired else "request_failed", "session": manager.status()})

        def external_error_body(self, error):
            details = error.details if isinstance(error, NotionError) else {}
            return {"error": {"message": str(error), "type": "notion_connector_error", "code": details.get("code", details.get("category", "request_failed"))}, "notion": details}

        def report_external_error(self, error):
            status = error.status if isinstance(error, NotionError) else 400
            if isinstance(error, NotionError) and error.details.get("uncertain_outcome"):
                status = 409
            if status in (401, 403):
                status = 503  # This is an upstream session error, not a connector-key error.
            self.reply(status if status in (400, 404, 409, 429, 503) else 502, self.external_error_body(error))

        def empty_reply(self, status):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/openapi.json":
                if self.check_request(api=False):
                    self.reply(200, connector.openapi(f"http://127.0.0.1:{self.server.server_address[1]}"))
                return
            if url.path in ("/v1/models", "/mcp"):
                if not self.check_request(external=True):
                    return
                if url.path == "/mcp":
                    self.send_response(405)
                    self.send_header("Allow", "POST")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.reply(200, connector.models())
                return
            if url.path == "/":
                if not self.check_request(api=False):
                    return
                body = html.encode("utf-8")
                self.send_response(200)
                for key, value in {
                    "Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body)),
                    "Cache-Control": "no-store", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                }.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
                return
            if not self.check_request():
                return
            try:
                with manager.lock:
                    if url.path == "/api/session":
                        self.reply(200, manager.status())
                        return
                    if url.path == "/api/connection":
                        base = f"http://127.0.0.1:{self.server.server_address[1]}"
                        config = {
                            "$schema": "https://opencode.ai/config.json",
                            "provider": {"notion": {"npm": "@ai-sdk/openai-compatible", "name": "Notion Agent", "options": {"baseURL": base + "/v1", "apiKey": "{env:NOTION_CONNECTOR_KEY}"}, "models": {"notion-auto": {"name": "Notion · авто"}}}},
                            "mcp": {"notion": {"type": "remote", "url": base + "/mcp", "enabled": True, "oauth": False, "timeout": 300000, "headers": {"Authorization": "Bearer {env:NOTION_CONNECTOR_KEY}"}}},
                        }
                        self.reply(200, {"base_url": base + "/v1", "mcp_url": base + "/mcp", "openapi_url": base + "/openapi.json", "api_key": manager.api_key, "opencode": config})
                        return
                    params = parse_qs(url.query)
                    session_id = params.get("session_id", [None])[0]
                    client = manager.require_client(session_id) if isinstance(manager, SessionPool) else manager.require_client()
                    if url.path == "/api/threads":
                        cursor = json.loads(params["cursor"][0]) if "cursor" in params else None
                        data = client.list_threads(cursor=cursor)
                        self.reply(200, {key: data.get(key) for key in ("transcripts", "threadIds", "unreadThreadIds", "hasMore", "nextCursor")})
                    elif url.path == "/api/thread":
                        thread_id = parse_qs(url.query).get("id", [""])[0]
                        self.reply(200, client.public_thread(client.load_thread(thread_id)))
                    else:
                        self.reply(404, {"error": "Маршрут не найден."})
            except (NotionError, ValueError, OSError) as error:
                self.report_error(error)

        def read_body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 600_000:
                raise ValueError("Некорректный размер запроса.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Ожидается JSON-объект.")
            return body

        def do_POST(self):
            path = urlsplit(self.path).path
            external = path in ("/mcp", "/v1/chat/completions")
            if not self.check_request(external=external):
                return
            if external:
                try:
                    body = self.read_body()
                    if path == "/mcp":
                        result = connector.rpc(body)
                        self.empty_reply(202) if result is None else self.reply(200, result)
                        return
                    if not isinstance(body.get("stream", False), bool):
                        raise NotionError("stream должен быть boolean.", status=400)
                    if not body.get("stream"):
                        self.reply(200, connector.completion(body))
                        return
                    chunks = connector.chunks(body)
                    first = next(chunks)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    disconnected = False
                    try:
                        for chunk in (item for iterator in ([first], chunks) for item in iterator):
                            if disconnected:
                                continue
                            try:
                                self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                                disconnected = True
                        if not disconnected:
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                    except (NotionError, ValueError) as error:
                        if not disconnected:
                            self.wfile.write(("data: " + json.dumps(self.external_error_body(error), ensure_ascii=False) + "\n\n").encode("utf-8"))
                            self.wfile.flush()
                    finally:
                        chunks.close()
                except (NotionError, ValueError, OSError, StopIteration) as error:
                    self.report_external_error(error)
                return
            try:
                body = self.read_body()
                if path == "/api/session/check":
                    self.reply(200, manager.check(body.get("session_id")) if isinstance(manager, SessionPool) else manager.check())
                    return
                if path == "/api/session/select":
                    self.reply(200, manager.select(body.get("session_id")))
                    return
                if path == "/api/session/configure":
                    session_id = body.pop("session_id", None)
                    self.reply(200, manager.configure(session_id, **body))
                    return
                if path == "/api/session/refresh":
                    try:
                        state = manager.update_from_curl(body.get("curl", ""), name=body.get("name"), priority=body.get("priority"), session_id=body.get("session_id"), add=body.get("add", False)) if isinstance(manager, SessionPool) else manager.update_from_curl(body.get("curl", ""))
                        self.reply(200, state)
                    except (NotionError, ValueError, KeyError, OSError) as error:
                        # Failed candidates leave the old session untouched.
                        self.reply(400, {"error": str(error), "session": manager.status()})
                    return
                if path != "/api/send":
                    self.reply(404, {"error": "Маршрут не найден."})
                    return
                if not isinstance(body.get("text"), str):
                    raise ValueError("Нужны thread_id и text.")
                with manager.lock:
                    if isinstance(manager, SessionPool):
                        events = manager.send_message(body.get("thread_id", ""), body["text"], session_id=body.get("session_id"), allow_failover=body.get("auto_failover", True))
                    else:
                        events = manager.require_client().send_message(body.get("thread_id", ""), body["text"])
                    first = next(events)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    disconnected = False
                    try:
                        for event in (item for iterator in ([first], events) for item in iterator):
                            if event.get("status") in (401, 403):
                                if not isinstance(manager, SessionPool):
                                    manager.note_failure(NotionError(event.get("message", ""), status=event["status"]))
                                event["code"] = "session_expired"
                            if disconnected:
                                continue
                            try:
                                self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                                disconnected = True
                    finally:
                        events.close()
            except (NotionError, ValueError, OSError, StopIteration) as error:
                self.report_error(error)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    actual_port = server.server_address[1]
    allowed_hosts.clear()
    allowed_hosts.update({f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"})
    allowed_origins.clear()
    allowed_origins.update({"http://" + host for host in allowed_hosts})
    server.daemon_threads = True
    return server, manager


def run(port=8765, session_path=DEFAULT_SESSION_PATH):
    manager = SessionPool(session_path)
    print("Проверка сессии Notion…", flush=True)
    state = manager.check(all_profiles=True)
    print(state["message"], flush=True)
    server, manager = make_server(port, session_path, manager=manager)
    print(f"Notion Agent: http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION_PATH)
    args = parser.parse_args()
    run(args.port, args.session)
