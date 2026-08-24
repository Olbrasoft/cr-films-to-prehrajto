from __future__ import annotations

import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from ..audio import detect_audio_language
from ..matching import classify_candidate
from ..models import AccountVideo, Candidate, Film, MatchTier, Subtitle
from ..ranking import language_tier

TARGET_EMAIL = "filmy.prehrajto@post.cz"
BASE_URL = "https://prehraj.to"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/145 Safari/537.36"
)

CZ_DUB_RE = re.compile(
    r"\b(?:cz|cesk[ýyae])\s*[._-]?(?:dab|dub|dabing)\b|\bczdub\b", re.IGNORECASE
)
SK_DUB_RE = re.compile(
    r"\b(?:sk|slovensk[ýyae])\s*[._-]?(?:dab|dub|dabing)\b|\bskdub\b", re.IGNORECASE
)
CZ_SUB_RE = re.compile(
    r"\b(?:cz|cesk[éeýy])\s*[._-]?(?:tit|titulky|subs?)\b|\bcztit\b", re.IGNORECASE
)
EN_ONLY_RE = re.compile(r"\b(?:eng|en)\s*(?:only|dub)?\b", re.IGNORECASE)
CZ_AUDIO_RE = re.compile(r"\b(?:cz|czech)\b", re.IGNORECASE)
SK_AUDIO_RE = re.compile(r"\b(?:sk|slovak)\b", re.IGNORECASE)
VIDEO_PUSH_RE = re.compile(r"videos\.push\(\s*\{(?P<body>[^}]+)\}\s*\)\s*;", re.DOTALL)
PROP_RE = re.compile(
    r"(?P<key>src|res|label|default)\s*:\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>true|false))"
)
TRACK_RE = re.compile(
    r"src\s*:\s*[\"'](?P<src>https?://[^\"']+)[\"']\s*,\s*srclang\s*:\s*[\"'](?P<lang>[^\"']+)",
    re.DOTALL,
)
DURATION_RE = re.compile(r"['\"]videoLength['\"]\s*:\s*(\d+)")


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def validate_target_email(email: str) -> None:
    if email.strip().lower() != TARGET_EMAIL:
        raise ValueError(f"Refusing target account other than {TARGET_EMAIL}")


def login(
    email: str, password: str, *, session: requests.Session | None = None
) -> requests.Session:
    validate_target_email(email)
    if not password:
        raise ValueError("PREHRAJTO_PASSWORD is required")
    sess = session or requests.Session()
    sess.headers["User-Agent"] = USER_AGENT
    prime = sess.get(BASE_URL + "/", timeout=30)
    prime.raise_for_status()
    response = sess.post(
        BASE_URL + "/?frm=homepageLoginForm-loginForm",
        files={
            "email": (None, email),
            "password": (None, password),
            "_do": (None, "homepageLoginForm-loginForm-submit"),
            "login": (None, "Přihlásit se"),
        },
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        allow_redirects=False,
        timeout=30,
    )
    response.raise_for_status()
    check = sess.get(BASE_URL + "/profil", allow_redirects=False, timeout=30)
    if check.status_code != 200:
        raise ProviderError("Target account login verification failed")
    return sess


def parse_inventory_html(html: str) -> tuple[list[AccountVideo], int]:
    soup = BeautifulSoup(html, "html.parser")
    videos: dict[str, AccountVideo] = {}
    for node in soup.select("[data-video-id], [data-id]"):
        video_id = node.get("data-video-id") or node.get("data-id")
        if not video_id or not str(video_id).isdigit():
            continue
        name_node = node.select_one(
            "input[name*='name'], .video__title, .uploaded-video__name, h3"
        )
        name = (
            name_node.get("value") if name_node and name_node.name == "input" else None
        ) or (name_node.get_text(" ", strip=True) if name_node else "")
        link = node.select_one("a[href]")
        videos[str(video_id)] = AccountVideo(
            str(video_id), name.strip(), link.get("href") if link else None
        )

    # Management actions carry a stable videoId even when the card lacks data attributes.
    for link in soup.select("a[href*='uploadedVideoListing-videoId=']"):
        match = re.search(r"uploadedVideoListing-videoId=(\d+)", link.get("href", ""))
        if not match:
            continue
        video_id = match.group(1)
        container = link.parent
        name_node = None
        detail_link = None
        for _ in range(12):
            if container is None:
                break
            name_node = container.select_one(
                f"#snippet-uploadedVideoListing-videoName-{video_id}"
            )
            detail_link = next(
                (
                    anchor
                    for anchor in container.select("a[href]")
                    if "Detail souboru" in anchor.get_text(" ", strip=True)
                ),
                None,
            )
            if name_node is not None and detail_link is not None:
                break
            container = container.parent
        if name_node is None or detail_link is None:
            raise ProviderError(
                f"Could not safely parse target inventory video {video_id}"
            )
        name = (
            name_node.get("value") if name_node and name_node.name == "input" else None
        ) or (name_node.get_text(" ", strip=True) if name_node else "")
        detail_url = detail_link.get("href") if detail_link else None
        videos.setdefault(
            video_id,
            AccountVideo(video_id, name.strip(), detail_url),
        )

    pages = [
        int(v)
        for v in re.findall(r"uploadedVideoListing-visualPaginator-page=(\d+)", html)
    ]
    return list(videos.values()), max(pages, default=1)


