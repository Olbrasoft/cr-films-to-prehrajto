from cr_films_to_prehrajto.providers.prehrajto import (
    parse_detail_html,
    parse_inventory_html,
    parse_search_html,
)
from cr_films_to_prehrajto.providers.sktorrent import (
    parse_search_html as parse_sk_search,
)
from cr_films_to_prehrajto.providers.sktorrent import parse_tracks


def test_prehrajto_search_fixture(fixtures):
    rows = parse_search_html((fixtures / "prehrajto_search.html").read_text())
    assert rows[0]["source_id"] == "abcdef1234567890"
    assert rows[0]["duration_sec"] == 6900


def test_prehrajto_detail_fixture(fixtures):
    variants, subtitles, duration = parse_detail_html(
        (fixtures / "prehrajto_detail.html").read_text()
    )
    assert max(res for res, _ in variants) == 1080
    assert subtitles[0].lang == "cs"
    assert duration == 6900


def test_inventory_fixture(fixtures):
    videos, pages = parse_inventory_html((fixtures / "inventory.html").read_text())
    assert videos[0].video_id == "12345"
    assert videos[0].name == "Pelíšky (1999) CZ"
    assert pages == 3


def test_sktorrent_detail_fixture(fixtures):
    tracks = parse_tracks((fixtures / "sktorrent_detail.html").read_text())
    assert tracks[0].lang == "cs"
    assert tracks[0].url.endswith("/vtt/42/Cesky.vtt")


def test_sktorrent_search_fixture(fixtures):
    hits = parse_sk_search((fixtures / "sktorrent_search.html").read_text())
    assert hits == [
        {"source_id": "42", "title": "Pelíšky (1999) CZ", "duration_sec": 6900}
    ]
