"""Dry-run support: capture outgoing messages instead of sending them."""

OUTBOX: list[dict] = []


def record(channel: str, to: str, text: str) -> None:
    OUTBOX.append({"channel": channel, "to": to, "text": text})
    del OUTBOX[:-200]
    print(f"\n[DRY RUN {channel} -> {to}]\n{text}\n", flush=True)