def inventory_account(session: requests.Session) -> list[AccountVideo]:
    def fetch_page(page: int) -> tuple[list[AccountVideo], int]:
        response = session.get(
            BASE_URL + "/profil/nahrana-videa",
            params={"uploadedVideoListing-visualPaginator-page": page}
            if page > 1
            else None,
            timeout=30,
        )
        response.raise_for_status()
        return parse_inventory_html(response.text)

    inventory: dict[str, AccountVideo] = {}
    first_rows, last_page = fetch_page(1)
    for row in first_rows:
        inventory[row.video_id] = row

    with ThreadPoolExecutor(max_workers=8) as executor:
        pages = executor.map(fetch_page, range(2, last_page + 1))
        for rows, _ in pages:
            for row in rows:
                inventory[row.video_id] = row
    return sorted(inventory.values(), key=lambda item: int(item.video_id))


def parse_search_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    hits = []
    seen = set()
    for container in soup.select("div.video__picture--container"):
        anchor = container.find("a", href=True)
        if not anchor:
            continue
        match = re.match(r"^/(.+?)/([0-9a-f]{8,})$", anchor["href"])
        if not match or match.group(2) in seen:
            continue
        seen.add(match.group(2))
        title_node = container.find("h3", class_="video__title")
        duration_node = container.find("div", class_="video__tag--time")
        duration = None
        if duration_node:
            try:
                parts = [
                    int(part) for part in duration_node.get_text(strip=True).split(":")
                ]
                duration = sum(
                    value * 60**index for index, value in enumerate(reversed(parts))
                )
            except ValueError:
                pass
        hits.append(
            {
                "source_id": match.group(2),
                "url": BASE_URL + anchor["href"],
                "title": title_node.get_text(strip=True)
                if title_node
                else match.group(1),
                "duration_sec": duration,
            }
        )
    return hits


def parse_detail_html(
    html: str,
) -> tuple[list[tuple[int, str]], list[Subtitle], int | None]:
    variants = []
    for match in VIDEO_PUSH_RE.finditer(html):
        props = {}
        for prop in PROP_RE.finditer(match.group("body")):
            props[prop.group("key")] = (
                prop.group("dq") or prop.group("sq") or prop.group("bare")
            )
        if props.get("src") and str(props.get("res", "")).isdigit():
            variants.append((int(props["res"]), props["src"]))
    if not variants:
        raise ProviderError("No playable stream variants", permanent=True)
    subtitles = [
        Subtitle(match.group("lang").lower(), match.group("src"), format="vtt")
        for match in TRACK_RE.finditer(html)
    ]
    duration_match = DURATION_RE.search(html)
    return variants, subtitles, int(duration_match.group(1)) if duration_match else None


def infer_title_language(
    title: str, film: Film, subtitles: list[Subtitle]
) -> tuple[str | None, str]:
    if CZ_DUB_RE.search(title):
        return "cs", "title_cz_audio"
    if SK_DUB_RE.search(title):
        return "sk", "title_sk_audio"
    if CZ_SUB_RE.search(title):
        return None, "title_cz_subtitles"
    if CZ_AUDIO_RE.search(title):
        return "cs", "title_cz_audio_marker"
    if SK_AUDIO_RE.search(title):
        return "sk", "title_sk_audio_marker"
    if EN_ONLY_RE.search(title):
        return "en", "title_foreign_audio"
    original = (film.original_language or "").lower()
    if original in {"cs", "cz", "ces", "cze"}:
        return "cs", "native_film_language"
    if original in {"sk", "slk", "slo"}:
        return "sk", "native_film_language"
    if any(s.lang in {"cs", "cz", "ces", "cze"} for s in subtitles):
        return None, "verified_czech_subtitle_track"
    return None, "unknown"


