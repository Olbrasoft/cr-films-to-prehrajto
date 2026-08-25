from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import requests

from .models import Candidate, Film, LanguageTier
from .providers.prehrajto import BASE_URL, parse_inventory_html, upload_video
from .ranking import CZECH_CODES, display_name

MIN_FILE_SIZE = 10_000_000
MAX_FILE_SIZE = 6_000_000_000


class TransferError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        permanent: bool = False,
        target_video_id: str | None = None,
    ):
        super().__init__(message)
        self.permanent = permanent
        self.target_video_id = target_video_id


def content_length(url: str, session: requests.Session | None = None) -> int | None:
    try:
        response = (session or requests).head(url, timeout=30, allow_redirects=True)
        response.raise_for_status()
        return int(response.headers["Content-Length"])
    except (requests.RequestException, KeyError, ValueError):
        return None


def download(url: str, destination: Path) -> int:
    expected = content_length(url)
    if expected is not None and expected > MAX_FILE_SIZE:
        raise TransferError("Source exceeds the 6 GB runner limit", permanent=True)
    command = [
        "curl",
        "-fL",
        url,
        "-H",
        "User-Agent: Mozilla/5.0",
        "--max-time",
        "3600",
        "--speed-time",
        "60",
        "--speed-limit",
        "10000",
        "-sS",
        "-o",
        str(destination),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=3660, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        destination.unlink(missing_ok=True)
        raise TransferError(f"Download failed: {type(error).__name__}") from error
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise TransferError(f"Download failed with curl exit {result.returncode}")
    size = destination.stat().st_size
    if size < MIN_FILE_SIZE:
        destination.unlink(missing_ok=True)
        raise TransferError("Downloaded file is too small", permanent=True)
    if size > MAX_FILE_SIZE:
        destination.unlink(missing_ok=True)
        raise TransferError(
            "Downloaded file exceeds the 6 GB runner limit", permanent=True
        )
    return size


def vtt_to_srt(content: bytes) -> bytes:
    text = content.lstrip(b"\xef\xbb\xbf").decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\ufeff", "")
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("WEBVTT"):
        lines.pop(0)
    blocks = re.split(r"\n\s*\n", "\n".join(lines).strip())
    output = []
    number = 1
    timestamp = re.compile(
        r"(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*"
        r"(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3})"
    )
    for block in blocks:
        block_lines = [line for line in block.splitlines() if line.strip()]
        if block_lines and "-->" not in block_lines[0]:
            block_lines.pop(0)
        if not block_lines:
            continue
        match = timestamp.search(block_lines[0])
        if not match:
            continue
        values = match.groups()
        start = f"{values[0] or '00'}:{values[1]}:{values[2]},{values[3]}"
        end = f"{values[4] or '00'}:{values[5]}:{values[6]},{values[7]}"
        output.append(
            f"{number}\r\n{start} --> {end}\r\n" + "\r\n".join(block_lines[1:])
        )
        number += 1
    return ("\r\n\r\n".join(output) + "\r\n").encode()


def upload_czech_subtitle(session: requests.Session, video_id: str, url: str) -> None:
    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException as error:
        raise TransferError(
            f"Czech subtitle download failed ({type(error).__name__})",
            target_video_id=video_id,
        ) from None
    if response.status_code >= 400:
        raise TransferError(
            f"Czech subtitle download failed (HTTP {response.status_code})",
            target_video_id=video_id,
        )
    content = response.content
    if content.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT"):
        content = vtt_to_srt(content)
    if not content.strip():
        raise TransferError(
            "Czech subtitle file is empty", permanent=True, target_video_id=video_id
        )
    try:
        upload = session.post(
            BASE_URL + "/profil/nahrana-videa?do=uploadedVideoListing-uploadSubtitles",
            files=[
                (
                    "files[]",
                    (f"cs-{int(time.time())}.srt", content, "application/x-subrip"),
                ),
                ("video", (None, video_id)),
            ],
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=60,
        )
    except requests.RequestException as error:
        raise TransferError(
            f"Czech subtitle upload failed ({type(error).__name__})",
            target_video_id=video_id,
        ) from None
    if upload.status_code >= 400:
        raise TransferError("Czech subtitle upload failed", target_video_id=video_id)


def verify_czech_subtitle(
    session: requests.Session,
    video_id: str,
    display_name: str,
    attempts: int = 3,
) -> bool:
    for attempt in range(attempts):
        try:
            listing = session.get(
                BASE_URL + "/profil/nahrana-videa",
                params={"searchPhrase": display_name},
                timeout=30,
            )
            listing.raise_for_status()
            rows, _ = parse_inventory_html(listing.text)
            video = next(
                (item for item in rows if item.video_id == str(video_id)),
                None,
            )
            if video and video.url:
                response = session.get(urllib_join(BASE_URL, video.url), timeout=30)
                if response.ok and re.search(
                    r"srclang\s*:\s*[\"'](?:cs|cz|cze|ces)[\"']",
                    response.text,
                    re.IGNORECASE,
                ):
                    return True
        except requests.RequestException:
            pass
        if attempt + 1 < attempts:
            time.sleep(30)
    return False


def urllib_join(base: str, value: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, value)


class TransferService:
    def __init__(
        self,
        session: requests.Session,
        temp_dir: Path,
        *,
        on_partial_upload: Callable[[Film, Candidate, dict], None] | None = None,
    ):
        self.session = session
        self.temp_dir = temp_dir
        self.on_partial_upload = on_partial_upload

    def transfer(self, film, candidate: Candidate) -> dict:
        if not candidate.stream_url:
            raise TransferError("Candidate has no resolved stream URL", permanent=True)
        name = display_name(film, candidate)
        path = self.temp_dir / f"film-{film.cr_film_id}.mp4"
        video_id = None
        try:
            size = download(candidate.stream_url, path)
            try:
                video_id = upload_video(self.session, path, name, film.description)
            except Exception as error:  # noqa: BLE001 - HTTP/JSON layers vary by endpoint
                raise TransferError(
                    f"Video upload failed ({type(error).__name__})"
                ) from None
            if candidate.language_tier == LanguageTier.CZECH_SUBTITLES:
                if self.on_partial_upload:
                    self.on_partial_upload(
                        film,
                        candidate,
                        {
                            "target_video_id": str(video_id),
                            "display_name": name,
                            "size_bytes": size,
                        },
                    )
                subtitle = next(
                    (
                        s
                        for s in candidate.subtitles
                        if s.lang.lower() in CZECH_CODES and s.url
                    ),
                    None,
                )
                if not subtitle:
                    raise TransferError(
                        "Czech subtitle track cannot be preserved",
                        permanent=True,
                        target_video_id=video_id,
                    )
                upload_czech_subtitle(self.session, video_id, subtitle.url)
                subtitle_status = (
                    "verified"
                    if verify_czech_subtitle(self.session, video_id, name, attempts=1)
                    else "pending"
                )
            else:
                subtitle_status = "not_required"
            return {
                "target_video_id": video_id,
                "display_name": name,
                "size_bytes": size,
                "subtitle_verification": subtitle_status,
            }
        finally:
            path.unlink(missing_ok=True)

    def repair_subtitle(self, film, candidate: Candidate, video_id: str) -> dict:
        subtitle = next(
            (
                item
                for item in candidate.subtitles
                if item.lang.lower() in CZECH_CODES and item.url
            ),
            None,
        )
        if not subtitle:
            raise TransferError(
                "Czech subtitle repair source is unavailable",
                permanent=True,
                target_video_id=video_id,
            )
        upload_czech_subtitle(self.session, video_id, subtitle.url)
        name = display_name(film, candidate)
        subtitle_status = (
            "verified"
            if verify_czech_subtitle(self.session, video_id, name, attempts=1)
            else "pending"
        )
        return {
            "target_video_id": video_id,
            "display_name": name,
            "size_bytes": None,
            "repaired_partial_upload": True,
            "subtitle_verification": subtitle_status,
        }
