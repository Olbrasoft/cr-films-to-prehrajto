from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from ..audio import detect_audio_language
from ..matching import YEAR_RE, classify_candidate, normalize_title
from ..models import AccountVideo, Candidate, Film, MatchTier, Subtitle
from ..ranking import language_tier, rank_candidates

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
    cache_value = os.environ.get("PREHRAJTO_INVENTORY_CACHE_DIR")
    cache_dir = Path(cache_value) if cache_value else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch_page(page: int) -> tuple[list[AccountVideo], int]:
        cache_path = cache_dir / f"page-{page}.json" if cache_dir else None
        if cache_path and cache_path.exists():
            payload = json.loads(cache_path.read_text())
            return (
                [AccountVideo(**row) for row in payload["videos"]],
                int(payload["last_page"]),
            )
        for attempt in range(3):
            response = session.get(
                BASE_URL + "/profil/nahrana-videa",
                params={"uploadedVideoListing-visualPaginator-page": page}
                if page > 1
                else None,
                timeout=30,
            )
            if response.status_code < 500 or attempt == 2:
                response.raise_for_status()
                rows, last_page = parse_inventory_html(response.text)
                if cache_path:
                    temporary = cache_path.with_suffix(".tmp")
                    temporary.write_text(
                        json.dumps(
                            {
                                "last_page": last_page,
                                "videos": [
                                    {
                                        "video_id": row.video_id,
                                        "name": row.name,
                                        "url": row.url,
                                    }
                                    for row in rows
                                ],
                            },
                            ensure_ascii=False,
                        )
                    )
                    temporary.replace(cache_path)
                return rows, last_page
            time.sleep(attempt + 1)
        raise ProviderError(f"Could not inventory account page {page}")

    inventory: dict[str, AccountVideo] = {}
    first_rows, last_page = fetch_page(1)
    for row in first_rows:
        inventory[row.video_id] = row

    workers = max(1, min(int(os.environ.get("PREHRAJTO_INVENTORY_WORKERS", "8")), 16))
    with ThreadPoolExecutor(max_workers=workers) as executor:
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
    max_rate_limit_retries: int = 1
    allow_direct: bool = False
    use_whisper: bool = False
    _last_request: float = 0.0

    @staticmethod
    def _catalog_candidates(film: Film) -> list[Candidate]:
        candidates = []
        for source in film.sources:
            if source.get("provider") != "prehrajto" or not source.get(
                "is_alive", True
            ):
                continue
            metadata = source.get("metadata") or {}
            url = metadata.get("url")
            source_id = source.get("external_id")
            if not url or source_id is None:
                continue
            title = source.get("title") or film.title
            result = classify_candidate(
                film,
                title,
                duration_sec=source.get("duration_sec"),
            )
            # Release noise makes very short exact titles (for example "EX")
            # score poorly. A catalog-linked source may pass only when its
            # title prefix, exact year, and runtime all corroborate identity.
            if result.reason == "title_mismatch" and film.year:
                source_title = normalize_title(YEAR_RE.sub(" ", title))
                aliases = {
                    normalize_title(alias)
                    for alias in (film.title, film.original_title)
                    if alias
                }
                duration = source.get("duration_sec")
                runtime_delta = (
                    abs(duration / 60 - film.runtime_min) / film.runtime_min
                    if duration and film.runtime_min
                    else None
                )
                embedded_year = YEAR_RE.search(title)
                exact_prefix = any(
                    source_title == alias or source_title.startswith(alias + " ")
                    for alias in aliases
                )
                if (
                    exact_prefix
                    and embedded_year
                    and int(embedded_year.group(1)) == film.year
                    and runtime_delta is not None
                    and runtime_delta <= 0.20
                ):
                    result = type(result)(
                        MatchTier.SOLID,
                        1.0,
                        {
                            "method": "catalog_source_exact_prefix_year_runtime",
                            "film_year": film.year,
                            "candidate_year": film.year,
                            "runtime_delta": round(runtime_delta, 3),
                        },
                    )
            candidates.append(
                Candidate(
                    provider="prehrajto",
                    source_id=str(source_id),
                    url=str(url),
                    title=title,
                    duration_sec=source.get("duration_sec"),
                    audio_language=source.get("audio_lang"),
                    language_evidence=source.get("audio_detected_by")
                    or "catalog_metadata",
                    match_tier=result.tier,
                    match_evidence={
                        **result.evidence,
                        "catalog_source": True,
                        "rejection_reason": result.reason,
                    },
                    query="exported_catalog_source",
                )
            )
        return candidates

    def _resolve_candidates(
        self, film: Film, candidates: list[Candidate]
    ) -> list[Candidate]:
        resolved = []
        for candidate in candidates:
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
            resolved_runtime_delta = resolved_match.evidence.get("runtime_delta")
            catalog_override = (
                candidate.match_evidence.get("method")
                == "catalog_source_exact_prefix_year_runtime"
                and resolved_match.reason == "title_mismatch"
                and resolved_runtime_delta is not None
                and resolved_runtime_delta <= 0.20
            )
            if not catalog_override:
                candidate.match_tier = resolved_match.tier
                candidate.match_evidence = {
                    **resolved_match.evidence,
                    "catalog_source": candidate.match_evidence.get(
                        "catalog_source", False
                    ),
                    "rejection_reason": resolved_match.reason,
                }
            audio = candidate.audio_language
            evidence = candidate.language_evidence
            if not audio:
                audio, evidence = infer_title_language(
                    candidate.title, film, subtitles
                )
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

    def _proxy_get(self, url: str):
        if self.allow_direct:
            try:
                response = self.session.get(url, timeout=30)
            except requests.RequestException as error:
                raise ProviderError(
                    f"Direct source request failed ({type(error).__name__})"
                ) from None
            if response.status_code >= 500:
                raise ProviderError(f"Direct source HTTP {response.status_code}")
            if response.status_code >= 400:
                raise ProviderError(
                    f"Direct source HTTP {response.status_code}", permanent=True
                )
            return response
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
        catalog_candidates = self._resolve_candidates(
            film, self._catalog_candidates(film)
        )
        if rank_candidates(catalog_candidates):
            return catalog_candidates

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
        return self._resolve_candidates(film, list(discovered.values()))


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
