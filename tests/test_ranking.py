from cr_films_to_prehrajto.models import Candidate, LanguageTier, MatchTier, Subtitle
from cr_films_to_prehrajto.ranking import rank_candidates, subtitle_will_survive


def candidate(source_id, tier, resolution, subtitles=None):
    return Candidate(
        provider="prehrajto",
        source_id=source_id,
        url="https://prehraj.to/x",
        title="Film 2000",
        language_tier=tier,
        resolution=resolution,
        subtitles=subtitles or [],
        match_tier=MatchTier.SOLID,
    )


def test_czech_720_beats_foreign_1080():
    ranked = rank_candidates(
        [
            candidate("foreign", LanguageTier.UNACCEPTABLE, 1080),
            candidate("cz", LanguageTier.CZECH_AUDIO, 720),
        ]
    )
    assert ranked[0].source_id == "cz"


def test_czech_1080_beats_czech_720():
    ranked = rank_candidates(
        [
            candidate("720", LanguageTier.CZECH_AUDIO, 720),
            candidate("1080", LanguageTier.CZECH_AUDIO, 1080),
        ]
    )
    assert ranked[0].source_id == "1080"


def test_slovak_beats_czech_subtitles():
    ranked = rank_candidates(
        [
            candidate(
                "subs",
                LanguageTier.CZECH_SUBTITLES,
                1080,
                [Subtitle("cs", "https://x/sub.vtt")],
            ),
            candidate("sk", LanguageTier.SLOVAK_AUDIO, 720),
        ]
    )
    assert ranked[0].source_id == "sk"


def test_external_czech_subtitle_must_have_url():
    item = candidate("subs", LanguageTier.CZECH_SUBTITLES, 1080, [Subtitle("cs")])
    assert not subtitle_will_survive(item)
    assert rank_candidates([item]) == []


def test_burned_in_czech_subtitle_is_acceptable():
    item = candidate(
        "subs", LanguageTier.CZECH_SUBTITLES, 720, [Subtitle("cs", burned_in=True)]
    )
    assert subtitle_will_survive(item)


def test_ambiguous_candidate_never_ranks():
    item = candidate("amb", LanguageTier.CZECH_AUDIO, 1080)
    item.match_tier = MatchTier.AMBIGUOUS
    assert rank_candidates([item]) == []
