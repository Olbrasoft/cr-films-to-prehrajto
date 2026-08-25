#!/usr/bin/env python3
"""Fetch the first search page for films and persist reusable source hits."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=Path("state/missing-films.json"))
    parser.add_argument("--out", type=Path, default=Path("state/source-prefetch.json"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--merge", action="store_true")
    parser.add_argument("--target-count", type=int, default=0)
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
        for alias in dict.fromkeys(a for a in (film.title, film.original_title) if a):
            query = f"{alias} ({film.year})" if film.year else alias
            response = None
            for search_base in (BASE_URL, SEARCH_BASE_URL):
                try:
                    candidate_response = session.get(
                        search_base + "/hledej/" + urllib.parse.quote(query, safe=""),
                        timeout=15,
                    )
                    candidate_response.raise_for_status()
                    if parse_search_html(candidate_response.text):
                        response = candidate_response
                        break
                except requests.RequestException:
                    continue
            if response is None:
                continue
            for hit in parse_search_html(response.text):
                match = classify_candidate(
                    film, hit["title"], duration_sec=hit["duration_sec"]
                )
                normalized_hit = normalize_title(hit["title"])
                aliases = [
                    normalize_title(value)
                    for value in (film.title, film.original_title)
                    if value
                ]
                identity = any(
                    normalized_hit.startswith(alias)
                    for alias in aliases
                    if len(alias) >= 4
                )
                if identity and match.tier in (MatchTier.STRONG, MatchTier.SOLID):
                    hits[hit["source_id"]] = hit
            time.sleep(args.delay)
        if hits:
            result[str(film.cr_film_id)] = list(hits.values())
        scanned_ids.add(str(film.cr_film_id))
        if index % 25 == 0:
            print(f"prefetched {index}/{len(films)} films", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(args.out)
    args.scan_state.parent.mkdir(parents=True, exist_ok=True)
    scan_temporary = args.scan_state.with_suffix(args.scan_state.suffix + ".tmp")
    scan_temporary.write_text(
        json.dumps({"film_ids": sorted(scanned_ids, key=int)}, indent=2) + "\n"
    )
    scan_temporary.replace(args.scan_state)
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
    print(
        f"searched {searched} films; saved {len(result)} queued films; "
        f"{remaining} films remain unscanned"
    )


if __name__ == "__main__":
    main()
