from __future__ import annotations

import re

from .matching import YEAR_RE, YEAR_TOLERANCE, normalize_title, similarity
from .models import AccountVideo, Film, ReconciliationStatus


ACCOUNT_NAME_NOISE_RE = re.compile(
    r"\.(?:mp4|mkv|avi|webm)\b|\(\s*zpracovává se\s*\)", re.IGNORECASE
)


def _account_match_evidence(film: Film, video: AccountVideo) -> dict:
    aliases = {
        normalize_title(alias)
        for alias in (film.title, film.original_title)
        if alias
    }
    clean_name = ACCOUNT_NAME_NOISE_RE.sub(" ", video.name)
    embedded_year = YEAR_RE.search(clean_name)
    candidate_year = int(embedded_year.group(1)) if embedded_year else None
    candidate_base = normalize_title(YEAR_RE.sub(" ", clean_name))
    title_similarity = max(
        (similarity(candidate_base, alias) for alias in aliases),
        default=0.0,
    )
    year_match = bool(
        film.year
        and candidate_year
        and abs(film.year - candidate_year) <= YEAR_TOLERANCE
    )
    return {
        "target_video_id": video.video_id,
        "account_name": video.name,
        "title_similarity": round(title_similarity, 3),
        "title_exact_alias": candidate_base in aliases,
        "film_year": film.year,
        "candidate_year": candidate_year,
        "year_match": year_match,
    }


def reconcile_film(
    film: Film,
    inventory: list[AccountVideo],
    historical_video_id: str | None = None,
) -> tuple[ReconciliationStatus, dict]:
    live_by_id = {str(video.video_id): video for video in inventory}
    if historical_video_id and str(historical_video_id) in live_by_id:
        video = live_by_id[str(historical_video_id)]
        return ReconciliationStatus.PREEXISTING, {
            "method": "historical_stable_id",
            "target_video_id": video.video_id,
            "account_name": video.name,
        }

    confident: list[dict] = []
    ambiguous: list[dict] = []
    for video in inventory:
        evidence = _account_match_evidence(film, video)
        if evidence["title_exact_alias"] and evidence["year_match"]:
            confident.append(evidence)
        elif evidence["title_exact_alias"] and evidence["candidate_year"] is None:
            ambiguous.append(evidence)

    if len(confident) == 1:
        return ReconciliationStatus.PREEXISTING, {
            "method": "title_year_runtime",
            **confident[0],
        }
    if confident or ambiguous:
        return ReconciliationStatus.AMBIGUOUS, {"matches": confident + ambiguous}
    return ReconciliationStatus.MISSING, {"method": "no_confident_account_match"}
