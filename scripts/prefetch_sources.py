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
    result: dict[str, list[dict]] = {}
    for index, film in enumerate(films, 1):
        hits: dict[str, dict] = {}
        for alias in dict.fromkeys(a for a in (film.title, film.original_title) if a):
            query = f"{alias} ({film.year})" if film.year else alias
            try:
                response = session.get(
                    BASE_URL + "/hledej/" + urllib.parse.quote(query, safe=""),
                    timeout=15,
                )
                response.raise_for_status()
            except requests.RequestException:
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
                    normalized_hit.startswith(alias) or f" {alias} " in normalized_hit
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
