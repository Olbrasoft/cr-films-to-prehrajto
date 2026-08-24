from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class StateStore:
    def __init__(
        self,
        path: Path,
        on_persist: Callable[[Path], None] | None = None,
    ):
        self.path = path
        self.on_persist = on_persist
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "snapshot": {}, "films": {}}
        data = json.loads(self.path.read_text())
        if data.get("schema_version") != 1:
            raise ValueError("Unsupported state schema version")
        data.setdefault("films", {})
        return data

    def save(self, *, notify: bool = True) -> None:
        self.data["updated_at"] = now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        if notify and self.on_persist:
            self.on_persist(self.path)

    def set_snapshot(self, snapshot_id: str, exported_at: str | None) -> None:
        self.data["snapshot"] = {"id": snapshot_id, "exported_at": exported_at}
        self.save(notify=False)

    def film(self, cr_film_id: int) -> dict[str, Any]:
        return self.data["films"].setdefault(str(cr_film_id), {"attempts": []})

    def record_reconciliation(
        self,
        cr_film_id: int,
        status: str,
        evidence: dict,
        identity: dict | None = None,
        *,
        persist: bool = True,
    ) -> None:
        row = self.film(cr_film_id)
        if identity:
            row["identity"] = identity
        row["reconciliation"] = {
            "status": status,
            "evidence": evidence,
            "at": now_iso(),
        }
        if persist:
            self.save(notify=False)

    def record_attempt(self, cr_film_id: int, attempt: dict) -> None:
        attempt = {
            **attempt,
            "snapshot_id": attempt.get("snapshot_id")
            or self.data.get("snapshot", {}).get("id"),
            "at": now_iso(),
        }
        self.film(cr_film_id).setdefault("attempts", []).append(attempt)
        self.save()

    def record_success(self, cr_film_id: int, upload: dict) -> None:
        self.film(cr_film_id)["upload"] = {**upload, "uploaded_at": now_iso()}
        self.save()

    def uploaded(self, cr_film_id: int) -> bool:
        row = self.data["films"].get(str(cr_film_id), {})
        return bool(row.get("upload", {}).get("target_video_id"))

    def burned_source_ids(self, cr_film_id: int) -> set[str]:
        return {
            str(a["source_id"])
            for a in self.data["films"].get(str(cr_film_id), {}).get("attempts", [])
            if a.get("permanent") and a.get("source_id")
        }

    def pending_partial_upload(self, cr_film_id: int) -> dict | None:
        if self.uploaded(cr_film_id):
            return None
        row = self.data["films"].get(str(cr_film_id), {})
        for attempt in reversed(row.get("attempts", [])):
            if attempt.get("partial_target_video_id"):
                return attempt
        return None

    def discovery_exhausted_for_snapshot(self, cr_film_id: int) -> bool:
        snapshot_id = self.data.get("snapshot", {}).get("id")
        return any(
            attempt.get("status") == "no_acceptable_source"
            and attempt.get("discovery_exhausted") is True
            and attempt.get("snapshot_id") == snapshot_id
            for attempt in reversed(
                self.data["films"].get(str(cr_film_id), {}).get("attempts", [])
            )
        )
