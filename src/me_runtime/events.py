"""Machine-readable JSONL event writer (stdout) for runtime-bridge-v1."""
from __future__ import annotations

import hashlib
import json
import sys
from typing import Any


class EventWriter:
    def __init__(self, run_id: str, stream=None) -> None:
        self.run_id = run_id
        self.seq = 0
        self.stream = stream or sys.stdout

    def emit(self, event: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        payload = {"bridge": "runtime-bridge-v1", "run_id": self.run_id,
                   "seq": self.seq, **event}
        self.stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.stream.flush()
        return payload

    @staticmethod
    def sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()
