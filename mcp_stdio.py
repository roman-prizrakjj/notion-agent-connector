"""Portable MCP stdio adapter to the running loopback connector server."""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests

from notion_client import DEFAULT_SESSION_PATH


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--session", default=str(DEFAULT_SESSION_PATH))
    args = parser.parse_args()
    key = os.environ.get("NOTION_CONNECTOR_KEY")
    if not key:
        try:
            with open(args.session, encoding="utf-8-sig") as source:
                key = json.load(source).get("api_key")
        except (OSError, ValueError):
            pass
    if not key:
        print("Запустите server.py и подключите сессию либо задайте NOTION_CONNECTOR_KEY.", file=sys.stderr)
        return 1
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"})
    for line in sys.stdin:
        message = None
        try:
            message = json.loads(line)
            response = session.post(args.url, json=message, timeout=(10, 300))
            if response.status_code == 202:
                continue
            response.raise_for_status()
            result = response.json()
        except (ValueError, requests.RequestException) as error:
            if not isinstance(message, dict) or "id" not in message:
                print("Не удалось передать уведомление серверу.", file=sys.stderr)
                continue
            result = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": "Не удалось обратиться к локальному серверу Notion: " + type(error).__name__}}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
