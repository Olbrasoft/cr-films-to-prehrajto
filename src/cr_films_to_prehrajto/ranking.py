from __future__ import annotations

from .models import Candidate, LanguageTier, MatchTier, Subtitle

CZECH_CODES = {"cs", "ces", "cze", "cz"}
SLOVAK_CODES = {"sk", "slk", "slo"}


def has_czech_subtitles(subtitles: list[Subtitle]) -> bool:
    return any(s.lang.lower() in CZECH_CODES for s in subtitles)


def language_tier(
    audio_language: str | None, subtitles: list[Subtitle]
) -> LanguageTier:
    lang = (audio_language or "").lower()
    if lang in CZECH_CODES:
        return LanguageTier.CZECH_AUDIO
    if lang in SLOVAK_CODES:
        return LanguageTier.SLOVAK_AUDIO
    if has_czech_subtitles(subtitles):
        return LanguageTier.CZECH_SUBTITLES
    return LanguageTier.UNACCEPTABLE


def subtitle_will_survive(candidate: Candidate) -> bool:
    if candidate.language_tier != LanguageTier.CZECH_SUBTITLES:
        return True
    return any(
        s.lang.lower() in CZECH_CODES and (s.burned_in or bool(s.url))
        for s in candidate.subtitles
    )


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    acceptable = [
        c
        for c in candidates
        if c.match_tier in (MatchTier.STRONG, MatchTier.SOLID)
        and c.language_tier != LanguageTier.UNACCEPTABLE
        and subtitle_will_survive(c)
    ]
    return sorted(
        acceptable,
        key=lambda c: (int(c.language_tier), -c.resolution, c.source_id),
    )


def display_name(film, candidate: Candidate) -> str:
    base = f"{film.title} ({film.year})" if film.year else film.title
    if candidate.language_tier == LanguageTier.CZECH_SUBTITLES:
        return f"{base} CZ titulky"
    if (film.original_language or "").lower() in CZECH_CODES | SLOVAK_CODES:
        return f"{base} CZ"
    return f"{base} CZ Dabing"
