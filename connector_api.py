"""OpenAI text completions, stateless MCP tools, and OpenAPI documentation."""

from __future__ import annotations

import json
import time
import uuid

from notion_client import NotionError


def normalize_messages(body):
    if body.get("tools") or body.get("functions"):
        raise NotionError("Локальные tool calls OpenAI не поддерживаются этим текстовым мостом. Для инструментов подключите /mcp.", status=400, details={"code": "unsupported_tools"})
    if body.get("tool_choice") not in (None, "none", "auto"):
        raise NotionError("tool_choice не поддерживается.", status=400)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 500:
        raise NotionError("Передайте непустой массив messages, не больше 500 элементов.", status=400)
    normalized = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer", "user", "assistant"):
            raise NotionError("Поддерживаются текстовые роли system, developer, user и assistant.", status=400)
        content = message.get("content")
        if isinstance(content, list):
            if any(not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str) for part in content):
                raise NotionError("Изображения и другие нетекстовые блоки не поддерживаются.", status=400)
            content = "\n".join(part["text"] for part in content)
        if not isinstance(content, str) or message.get("tool_calls"):
            raise NotionError("Нужен текст content без tool_calls.", status=400)
        normalized.append({"role": message["role"], "content": content})
    if len(json.dumps(normalized, ensure_ascii=False)) > 100_000:
        raise NotionError("История превышает лимит клиента в 100 000 символов.", status=400)
    return normalized


