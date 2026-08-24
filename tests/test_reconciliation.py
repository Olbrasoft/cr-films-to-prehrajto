from cr_films_to_prehrajto.models import AccountVideo, ReconciliationStatus
from cr_films_to_prehrajto.reconciliation import reconcile_film


def test_live_historical_stable_id_is_preexisting(film):
    status, evidence = reconcile_film(film, [AccountVideo("99", "anything")], "99")
    assert status == ReconciliationStatus.PREEXISTING
    assert evidence["method"] == "historical_stable_id"


def test_exact_title_and_year_is_preexisting(film):
    status, _ = reconcile_film(film, [AccountVideo("99", "Pelíšky (1999) CZ")])
    assert status == ReconciliationStatus.PREEXISTING


def test_title_only_is_ambiguous_not_missing(film):
    status, _ = reconcile_film(film, [AccountVideo("99", "Pelíšky CZ")])
    assert status == ReconciliationStatus.AMBIGUOUS


def test_wrong_year_does_not_suppress_upload(film):
    status, _ = reconcile_film(film, [AccountVideo("99", "Pelíšky (2007) CZ")])
    assert status == ReconciliationStatus.MISSING
