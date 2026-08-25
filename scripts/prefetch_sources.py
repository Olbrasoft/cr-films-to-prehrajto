#!/usr/bin/env python3
"""Fetch the first search page for films and persist reusable source hits."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import requests

from cr_films_to_prehrajto.matching import classify_candidate, normalize_title
from cr_films_to_prehrajto.models import Film, MatchTier
from cr_films_to_prehrajto.providers.prehrajto import (
    BASE_URL,
    SEARCH_BASE_URL,
    USER_AGENT,
    login,
    parse_search_html,
)


@dataclass(frozen=True)
class SearchResult:
    completed: bool
    hits: list[dict]


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _retry_delay(response: requests.Response, attempt: int, base_delay: float) -> float:
    retry_after = response.headers.get("Retry-After", "")
    try:
        return max(float(retry_after), base_delay)
    except ValueError:
        return max(base_delay * (2**attempt), 1.0)


def search_alias(
    session: requests.Session,
    query: str,
    *,
    delay: float,
    retries: int,
) -> SearchResult:
    """Search both public hostnames and distinguish an empty page from an outage."""
    completed = False
    for search_base in (BASE_URL, SEARCH_BASE_URL):
        for attempt in range(retries + 1):
            try:
                response = session.get(
                    search_base + "/hledej/" + urllib.parse.quote(query, safe=""),
                    timeout=15,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < retries:
                        time.sleep(_retry_delay(response, attempt, delay))
                        continue
                response.raise_for_status()
                hits = parse_search_html(response.text)
                if hits:
                    return SearchResult(True, hits)
                lower_html = response.text.lower()
                # A valid empty result still contains the site's search shell.
                # Do not treat a 200 bot challenge or generic proxy page as a
                # completed query, otherwise that film would never be retried.
                if "přehraj.to" in lower_html and (
                    "hledej" in lower_html or "search" in lower_html
                ):
                    completed = True
                break
            except requests.RequestException:
                if attempt < retries:
                    time.sleep(max(delay * (2**attempt), 1.0))
                    continue
                break
        time.sleep(delay)
    return SearchResult(completed, [])


def acceptable_hits(film: Film, hits: list[dict]) -> dict[str, dict]:
    accepted: dict[str, dict] = {}
    aliases = [
        normalize_title(value)
        for value in (film.title, film.original_title)
        if value
    ]
    for hit in hits:
        match = classify_candidate(film, hit["title"], duration_sec=hit["duration_sec"])
        normalized_hit = normalize_title(hit["title"])
        identity = any(
            normalized_hit.startswith(alias) for alias in aliases if len(alias) >= 4
        )
        if identity and match.tier in (MatchTier.STRONG, MatchTier.SOLID):
            accepted[hit["source_id"]] = hit
    return accepted


def save_progress(
    queue_path: Path,
    scan_path: Path,
    result: dict[str, list[dict]],
    scanned_ids: set[str],
) -> None:
    _write_json_atomic(queue_path, result)
    _write_json_atomic(
        scan_path,
        {"film_ids": sorted(scanned_ids, key=int)},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=Path("state/missing-films.json"))
    parser.add_argument("--out", type=Path, default=Path("state/source-prefetch.json"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--merge", action="store_true")
    parser.add_argument("--target-count", type=int, default=0)
    parser.add_argument("--request-retries", type=int, default=2)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--status-out", type=Path)
    parser.add_argument(
        "--scan-state",
        type=Path,
        default=Path("state/source-prefetch-scan.json"),
    )
    parser.add_argument(
        "--uploaded-index", type=Path, default=Path("state/account-index.json")
    )
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text())
    films = [Film.from_dict(row) for row in snapshot["films"]]
    email = os.environ.get("PREHRAJTO_EMAIL", "")
    password = os.environ.get("PREHRAJTO_PASSWORD", "")
    session = login(email, password) if email and password else requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "cs,en;q=0.8",
            "Referer": SEARCH_BASE_URL + "/",
        }
    )
    result: dict[str, list[dict]] = {}
    if args.merge and args.out.exists():
        payload = json.loads(args.out.read_text())
        if isinstance(payload, dict):
            result.update(payload)
    uploaded_ids: set[str] = set()
    if args.uploaded_index.exists():
        uploaded_payload = json.loads(args.uploaded_index.read_text())
        uploaded_ids.update((uploaded_payload.get("films") or {}).keys())
    for state_path in Path("state").glob("production-shard-*.json"):
        state_payload = json.loads(state_path.read_text())
        uploaded_ids.update(
            film_id
            for film_id, row in (state_payload.get("films") or {}).items()
            if row.get("upload")
        )
    result = {
        film_id: hits
        for film_id, hits in result.items()
        if film_id not in uploaded_ids
    }
    scanned_ids: set[str] = set()
    if args.merge and args.scan_state.exists():
        scan_payload = json.loads(args.scan_state.read_text())
        scanned_ids.update(str(value) for value in scan_payload.get("film_ids", []))
    searched = 0
    for index, film in enumerate(films, 1):
        if args.target_count and len(result) >= args.target_count:
            break
        if args.limit and searched >= args.limit:
            break
        if str(film.cr_film_id) in uploaded_ids:
            continue
        if str(film.cr_film_id) in result:
            scanned_ids.add(str(film.cr_film_id))
            continue
        if str(film.cr_film_id) in scanned_ids:
            continue
        searched += 1
        hits: dict[str, dict] = {}
        all_aliases_completed = True
        for alias in dict.fromkeys(a for a in (film.title, film.original_title) if a):
            query = f"{alias} ({film.year})" if film.year else alias
            search = search_alias(
                session,
                query,
                delay=args.delay,
                retries=max(args.request_retries, 0),
            )
            all_aliases_completed = all_aliases_completed and search.completed
            hits.update(acceptable_hits(film, search.hits))
            # One conservatively matched search page is enough. Resolution,
            # language verification and ranking remain the uploader's job.
            if hits:
                break
        if hits:
            result[str(film.cr_film_id)] = list(hits.values())
        if hits or all_aliases_completed:
            scanned_ids.add(str(film.cr_film_id))
        if args.checkpoint_every and searched % args.checkpoint_every == 0:
            save_progress(args.out, args.scan_state, result, scanned_ids)
        if index % 25 == 0:
            print(f"prefetched {index}/{len(films)} films", flush=True)
    save_progress(args.out, args.scan_state, result, scanned_ids)
    remaining = sum(
        str(film.cr_film_id) not in uploaded_ids
        and str(film.cr_film_id) not in scanned_ids
        for film in films
    )
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a") as output:
            output.write(f"queue={len(result)}\n")
            output.write(f"scanned={len(scanned_ids)}\n")
            output.write(f"remaining={remaining}\n")
    status = {
        "queue": len(result),
        "scanned": len(scanned_ids),
        "remaining": remaining,
        "searched": searched,
    }
    if args.status_out:
        _write_json_atomic(args.status_out, status)
    print(
        f"searched {searched} films; saved {len(result)} queued films; "
        f"{remaining} films remain unscanned"
    )


if __name__ == "__main__":
    main()
