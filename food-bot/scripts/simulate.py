"""Interactive local WhatsApp conversation simulator.

Start the API first:

    uvicorn app.main:app --reload

Then run this script from the project root:

    python scripts/simulate.py

The simulator posts messages to the local webhook, reads the dry-run outbox,
and prints replies. It never contacts Meta or sends a real WhatsApp message.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import itertools
import json
import os
import sys
from typing import Any
from pathlib import Path
from uuid import uuid4

import httpx


def _env_value(name: str, default: str = "") -> str:
    """Read a simple KEY=value entry without adding a dotenv dependency."""
    value = os.getenv(name)
    if value is not None:
        return value
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return default
    for line in env_path.read_text(encoding="utf-8").splitlines():
        key, separator, candidate = line.partition("=")
        if separator and key.strip() == name:
            return candidate.strip().strip("\"'")
    return default


def _signature(raw: bytes, secret: str) -> str | None:
    if not secret:
        return None
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _payload(message_id: str, phone: str, *, text: str | None = None,
            reply_id: str | None = None, media_id: str | None = None) -> dict[str, Any]:
    if media_id is not None:
        message: dict[str, Any] = {
            "id": message_id,
            "from": phone,
            "type": "image",
            "image": {"id": media_id},
        }
    elif reply_id is not None:
        message = {
            "id": message_id,
            "from": phone,
            "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": {"id": reply_id}},
        }
    else:
        message = {
            "id": message_id,
            "from": phone,
            "type": "text",
            "text": {"body": text or ""},
        }
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"messages": [message]}}]}],
    }


def _print_outbox(items: list[dict[str, Any]]) -> None:
    for item in items:
        print(f"\nBOT ({item['channel']} -> {item['to']}):")
        print(item["text"])


def _print_help() -> None:
    print(
        "\nCommands:\n"
        "  :help       Show this help\n"
        "  :outbox     Read pending dry-run messages\n"
        "  :image      Simulate uploading a receipt image\n"
        "  :quit       Exit\n"
        "\n"
        "Type normal text such as 'menu', or type an ID shown in brackets\n"
        "in a bot list/button response (for example cat:Food or add:1).\n"
    )


async def _read_outbox(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    wait_for_reply: bool = False,
) -> None:
    attempts = 20 if wait_for_reply else 1
    for attempt in range(attempts):
        response = await client.get(f"{base_url}/dev/outbox")
        response.raise_for_status()
        items = response.json()
        if items or attempt == attempts - 1:
            _print_outbox(items)
            return
        await asyncio.sleep(0.1)


async def _clear_outbox(client: httpx.AsyncClient, base_url: str) -> None:
    response = await client.get(f"{base_url}/dev/outbox")
    response.raise_for_status()


async def _send(
    client: httpx.AsyncClient,
    base_url: str,
    secret: str,
    message_id: str,
    phone: str,
    *,
    text: str | None = None,
    reply_id: str | None = None,
    media_id: str | None = None,
) -> None:
    payload = _payload(
        message_id,
        phone,
        text=text,
        reply_id=reply_id,
        media_id=media_id,
    )
    raw = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    signature = _signature(raw, secret)
    if signature:
        headers["X-Hub-Signature-256"] = signature
    response = await client.post(
        f"{base_url}/webhook/whatsapp",
        content=raw,
        headers=headers,
    )
    response.raise_for_status()
    await _read_outbox(client, base_url, wait_for_reply=True)


async def run(base_url: str, phone: str, secret: str) -> None:
    print(f"Connected simulator target: {base_url}")
    print(f"Customer phone: {phone}")
    _print_help()

    counter = itertools.count(1)
    session_id = uuid4().hex[:12]
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(f"{base_url}/health")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Could not connect to {base_url}. Start uvicorn first."
            ) from exc
        await _clear_outbox(client, base_url)

        while True:
            try:
                value = input("YOU> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not value:
                continue
            command = value.lower()
            if command in {":quit", ":q", "quit", "exit"}:
                return
            if command == ":help":
                _print_help()
                continue
            if command == ":outbox":
                await _read_outbox(client, base_url)
                continue
            message_id = f"sim-{session_id}-{next(counter)}"
            if command == ":image":
                await _send(
                    client,
                    base_url,
                    secret,
                    message_id,
                    phone,
                    media_id=f"simulated-media-{message_id}",
                )
            elif value.startswith(":"):
                print("Unknown command. Type :help for available commands.")
            elif value.startswith(("cat:", "catpg:", "add:", "qty:", "cart:")) or value in {
                "menu",
                "carts",
                "newcart",
                "checkout",
            }:
                await _send(
                    client,
                    base_url,
                    secret,
                    message_id,
                    phone,
                    reply_id=value,
                )
            else:
                await _send(
                    client,
                    base_url,
                    secret,
                    message_id,
                    phone,
                    text=value,
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--phone",
        default=_env_value("SIMULATOR_PHONE", "2348012345678"),
        help="Simulated customer phone number",
    )
    parser.add_argument(
        "--secret",
        default=_env_value("WHATSAPP_APP_SECRET"),
        help="Webhook secret; defaults to WHATSAPP_APP_SECRET",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args.base_url.rstrip("/"), args.phone, args.secret))
    except httpx.HTTPStatusError as exc:
        print(f"Request failed: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        raise SystemExit(1) from exc
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
