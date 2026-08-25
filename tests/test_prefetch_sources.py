import importlib.util
import json
import sys
from pathlib import Path

import requests


PREFETCH_PATH = Path(__file__).parents[1] / "scripts" / "prefetch_sources.py"
SPEC = importlib.util.spec_from_file_location("prefetch_sources", PREFETCH_PATH)
assert SPEC and SPEC.loader
prefetch_sources = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prefetch_sources
SPEC.loader.exec_module(prefetch_sources)


class FakeResponse:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, timeout):
        self.calls.append(url)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_search_alias_retries_rate_limit_and_accepts_empty_page(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(200, "<title>Search - Přehraj.to</title><form>hledej</form>"),
            FakeResponse(200, "<title>Search - Přehraj.to</title><form>hledej</form>"),
        ]
    )
    monkeypatch.setattr(prefetch_sources.time, "sleep", lambda _: None)

    result = prefetch_sources.search_alias(session, "Missing (2026)", delay=0, retries=1)

    assert result.completed is True
    assert result.hits == []
    assert len(session.calls) == 3


def test_search_alias_does_not_accept_a_200_bot_challenge(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(200, "<title>Just a moment...</title>"),
            FakeResponse(200, "<title>Access verification</title>"),
        ]
    )
    monkeypatch.setattr(prefetch_sources.time, "sleep", lambda _: None)

    result = prefetch_sources.search_alias(session, "Pelisky (1999)", delay=0, retries=0)

    assert result.completed is False
    assert result.hits == []


def test_prefetch_stops_after_first_safe_hit_and_persists_status(
    tmp_path, fixtures, film, monkeypatch
):
    snapshot = tmp_path / "missing.json"
    snapshot.write_text(
        json.dumps({"films": [film.to_dict()]}, ensure_ascii=False) + "\n"
    )
    queue = tmp_path / "queue.json"
    scan = tmp_path / "scan.json"
    status = tmp_path / "status.json"
    session = FakeSession([FakeResponse(200, (fixtures / "prehrajto_search.html").read_text())])
    session.headers = {}
    monkeypatch.setattr(prefetch_sources, "login", lambda *_: session)
    monkeypatch.setenv("PREHRAJTO_EMAIL", "filmy.prehrajto@post.cz")
    monkeypatch.setenv("PREHRAJTO_PASSWORD", "secret")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prefetch_sources.py",
            "--snapshot",
            str(snapshot),
            "--out",
            str(queue),
            "--scan-state",
            str(scan),
            "--status-out",
            str(status),
            "--uploaded-index",
            str(tmp_path / "account-index.json"),
            "--delay",
            "0",
        ],
    )

    prefetch_sources.main()

    assert len(session.calls) == 1
    assert list(json.loads(queue.read_text())) == ["42"]
    assert json.loads(scan.read_text())["film_ids"] == ["42"]
    assert json.loads(status.read_text()) == {
        "queue": 1,
        "scanned": 1,
        "remaining": 0,
        "searched": 1,
    }


def test_failed_search_is_left_unscanned_for_retry(tmp_path, film, monkeypatch):
    snapshot = tmp_path / "missing.json"
    snapshot.write_text(json.dumps({"films": [film.to_dict()]}) + "\n")
    queue = tmp_path / "queue.json"
    scan = tmp_path / "scan.json"
    status = tmp_path / "status.json"
    # Two aliases, two hostnames. Each hostname fails immediately.
    session = FakeSession([requests.Timeout()] * 4)
    session.headers = {}
    monkeypatch.setattr(prefetch_sources, "login", lambda *_: session)
    monkeypatch.setenv("PREHRAJTO_EMAIL", "filmy.prehrajto@post.cz")
    monkeypatch.setenv("PREHRAJTO_PASSWORD", "secret")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prefetch_sources.py",
            "--snapshot",
            str(snapshot),
            "--out",
            str(queue),
            "--scan-state",
            str(scan),
            "--status-out",
            str(status),
            "--uploaded-index",
            str(tmp_path / "account-index.json"),
            "--request-retries",
            "0",
            "--delay",
            "0",
        ],
    )

    prefetch_sources.main()

    assert json.loads(queue.read_text()) == {}
    assert json.loads(scan.read_text())["film_ids"] == []
    assert json.loads(status.read_text())["remaining"] == 1
