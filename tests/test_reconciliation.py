from cr_films_to_prehrajto.models import AccountVideo, Film, ReconciliationStatus
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


def test_related_superman_batman_titles_do_not_suppress_upload():
    film = Film(
        cr_film_id=27525,
        slug="superman-batman-apokalypsa",
        title="Superman/Batman: Apokalypsa",
        original_title="Superman/Batman: Apocalypse",
        year=2010,
        runtime_min=79,
        original_language=None,
        description="",
    )
    inventory = [
        AccountVideo(
            "25165925", "Superman/Batman: Veřejní nepřátelé (2009) CZ Dabing"
        ),
        AccountVideo("25169386", "Zombie Apocalypse (2011) CZ Dabing"),
    ]

    status, _ = reconcile_film(film, inventory)

    assert status == ReconciliationStatus.MISSING


def test_vhs_sequels_do_not_match_short_title_prefixes():
    film = Film(
        cr_film_id=28301,
        slug="v-h-s-2",
        title="V/H/S/2",
        original_title=None,
        year=2013,
        runtime_min=96,
        original_language=None,
        description="",
    )
    inventory = [
        AccountVideo("25323008", "V/H/S (2012) CZ Titulky"),
        AccountVideo("25320988", "V/H/S: Viral (2014) CZ Titulky"),
    ]

    status, _ = reconcile_film(film, inventory)

    assert status == ReconciliationStatus.MISSING


def test_vhs_halloween_does_not_match_a_different_installment():
    film = Film(
        cr_film_id=28971,
        slug="v-h-s-halloween",
        title="V/H/S/Halloween",
        original_title=None,
        year=2025,
        runtime_min=115,
        original_language=None,
        description="",
    )

    status, _ = reconcile_film(
        film, [AccountVideo("25265764", "V/H/S/Beyond (2024) CZ Titulky")]
    )

    assert status == ReconciliationStatus.MISSING
