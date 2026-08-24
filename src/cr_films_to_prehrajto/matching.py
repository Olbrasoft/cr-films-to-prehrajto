from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

from .models import Film, MatchTier

YEAR_TOLERANCE = 1
RUNTIME_TOLERANCE = 0.20
RUNTIME_HARD_REJECT = 0.50
SIMILARITY_GATE = 0.50
EPISODE_RE = re.compile(r"\bS\d{1,2}E\d{1,3}\b|\b\d{1,2}x\d{1,3}\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
RELEASE_NOISE_RE = re.compile(
    r"\b(?:2160p|1080p|720p|480p|bluray|bdrip|webrip|web[ ._-]?dl|hdrip|dvdrip|"
    r"cz(?:ech)?|sk|dab(?:ing)?|titulky|subs?|x26[45]|h26[45])\b",
    re.IGNORECASE,
)


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    value = value.replace("&", " a ")
    value = RELEASE_NOISE_RE.sub(" ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


@dataclass(frozen=True)
class MatchResult:
    tier: MatchTier
    score: float
    evidence: dict
    reason: str | None = None


def classify_candidate(
    film: Film,
    candidate_title: str,
    *,
    candidate_year: int | None = None,
    duration_sec: int | None = None,
) -> MatchResult:
    if EPISODE_RE.search(candidate_title):
        return MatchResult(MatchTier.REJECT, 0.0, {}, "tv_episode")

    aliases = [a for a in (film.title, film.original_title) if a]
    folded = normalize_title(candidate_title)
    scores = [similarity(candidate_title, alias) for alias in aliases]
    contains = any(
        len(normalize_title(alias)) >= 4 and normalize_title(alias) in folded
        for alias in aliases
    )
    score = max(scores or [0.0])
    if contains:
        score = max(score, 1.0)

    embedded_year = YEAR_RE.search(candidate_title)
    hit_year = candidate_year or (
        int(embedded_year.group(1)) if embedded_year else None
    )
    if film.year and hit_year and abs(film.year - hit_year) > YEAR_TOLERANCE:
        return MatchResult(
            MatchTier.REJECT,
            score,
            {"film_year": film.year, "candidate_year": hit_year},
            "wrong_year",
        )

    runtime_delta = None
    if film.runtime_min and duration_sec:
        runtime_delta = abs(duration_sec / 60 - film.runtime_min) / film.runtime_min
        if runtime_delta > RUNTIME_HARD_REJECT:
            return MatchResult(
                MatchTier.REJECT,
                score,
                {"runtime_delta": round(runtime_delta, 3)},
                "wrong_runtime",
            )

    evidence = {
        "title_similarity": round(score, 3),
        "title_contains_alias": contains,
        "film_year": film.year,
        "candidate_year": hit_year,
        "year_match": bool(
            film.year and hit_year and abs(film.year - hit_year) <= YEAR_TOLERANCE
        ),
        "film_runtime_min": film.runtime_min,
        "candidate_runtime_sec": duration_sec,
        "runtime_delta": round(runtime_delta, 3) if runtime_delta is not None else None,
    }
    if score < SIMILARITY_GATE and not contains:
        return MatchResult(MatchTier.REJECT, score, evidence, "title_mismatch")

    year_match = evidence["year_match"]
    runtime_match = runtime_delta is not None and runtime_delta <= RUNTIME_TOLERANCE
    if year_match and runtime_match:
        return MatchResult(MatchTier.STRONG, score, evidence)
    if year_match or runtime_match:
        return MatchResult(MatchTier.SOLID, score, evidence)
    return MatchResult(MatchTier.AMBIGUOUS, score, evidence, "title_only")
