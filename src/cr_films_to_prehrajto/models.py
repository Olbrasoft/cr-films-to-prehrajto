from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class LanguageTier(IntEnum):
    CZECH_AUDIO = 1
    SLOVAK_AUDIO = 2
    CZECH_SUBTITLES = 3
    UNACCEPTABLE = 99


class MatchTier(StrEnum):
    STRONG = "strong"
    SOLID = "solid"
    AMBIGUOUS = "ambiguous"
    REJECT = "reject"


class ReconciliationStatus(StrEnum):
    PREEXISTING = "preexisting_on_account"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Subtitle:
    lang: str
    url: str | None = None
    label: str | None = None
    format: str | None = None
    burned_in: bool = False


@dataclass(frozen=True)
class Film:
    cr_film_id: int
    slug: str
    title: str
    original_title: str | None
    year: int | None
    runtime_min: int | None
    original_language: str | None
    description: str
    tmdb_id: int | None = None
    imdb_id: str | None = None
    created_at: str | None = None
    added_at: str | None = None
    sources: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Film:
        return cls(
            cr_film_id=int(row["cr_film_id"]),
            slug=row["slug"],
            title=row["title"],
            original_title=row.get("original_title"),
            year=row.get("year"),
            runtime_min=row.get("runtime_min"),
            original_language=row.get("original_language"),
            description=row.get("description") or "",
            tmdb_id=row.get("tmdb_id"),
            imdb_id=row.get("imdb_id"),
            created_at=row.get("created_at"),
            added_at=row.get("added_at"),
            sources=tuple(row.get("sources") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sources"] = list(self.sources)
        return data


@dataclass
class Candidate:
    provider: str
    source_id: str
    url: str
    title: str
    year: int | None = None
    duration_sec: int | None = None
    language_tier: LanguageTier = LanguageTier.UNACCEPTABLE
    audio_language: str | None = None
    language_evidence: str | None = None
    resolution: int = 0
    stream_url: str | None = None
    subtitles: list[Subtitle] = field(default_factory=list)
    match_tier: MatchTier = MatchTier.REJECT
    match_evidence: dict[str, Any] = field(default_factory=dict)
    query: str | None = None

    def to_dict(self, *, sensitive: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["language_tier"] = self.language_tier.name.lower()
        data["match_tier"] = self.match_tier.value
        if not sensitive:
            data.pop("stream_url", None)
            for subtitle in data["subtitles"]:
                subtitle.pop("url", None)
        return data


@dataclass(frozen=True)
class AccountVideo:
    video_id: str
    name: str
    url: str | None = None
