from __future__ import annotations

import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from ..audio import detect_audio_language
from ..matching import classify_candidate
from ..models import Candidate, Film, MatchTier, Subtitle
from ..ranking import language_tier
from .prehrajto import infer_title_language

BASE_URL = "https://online.sktorrent.eu"
TRACK_RE = re.compile(r"<track\s+([^>]+?)/?>", re.IGNORECASE)
ATTR_RE = re.compile(r"([\w-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))")


def parse_tracks(html: str) -> list[Subtitle]:
    tracks = []
    for match in TRACK_RE.finditer(html):
        attrs = {
            item.group(1).lower(): item.group(2) or item.group(3) or item.group(4) or ""
            for item in ATTR_RE.finditer(match.group(1))
        }
        if attrs.get("kind", "subtitles").lower() not in {
            "subtitles",
            "substitles",
            "captions",
        }:
            continue
        src = attrs.get("src")
        if not src:
            continue
        label = attrs.get("label", "")
        lang = attrs.get("srclang", "").lower()
        if re.search(r"(?:č|c)esk", label, re.IGNORECASE):
            lang = "cs"
        elif re.search(r"slovensk", label, re.IGNORECASE):
            lang = "sk"
        tracks.append(
            Subtitle(
                lang=lang,
                url=urllib.parse.urljoin(BASE_URL, src),
                label=label,
                format="vtt",
            )
        )
    return tracks


def parse_search_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    hits = []
    for anchor in soup.select("a[href*='/video/']"):
        match = re.search(r"/video/(\d+)", anchor.get("href", ""))
        if not match:
            continue
        container = anchor.find_parent(["article", "li", "tr", "div"])
        title = (anchor.get("title") or anchor.get_text(" ", strip=True)).strip()
        duration = None
        if container:
            duration_text = container.select_one(".duration, .time, [data-duration]")
            if duration_text:
                raw = duration_text.get("data-duration") or duration_text.get_text(
                    strip=True
                )
                if raw and str(raw).isdigit():
                    duration = int(raw)
        hits.append(
            {"source_id": match.group(1), "title": title, "duration_sec": duration}
        )
    unique = {hit["source_id"]: hit for hit in hits}
    return list(unique.values())


def cdn_candidates(url: str):
    yield url
    path = re.sub(r"https?://(?:online\d*\.)?sktorrent\.eu", "", url)
    for number in range(1, 31):
        candidate = f"https://online{number}.sktorrent.eu{path}"
        if candidate != url:
            yield candidate


@dataclass
class SkTorrentProvider:
    session: requests.Session
    use_whisper: bool = False

    def _probe_cdn(self, candidate: str) -> bool:
        try:
            response = self.session.head(
                candidate,
                headers={"Referer": "https://sktorrent.eu/"},
                timeout=8,
                allow_redirects=True,
            )
        except requests.RequestException:
            return False
        return (
            response.status_code in (200, 206)
            and int(response.headers.get("Content-Length", "0")) > 1_000_000
        )

    def _resolve_cdn(self, url: str) -> str | None:
        candidates = list(cdn_candidates(url))
        # Probe bounded batches concurrently. This retains deterministic edge
        # preference while reducing the worst case from 31 * 8 seconds to
        # four 8-second batches.
        for offset in range(0, len(candidates), 8):
            batch = candidates[offset : offset + 8]
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                results = list(executor.map(self._probe_cdn, batch))
            for candidate, available in zip(batch, results, strict=True):
                if available:
                    return candidate
        return None

    def _from_source(self, film: Film, source: dict) -> Candidate | None:
        metadata = source.get("metadata") or {}
        source_id = str(source["external_id"])
        placeholder = metadata.get("url") or (
            f"https://online.sktorrent.eu/media/videos//h264/{source_id}_720p.mp4"
        )
        stream = self._resolve_cdn(placeholder)
        if not stream:
            return None
        if film.runtime_min and source.get("duration_sec"):
            runtime_delta = (
                abs(source["duration_sec"] / 60 - film.runtime_min) / film.runtime_min
            )
            if runtime_delta > 0.50:
                return None
        try:
            detail = self.session.get(
                f"{BASE_URL}/video/{source_id}/",
                headers={"Accept-Encoding": "identity"},
                timeout=30,
            )
        except requests.RequestException:
            detail = None
        subtitles = parse_tracks(detail.text) if detail is not None and detail.ok else []
        for sub in source.get("subtitles") or []:
            subtitles.append(
                Subtitle(
                    lang=(sub.get("lang") or "").lower(),
                    url=sub.get("url"),
                    label=sub.get("label"),
                    format=sub.get("format"),
                )
            )
        audio = source.get("audio_lang")
        evidence = source.get("audio_detected_by") or "catalog_metadata"
        if not audio:
            detected = detect_audio_language(stream, use_whisper=self.use_whisper)
            if detected.language:
                audio = detected.language
                evidence = f"{detected.method}:{detected.confidence:.3f}"
            else:
                audio, evidence = infer_title_language(
                    source.get("title") or film.title, film, subtitles
                )
        resolution_match = re.search(r"(\d{3,4})p", stream, re.IGNORECASE)
        return Candidate(
            provider="sktorrent",
            source_id=source_id,
            url=f"{BASE_URL}/video/{source_id}/",
            title=source.get("title") or film.title,
            duration_sec=source.get("duration_sec"),
            audio_language=audio,
            language_evidence=evidence,
            language_tier=language_tier(audio, subtitles),
            resolution=int(resolution_match.group(1)) if resolution_match else 720,
            stream_url=stream,
            subtitles=subtitles,
            match_tier=MatchTier.STRONG,
            match_evidence={"method": "catalog_stable_film_id"},
        )

    def discover(self, film: Film) -> list[Candidate]:
        candidates = []
        for source in film.sources:
            if source.get("provider") == "sktorrent" and source.get("is_alive", True):
                candidate = self._from_source(film, source)
                if candidate:
                    candidates.append(candidate)
        if candidates:
            return candidates

        # Conservative live search is used only when the snapshot has no SK metadata.
        query = f"{film.title} {film.year or ''}".strip()
        try:
            response = self.session.get(
                BASE_URL + "/search",
                params={"q": query},
                headers={"Accept-Encoding": "identity"},
                timeout=30,
            )
        except requests.RequestException:
            return []
        if not response.ok:
            return []
        for hit in parse_search_html(response.text):
            result = classify_candidate(
                film, hit["title"], duration_sec=hit["duration_sec"]
            )
            if result.tier not in (MatchTier.STRONG, MatchTier.SOLID):
                continue
            source = {
                "external_id": hit["source_id"],
                "title": hit["title"],
                "duration_sec": hit["duration_sec"],
                "metadata": {},
                "subtitles": [],
            }
            candidate = self._from_source(film, source)
            if candidate:
                candidate.query = query
                candidate.match_tier = result.tier
                candidate.match_evidence = result.evidence
                candidates.append(candidate)
        return candidates
