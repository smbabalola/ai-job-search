"""Development email provider: records messages in memory and appends one JSON
line per message to ``outbox.log`` (next to the database). Hosted mode refuses it."""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping


class ConsoleEmailProvider:
    name = "console"

    def __init__(self, log_path: Path | None) -> None:
        self.log_path = Path(log_path) if log_path else None
        self.sent: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def send(self, *, to: str, subject: str, text: str, html: str, idempotency_key: str,
             headers: Mapping[str, str]) -> str:
        message_id = f"console-{uuid.uuid4().hex[:16]}"
        record = {"message_id": message_id, "to": to, "subject": subject, "text": text, "html": html,
                  "idempotency_key": idempotency_key, "headers": dict(headers)}
        with self._lock:
            self.sent.append(record)
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return message_id
