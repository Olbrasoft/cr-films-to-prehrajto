from cr_films_to_prehrajto.matching import classify_candidate, normalize_title
from cr_films_to_prehrajto.models import Film, MatchTier


def test_normalizes_diacritics_and_release_noise():
    assert normalize_title("Pelíšky.1080p-CZ-Dabing") == "pelisky"


def test_year_and_runtime_make_strong_match(film):
    result = classify_candidate(film, "Pelíšky (1999) CZ", duration_sec=6900)
    assert result.tier == MatchTier.STRONG


def test_wrong_year_is_rejected_even_with_matching_runtime(film):
    result = classify_candidate(film, "Pelíšky (2005) CZ", duration_sec=6900)
    assert result.tier == MatchTier.REJECT
    assert result.reason == "wrong_year"


def test_wrong_runtime_is_rejected(film):
    result = classify_candidate(film, "Pelíšky (1999) CZ", duration_sec=30 * 60)
    assert result.tier == MatchTier.REJECT
    assert result.reason == "wrong_runtime"


def test_episode_is_rejected(film):
    assert classify_candidate(film, "Pelíšky S01E02 (1999)").reason == "tv_episode"
    assert classify_candidate(film, "Pelíšky 1x02 (1999)").reason == "tv_episode"


def test_title_only_is_ambiguous(film):
    result = classify_candidate(film, "Pelíšky CZ")
    assert result.tier == MatchTier.AMBIGUOUS


def test_original_alias_is_accepted(film):
    result = classify_candidate(film, "Cosy Dens 1999", duration_sec=6900)
    assert result.tier == MatchTier.STRONG


def test_short_generic_alias_does_not_match_other_films():
    film = Film(
        cr_film_id=2108,
        slug="duch",
        title="Duch",
        original_title="Ghost",
        year=1990,
        runtime_min=None,
        original_language=None,
        description="",
    )

    for wrong_title in (
        "Happy Ghost 4 (1990) CZ",
        "Ghost Dad (1990) CZ",
        "A Chinese Ghost Story II (1990) CZ titulky",
        "Erotic Ghost Story (1990) CZ titulky",
    ):
        result = classify_candidate(film, wrong_title)
        assert result.tier == MatchTier.REJECT
        assert result.reason == "title_mismatch"


def test_combined_czech_and_original_alias_remains_acceptable():
    film = Film(
        cr_film_id=2108,
        slug="duch",
        title="Duch",
        original_title="Ghost",
        year=1990,
        runtime_min=None,
        original_language=None,
        description="",
    )

    result = classify_candidate(film, "Duch (Ghost) (1990) CZ Dabing")

    assert result.tier == MatchTier.SOLID
