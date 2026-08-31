import pytest
import requests

from cr_films_to_prehrajto.models import AccountVideo
from cr_films_to_prehrajto.providers.prehrajto import (
    ProviderError,
    inventory_account,
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


def test_inventory_action_link_nested_below_title(fixtures):
    videos, pages = parse_inventory_html(
        (fixtures / "inventory_nested.html").read_text()
    )
    assert videos == [
        AccountVideo(
            "25591699",
            "Prime (2025) CZ Dabing",
            "/prime-2025-cz-dabing-mp4/d21f0c3321b9487b",
        )
    ]
    assert pages == 1


def test_inventory_action_link_without_matching_card_fails_closed():
    html = """
    <h3 id="snippet-uploadedVideoListing-videoName-999">Wrong card</h3>
    <a href="/profil/nahrana-videa?uploadedVideoListing-videoId=123&amp;do=x">x</a>
    <a href="/wrong/abc12345">Detail souboru</a>
    """
    with pytest.raises(ProviderError, match="safely parse target inventory video 123"):
        parse_inventory_html(html)


def test_deleted_inventory_requests_deleted_filter():
    class Response:
        status_code = 200
        text = """
            <div data-video-id="123">
              <h3>Deleted Film (2020) CZ</h3>
              <a href="/deleted-film/abcdef12">Detail souboru</a>
            </div>
        """

        def raise_for_status(self):
            return None

    class Session:
        def __init__(self):
            self.params = []

        def get(self, _url, *, params, timeout):
            self.params.append(params)
            assert timeout == 30
            return Response()

    session = Session()
    videos = inventory_account(session, deleted=True)

    assert videos[0].video_id == "123"
    assert session.params == [{"filterIsDeleted": "1"}]


def test_deleted_inventory_retries_gateway_timeout(monkeypatch):
    class Response:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text
            self.headers = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

    class Session:
        def __init__(self):
            self.responses = iter(
                [
                    Response(504),
                    Response(200, '<div data-video-id="123"><h3>Film</h3></div>'),
                ]
            )

        def get(self, _url, *, params, timeout):
            return next(self.responses)

    monkeypatch.setenv("PREHRAJTO_INVENTORY_RETRIES", "3")
    monkeypatch.setattr(
        "cr_films_to_prehrajto.providers.prehrajto.time.sleep", lambda _: None
    )

    assert inventory_account(Session(), deleted=True)[0].video_id == "123"


def test_sktorrent_detail_fixture(fixtures):
    tracks = parse_tracks((fixtures / "sktorrent_detail.html").read_text())
    assert tracks[0].lang == "cs"
    assert tracks[0].url.endswith("/vtt/42/Cesky.vtt")


def test_sktorrent_search_fixture(fixtures):
    hits = parse_sk_search((fixtures / "sktorrent_search.html").read_text())
    assert hits == [
        {"source_id": "42", "title": "Pelíšky (1999) CZ", "duration_sec": 6900}
    ]
