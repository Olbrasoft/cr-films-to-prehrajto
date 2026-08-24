from __future__ import annotations

from .matching import classify_candidate, normalize_title
from .models import AccountVideo, Film, MatchTier, ReconciliationStatus


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

    confident = []
    ambiguous = []
    for video in inventory:
        result = classify_candidate(film, video.name)
        evidence = {
            "target_video_id": video.video_id,
            "account_name": video.name,
            **result.evidence,
        }
        if result.tier in (MatchTier.STRONG, MatchTier.SOLID):
            confident.append(evidence)
        elif result.tier == MatchTier.AMBIGUOUS:
            film_aliases = {
                normalize_title(film.title),
                normalize_title(film.original_title or ""),
            }
            if normalize_title(video.name) in film_aliases or result.score >= 0.75:
                ambiguous.append(evidence)

    if len(confident) == 1:
        return ReconciliationStatus.PREEXISTING, {
            "method": "title_year_runtime",
            **confident[0],
        }
    if confident or ambiguous:
        return ReconciliationStatus.AMBIGUOUS, {"matches": confident + ambiguous}
    return ReconciliationStatus.MISSING, {"method": "no_confident_account_match"}
