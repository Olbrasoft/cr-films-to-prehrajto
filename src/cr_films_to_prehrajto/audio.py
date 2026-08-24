from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

ISO_MAP = {"cze": "cs", "ces": "cs", "cz": "cs", "slo": "sk", "slk": "sk"}
UNDEFINED_LANGUAGE_CODES = {"und", "zxx", "mul"}


def normalize_iso(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    if not value:
        return None
    if value in UNDEFINED_LANGUAGE_CODES:
        return None
    return ISO_MAP.get(value, value if len(value) in (2, 3) else None)


@dataclass(frozen=True)
class AudioEvidence:
    language: str | None
    method: str
    confidence: float


def probe_audio_languages(url: str, timeout: int = 30) -> list[str]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream_tags=language",
        "-of",
        "json",
        url,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=True
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    return [
        lang
        for stream in payload.get("streams", [])
        if (lang := normalize_iso((stream.get("tags") or {}).get("language")))
    ]


def detect_audio_language(url: str, *, use_whisper: bool = False) -> AudioEvidence:
    languages = probe_audio_languages(url)
    for preferred in ("cs", "sk"):
        if preferred in languages:
            return AudioEvidence(preferred, "ffprobe_tag", 1.0)
    if languages:
        return AudioEvidence(languages[0], "ffprobe_tag", 0.8)
    if not use_whisper:
        return AudioEvidence(None, "unknown", 0.0)
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return AudioEvidence(None, "whisper_unavailable", 0.0)
    with tempfile.TemporaryDirectory() as directory:
        sample = Path(directory) / "sample.wav"
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "300",
            "-i",
            url,
            "-t",
            "30",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(sample),
        ]
        try:
            subprocess.run(command, capture_output=True, timeout=180, check=True)
            model = WhisperModel("small", device="cpu", compute_type="int8")
            segments, info = model.transcribe(str(sample), beam_size=1, vad_filter=True)
            list(segments)
        except Exception:  # noqa: BLE001 - optional ML backends raise backend-specific errors
            return AudioEvidence(None, "whisper_failed", 0.0)
    return AudioEvidence(
        normalize_iso(info.language), "whisper", float(info.language_probability)
    )