class ConnectorAPI:
    PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

    def __init__(self, pool):
        self.pool = pool

    def models(self):
        profiles = self.pool.public_profiles()
        return {"object": "list", "data": [{"id": "notion-auto", "object": "model", "created": 0, "owned_by": "notion-connector"}] + [{"id": "notion-session-" + profile["id"], "object": "model", "created": 0, "owned_by": "notion-connector", "name": profile["name"]} for profile in profiles if profile["enabled"]]}

    def _route(self, body):
        model = body.get("model", "notion-auto")
        if model == "notion-auto":
            return model, None
        if isinstance(model, str) and model.startswith("notion-session-"):
            profile_id = model.removeprefix("notion-session-")
            if profile_id in self.pool.profiles:
                return model, profile_id
        raise NotionError("Неизвестная модель. Используйте /v1/models.", status=400)

    def completion(self, body):
        messages = normalize_messages(body)
        model, session_id = self._route(body)
        final = None
        for event in self.pool.ask(messages, session_id=session_id):
            if event["type"] == "done":
                final = event
        if final is None:
            raise NotionError("Notion не подтвердил завершение ответа.", status=409)
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": final["text"]}, "finish_reason": "stop"}],
            "notion": {"session_id": final["session_id"], "thread_id": final["thread_id"], "trace_id": final["trace_id"]},
        }

    def chunks(self, body):
        messages = normalize_messages(body)
        model, session_id = self._route(body)
        completion_id, created = "chatcmpl-" + uuid.uuid4().hex, int(time.time())
        previous, first, finished = "", True, False
        for event in self.pool.ask(messages, session_id=session_id):
            if event["type"] not in ("text", "done"):
                continue
            text = event.get("text", "")
            if not text.startswith(previous):
                raise NotionError("Ответ Notion изменил уже переданный текст. Обновите историю треда.", status=409)
            delta = text[len(previous):]
            previous = text
            if delta or first:
                payload = {"content": delta}
                if first:
                    payload["role"] = "assistant"
                first = False
                yield {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": payload, "finish_reason": None}], "notion_session_id": event["session_id"]}
            if event["type"] == "done":
                finished = True
                yield {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "notion": {"session_id": event["session_id"], "thread_id": event["thread_id"], "trace_id": event["trace_id"]}}
        if not finished:
            raise NotionError("Поток закончился без подтверждённого ответа.", status=409)

    @staticmethod
    def tools():
        session = {"type": "string", "description": "ID профиля. Не указывайте для автоматического выбора по приоритету."}
        def tool(name, description, properties, required, readonly):
            return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}, "annotations": {"readOnlyHint": readonly, "destructiveHint": False, "openWorldHint": True}}
        return [
            tool("list_notion_sessions", "Список профилей, приоритетов и состояний без cookies.", {}, [], True),
            tool("list_notion_threads", "Прочитать список чатов выбранного профиля Notion.", {"session_id": session}, [], True),
            tool("read_notion_thread", "Прочитать видимую историю чата Notion.", {"session_id": session, "thread_id": {"type": "string"}}, ["thread_id"], True),
            tool("ask_notion_agent", "Отправить вопрос агенту в отдельном треде. Автоматически выбрать резервный профиль при подтверждённом лимите до начала ответа.", {"session_id": session, "prompt": {"type": "string"}}, ["prompt"], False),
            tool("send_notion_message", "Отправить сообщение в существующий тред выбранного профиля. Этот инструмент не переносит чат в другую сессию.", {"session_id": session, "thread_id": {"type": "string"}, "text": {"type": "string"}}, ["thread_id", "text"], False),
        ]

    def call_tool(self, name, args):
        schema = next((tool["inputSchema"] for tool in self.tools() if tool["name"] == name), None)
        if schema is None:
            raise NotionError("Неизвестный инструмент.", status=400)
        if not isinstance(args, dict) or any(key not in schema["properties"] for key in args) or any(key not in args for key in schema["required"]) or any(not isinstance(value, str) for value in args.values()):
            raise NotionError("Некорректные аргументы инструмента.", status=400)
        if name == "list_notion_sessions":
            return {"sessions": self.pool.public_profiles()}
        session_id = args.get("session_id")
        if name == "ask_notion_agent":
            if not args["prompt"].strip() or len(args["prompt"]) > 100_000:
                raise NotionError("Нужен непустой prompt до 100 000 символов.", status=400)
            final = None
            for event in self.pool.ask([{"role": "user", "content": args["prompt"]}], session_id=session_id):
                if event["type"] == "done":
                    final = event
            if final is None:
                raise NotionError("Notion не подтвердил завершение запроса.", status=409)
            return {key: final[key] for key in ("text", "thread_id", "session_id", "trace_id")}
        with self.pool.lock:
            client = self.pool.require_client(session_id)
            if name == "list_notion_threads":
                data = client.list_threads()
                return {key: data.get(key) for key in ("transcripts", "threadIds", "hasMore", "nextCursor")}
            if name == "read_notion_thread":
                return client.public_thread(client.load_thread(args["thread_id"]))
            final = None
            for event in client.send_message(args["thread_id"], args["text"]):
                if event["type"] == "error":
                    raise NotionError(event["message"], status=event.get("status"), details=event.get("details"))
                if event["type"] == "done":
                    final = event
            if final is None or not final.get("completed"):
                raise NotionError("Сообщение отправлено; завершение ещё не подтверждено. Обновите историю.", status=409)
            return final

    def rpc(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if "id" not in message:
            return None
        identifier, method = message["id"], message.get("method")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32602, "message": "Invalid params"}}
        if not method and ("result" in message or "error" in message):
            return None
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                result = {"protocolVersion": requested if requested in self.PROTOCOL_VERSIONS else self.PROTOCOL_VERSIONS[0], "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "notion-agent-connector", "version": "0.2.0"}, "instructions": "Use list_notion_sessions to see available profiles. Calls that send messages create data in the user's Notion account."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                try:
                    value = self.call_tool(params.get("name"), params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value, "isError": False}
                except (NotionError, ValueError) as error:
                    result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "Method not found"}}
            return {"jsonrpc": "2.0", "id": identifier, "result": result}
        except (NotionError, ValueError) as error:
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32602, "message": str(error)}}

    @staticmethod
    def openapi(base_url):
        return {
            "openapi": "3.1.0", "info": {"title": "Notion Agent Connector", "version": "0.2.0", "description": "Text-only OpenAI-compatible bridge and ranked session pool. Local OpenAI function/tool calling and /v1/responses are not supported. MCP tools are available at /mcp."},
            "servers": [{"url": base_url}], "security": [{"BearerAuth": []}],
            "components": {"securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}}, "schemas": {"ChatRequest": {"type": "object", "required": ["model", "messages"], "properties": {"model": {"type": "string", "default": "notion-auto"}, "stream": {"type": "boolean", "default": False}, "messages": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["role", "content"], "properties": {"role": {"type": "string", "enum": ["system", "developer", "user", "assistant"]}, "content": {"type": "string"}}}}}}}},
            "paths": {
                "/v1/models": {"get": {"operationId": "listModels", "summary": "Available routing aliases", "responses": {"200": {"description": "Model list"}}}},
                "/v1/chat/completions": {"post": {"operationId": "chatCompletion", "summary": "Text completion with quota failover", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ChatRequest"}}}}, "responses": {"200": {"description": "JSON completion or SSE stream", "content": {"application/json": {"schema": {"type": "object"}}, "text/event-stream": {"schema": {"type": "string"}}}}, "400": {"description": "Unsupported input"}, "401": {"description": "Invalid connector key"}, "409": {"description": "Ambiguous or pending Notion execution"}, "429": {"description": "No available session"}}}},
            },
        }