@dataclass
class PrehrajtoProvider:
    proxy_url: str
    proxy_key: str
    session: requests.Session
    min_gap_seconds: float = 0.0
    max_rate_limit_retries: int = 2
    use_whisper: bool = False
    _last_request: float = 0.0

    def _proxy_get(self, url: str):
        if not self.proxy_url or not self.proxy_key:
            raise ProviderError("CZ proxy configuration is required")
        for attempt in range(self.max_rate_limit_retries + 1):
            wait = self.min_gap_seconds - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.get(
                    self.proxy_url,
                    params={"key": self.proxy_key, "url": url},
                    timeout=30,
                )
            except requests.RequestException as error:
                # requests includes the prepared URL (and therefore proxy key) in
                # exception text. Never propagate that text to public CI logs.
                raise ProviderError(
                    f"CZ proxy request failed ({type(error).__name__})"
                ) from None
            self._last_request = time.monotonic()
            if response.status_code != 429 or attempt == self.max_rate_limit_retries:
                break
            retry_after = response.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdigit() else 15 * (attempt + 1)
            time.sleep(min(delay, 60))
        if response.status_code == 404:
            raise ProviderError("Source not found", permanent=True)
        if response.status_code == 429:
            raise ProviderError("Proxy HTTP 429")
        if response.status_code >= 500:
            raise ProviderError(f"Proxy HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ProviderError(f"Proxy HTTP {response.status_code}", permanent=True)
        return response

    def discover(self, film: Film) -> list[Candidate]:
        discovered: dict[str, Candidate] = {}
        for alias in dict.fromkeys(a for a in (film.title, film.original_title) if a):
            query = f"{alias} ({film.year})" if film.year else alias
            response = self._proxy_get(
                BASE_URL + "/hledej/" + urllib.parse.quote(query, safe="")
            )
            for hit in parse_search_html(response.text):
                match = classify_candidate(
                    film, hit["title"], duration_sec=hit["duration_sec"]
                )
                if match.tier not in (MatchTier.STRONG, MatchTier.SOLID):
                    discovered.setdefault(
                        hit["source_id"],
                        Candidate(
                            provider="prehrajto",
                            source_id=hit["source_id"],
                            url=hit["url"],
                            title=hit["title"],
                            duration_sec=hit["duration_sec"],
                            match_tier=match.tier,
                            match_evidence={
                                **match.evidence,
                                "rejection_reason": match.reason,
                            },
                            query=query,
                        ),
                    )
                    continue
                discovered.setdefault(
                    hit["source_id"],
                    Candidate(
                        provider="prehrajto",
                        source_id=hit["source_id"],
                        url=hit["url"],
                        title=hit["title"],
                        duration_sec=hit["duration_sec"],
                        match_tier=match.tier,
                        match_evidence={
                            **match.evidence,
                            "rejection_reason": match.reason,
                        },
                        query=query,
                    ),
                )
        resolved = []
        for candidate in discovered.values():
            if candidate.match_tier not in (MatchTier.STRONG, MatchTier.SOLID):
                resolved.append(candidate)
                continue
            try:
                variants, subtitles, detail_duration = parse_detail_html(
                    self._proxy_get(candidate.url).text
                )
            except ProviderError:
                continue
            candidate.resolution, candidate.stream_url = max(
                variants, key=lambda item: item[0]
            )
            candidate.subtitles = subtitles
            candidate.duration_sec = detail_duration or candidate.duration_sec
            resolved_match = classify_candidate(
                film, candidate.title, duration_sec=candidate.duration_sec
            )
            candidate.match_tier = resolved_match.tier
            candidate.match_evidence = {
                **resolved_match.evidence,
                "rejection_reason": resolved_match.reason,
            }
            if resolved_match.tier not in (MatchTier.STRONG, MatchTier.SOLID):
                resolved.append(candidate)
                continue
            audio, evidence = infer_title_language(candidate.title, film, subtitles)
            detected = detect_audio_language(
                candidate.stream_url, use_whisper=self.use_whisper
            )
            if detected.language:
                audio = detected.language
                evidence = f"{detected.method}:{detected.confidence:.3f}"
            candidate.audio_language = audio
            candidate.language_evidence = evidence
            candidate.language_tier = language_tier(audio, subtitles)
            resolved.append(candidate)
        return resolved


def upload_video(
    session: requests.Session, path: Path, name: str, description: str
) -> str:
    upload_name = name + path.suffix
    size = path.stat().st_size
    session.get(BASE_URL + "/profil/nahrat-soubor", timeout=30).raise_for_status()
    prepared = session.post(
        BASE_URL + "/profil/nahrat-soubor?do=prepareVideo",
        headers={"X-Requested-With": "XMLHttpRequest"},
        data={
            "description": description,
            "name": upload_name,
            "size": str(size),
            "type": "video/mp4",
            "erotic": "false",
            "folder": "",
            "private": "false",
        },
        timeout=30,
    )
    prepared.raise_for_status()
    payload = prepared.json()
    video_id = str(json.loads(payload["params"])["video_id"])
    with path.open("rb") as handle:
        multipart = [
            ("files", (upload_name, handle, "video/mp4")),
            ("response", (None, payload["response"])),
            ("project", (None, payload["project"])),
            ("nonce", (None, payload["nonce"])),
            ("params", (None, payload["params"])),
            ("signature", (None, payload["signature"])),
        ]
        response = requests.post(
            "https://api.premiumcdn.net/upload/",
            files=multipart,
            headers={"Referer": BASE_URL + "/", "User-Agent": USER_AGENT},
            timeout=3600,
        )
    response.raise_for_status()
    renamed = session.post(
        BASE_URL + f"/profil/nahrana-videa?uploadedVideoListing-videoId={video_id}"
        "&do=uploadedVideoListing-changeVideoName",
        data={"uploadedVideoListing-name": name},
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=30,
    )
    renamed.raise_for_status()
    return video_id
