"""
Hash-chained audit log for guardrail decisions.

Every decision (allow, block, approval, deny) is appended to a JSONL file.
Each entry's hash includes the previous entry's hash, so any tampering
with an earlier entry breaks the chain and is detected by `verify()`.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


GENESIS_HASH = "GENESIS"


@dataclass
class AuditEntry:
    """A single decision recorded in the audit log."""
    timestamp: str
    sequence: int
    tool_name: str
    args: dict
    decision: str  # "allowed" | "blocked" | "approved" | "denied"
    reason: str = ""
    prev_hash: str = GENESIS_HASH
    this_hash: str = ""


class AuditLog:
    """
    Append-only, tamper-evident log of guardrail decisions.

    Usage:
        audit = AuditLog("audit.jsonl")
        audit.append("send_email", {"to": "..."}, "approved")
        assert audit.verify()
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._ensure_file()

    def _ensure_file(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()

    def _read_entries(self) -> list[AuditEntry]:
        """Read all entries from disk. Returns [] for an empty log."""
        entries: list[AuditEntry] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entries.append(AuditEntry(**json.loads(line)))
        return entries

    def _last_hash_and_sequence(self) -> tuple[str, int]:
        entries = self._read_entries()
        if not entries:
            return GENESIS_HASH, 0
        last = entries[-1]
        return last.this_hash, last.sequence


    # Canonical form: sorted keys, no whitespace. This is deterministic for
    # our own writer but not a formally specified canonical form. If you need
    # cross-implementation verification, pin to RFC 8785 (JCS).

    @staticmethod
    def _compute_hash(entry_dict: dict) -> str:
        """Deterministic SHA-256 over the entry contents (excluding this_hash)."""
        canonical = json.dumps(entry_dict, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def append(
        self,
        tool_name: str,
        args: dict[str, Any],
        decision: str,
        reason: str = "",
    ) -> AuditEntry:
        """
        Append a decision to the log. Returns the new entry.
        """
        prev_hash, last_seq = self._last_hash_and_sequence()

        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            sequence=last_seq + 1,
            tool_name=tool_name,
            args=args,
            decision=decision,
            reason=reason,
            prev_hash=prev_hash,
            this_hash="",
        )

        # Compute hash over the entry contents excluding this_hash itself
        entry_dict = asdict(entry)
        entry_dict.pop("this_hash")
        entry.this_hash = self._compute_hash(entry_dict)

        # Append to file
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), sort_keys=True) + "\n")

        return entry

    def verify(self) -> tuple[bool, Optional[int]]:
        """
        Verify the entire chain.

        Returns:
            (True, None) if intact
            (False, sequence_number) if tampered at that entry
        """
        entries = self._read_entries()
        prev = GENESIS_HASH

        for entry in entries:
            # Check prev_hash matches what we expect
            if entry.prev_hash != prev:
                return False, entry.sequence

            # Recompute this_hash from contents
            entry_dict = asdict(entry)
            entry_dict.pop("this_hash")
            expected = self._compute_hash(entry_dict)
            if entry.this_hash != expected:
                return False, entry.sequence

            prev = entry.this_hash

        return True, None

    def tail(self, n: int = 10) -> list[AuditEntry]:
        """Return the last n entries."""
        return self._read_entries()[-n:]

    def __len__(self) -> int:
        return len(self._read_entries())