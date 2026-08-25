from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import requests

from .models import AccountVideo
from .providers.prehrajto import BASE_URL, parse_inventory_html
from .state import StateStore, now_iso


class GitStatePusher:
    def __init__(self, state_path: Path, shard_id: int, retries: int = 30):
        self.state_path = state_path
        self.shard_id = shard_id
        self.retries = retries

    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def __call__(self, _path: Path) -> None:
        add = self._run("add", "--", str(self.state_path))
        if add.returncode != 0:
            raise RuntimeError("Could not stage production state")
        if self._run("diff", "--cached", "--quiet").returncode == 0:
            return
        commit = self._run(
            "commit",
            "-m",
            f"chore(production shard {self.shard_id}): persist state",
        )
        if commit.returncode != 0:
            raise RuntimeError("Could not commit production state")
        for attempt in range(self.retries):
            if self._run("push", "origin", "HEAD:main").returncode == 0:
                return
            pull = self._run("pull", "--rebase", "origin", "main")
            if pull.returncode != 0:
                # Another shard can update main while the rebase is running.
                # Retry the complete fetch/rebase/push cycle instead of
                # aborting the production job on a transient race.
                time.sleep(min(attempt + 1, 10))
                continue
            # Give the other shard a short window to finish its push.  The
            # bounded backoff prevents two fast shards from starving each
            # other while keeping state persistence crash-safe.
            time.sleep(min(attempt + 1, 10))
        raise RuntimeError("Could not push production state after retries")


def account_video_status(
    session: requests.Session,
    display_name: str,
    video_id: str,
) -> tuple[str, AccountVideo | None]:
    for status, deleted in (("active", False), ("deleted", True)):
        params = {"searchPhrase": display_name}
        if deleted:
            params["filterIsDeleted"] = "1"
        response = session.get(
            BASE_URL + "/profil/nahrana-videa",
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        rows, _ = parse_inventory_html(response.text)
        video = next((row for row in rows if row.video_id == str(video_id)), None)
        if video:
            if status == "active" and "zpracovává se" in video.name.lower():
                return "pending", video
            return status, video
    return "pending", None


def verify_pending_uploads(
    state_paths: list[Path],
    lookup: Callable[[str, str], tuple[str, AccountVideo | None]],
    *,
    attempts: int = 1,
    delay_seconds: float = 0,
    subtitle_lookup: Callable[[str, str], bool] | None = None,
) -> dict[str, int]:
    states = [StateStore(path) for path in state_paths if path.exists()]
    for attempt_number in range(attempts):
        pending = []
        for state in states:
            for film_id, row in state.data["films"].items():
                upload = row.get("upload") or {}
                if upload.get("processing_status") == "pending" or (
                    upload.get("language_tier") == "czech_subtitles"
                    and upload.get("subtitle_verification") == "pending"
                ):
                    pending.append((state, film_id, row, upload))
        if not pending:
            break
        for state, film_id, row, upload in pending:
            status, video = lookup(
                upload.get("display_name", ""), str(upload["target_video_id"])
            )
            if status == "active":
                upload["processing_status"] = "active"
                upload["verified_at"] = now_iso()
                if video and video.name:
                    upload["live_display_name"] = video.name
                if (
                    upload.get("language_tier") == "czech_subtitles"
                    and upload.get("subtitle_verification") == "pending"
                    and subtitle_lookup
                    and subtitle_lookup(
                        upload.get("display_name", ""),
                        str(upload["target_video_id"]),
                    )
                ):
                    upload["subtitle_verification"] = "verified"
                state.save(notify=False)
            elif status == "deleted":
                failed = row.pop("upload")
                state.record_attempt(
                    int(film_id),
                    {
                        "provider": failed.get("provider"),
                        "source_id": failed.get("source_id"),
                        "status": "failed_after_upload",
                        "reason": "Target video moved to deleted inventory during processing",
                        "permanent": True,
                        "deleted_target_video_id": failed.get("target_video_id"),
                        "discovery_exhausted": False,
                    },
                )
        if attempt_number + 1 < attempts:
            remaining = any(
                (row.get("upload") or {}).get("processing_status") == "pending"
                for state in states
                for row in state.data["films"].values()
            )
            if remaining:
                time.sleep(delay_seconds)
    counts = {"active": 0, "pending": 0, "failed": 0}
    for state in states:
        for row in state.data["films"].values():
            upload = row.get("upload") or {}
            if upload.get("processing_status") == "active":
                counts["active"] += 1
            elif upload.get("processing_status") == "pending" or (
                upload.get("language_tier") == "czech_subtitles"
                and upload.get("subtitle_verification") == "pending"
            ):
                counts["pending"] += 1
            counts["failed"] += sum(
                attempt.get("status") == "failed_after_upload"
                for attempt in row.get("attempts", [])
            )
    return counts
