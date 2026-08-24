from cr_films_to_prehrajto.matching import classify_candidate, normalize_title
from cr_films_to_prehrajto.models import MatchTier


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
