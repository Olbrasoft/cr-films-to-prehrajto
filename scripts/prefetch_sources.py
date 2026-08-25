#!/usr/bin/env python3
"""Fetch the first search page for films and persist reusable source hits."""
from __future__ import annotations

import argparse
import json
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
    parse_search_html,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=Path("state/missing-films.json"))
    parser.add_argument("--out", type=Path, default=Path("state/source-prefetch.json"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text())
    films = [Film.from_dict(row) for row in snapshot["films"]]
    if args.limit:
        films = films[: args.limit]
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "cs,en;q=0.8",
            "Referer": SEARCH_BASE_URL + "/",
        }
    )
    result: dict[str, list[dict]] = {}
    for index, film in enumerate(films, 1):
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
        if index % 25 == 0:
            print(f"prefetched {index}/{len(films)} films", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"saved {len(result)} films to {args.out}")


if __name__ == "__main__":
    main()
