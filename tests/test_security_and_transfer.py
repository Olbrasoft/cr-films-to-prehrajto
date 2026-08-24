from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests

from cr_films_to_prehrajto.audio import AudioEvidence, normalize_iso
from cr_films_to_prehrajto.models import Candidate, MatchTier
from cr_films_to_prehrajto.providers.prehrajto import (
    PrehrajtoProvider,
    ProviderError,
    validate_target_email,
)
from cr_films_to_prehrajto.security import redact, safe_url
from cr_films_to_prehrajto.transfer import vtt_to_srt


def test_only_target_post_account_is_allowed():
    validate_target_email("filmy.prehrajto@post.cz")
    with pytest.raises(ValueError):
        validate_target_email("filmy.prehrajto@email.cz")


def test_undefined_audio_tag_does_not_override_language_evidence():
    assert normalize_iso("und") is None
    assert normalize_iso("zxx") is None
    assert normalize_iso("mul") is None


def test_signed_urls_are_redacted():
    assert (
        safe_url("https://cdn.example/a.mp4?token=secret")
        == "https://cdn.example/a.mp4"
    )
    value = redact("failed https://x/a?signature=dummy-value password=example-value")
    assert "dummy-value" not in value and "example-value" not in value


def test_candidate_evidence_never_serializes_signed_urls():
    candidate = Candidate(
        provider="prehrajto",
        source_id="abc",
        url="https://prehraj.to/film/abc",
        title="Film 2000 CZ",
        stream_url="https://cdn.example/movie.mp4?token=dummy-value",
        match_tier=MatchTier.SOLID,
    )
    assert "stream_url" not in candidate.to_dict()


def test_proxy_exception_does_not_expose_key():
    class BrokenSession:
        def get(self, *args, **kwargs):
            raise requests.RequestException(
                "failed https://proxy.example/?key=dummy-value&url=target"
            )

    provider = PrehrajtoProvider(
        "https://proxy.example/", "dummy-value", BrokenSession()
    )
    with pytest.raises(ProviderError) as raised:
        provider._proxy_get("https://prehraj.to/film/abc")
    assert "dummy-value" not in str(raised.value)


def test_direct_source_mode_does_not_require_proxy_configuration():
    class DirectSession:
        def get(self, url, timeout):
            assert url == "https://prehraj.to/hledej/test"
            response = requests.Response()
            response.status_code = 200
            response._content = b"ok"
            return response

    provider = PrehrajtoProvider("", "", DirectSession(), allow_direct=True)
    assert provider._proxy_get("https://prehraj.to/hledej/test").text == "ok"


def test_exported_prehrajto_source_is_resolved_before_live_search(
    film, fixtures, monkeypatch
):
    source_url = "https://prehrajto.cz/pelisky-1999-czdab/abc12345"
    film = replace(
        film,
        sources=(
            {
                "provider": "prehrajto",
                "external_id": "abc12345",
                "title": "Pelíšky (1999) CZdab",
                "duration_sec": 6900,
                "audio_lang": "cs",
                "is_alive": True,
                "metadata": {"url": source_url},
            },
        ),
    )
    calls = []
    provider = PrehrajtoProvider("proxy", "key", requests.Session())
    provider._proxy_get = lambda url: (
        calls.append(url)
        or SimpleNamespace(text=(fixtures / "prehrajto_detail.html").read_text())
    )
    monkeypatch.setattr(
        "cr_films_to_prehrajto.providers.prehrajto.detect_audio_language",
        lambda *_args, **_kwargs: AudioEvidence(None, "unknown", 0.0),
    )

    candidates = provider.discover(film)

    assert calls == [source_url]
    assert candidates[0].source_id == "abc12345"
    assert candidates[0].resolution == 1080
    assert candidates[0].audio_language == "cs"


def test_catalog_source_rejects_wrong_title_even_with_stable_film_link(film):
    film = replace(
        film,
        sources=(
            {
                "provider": "prehrajto",
                "external_id": "wrong123",
                "title": "Completely Different Film (1999) CZdab",
                "duration_sec": 6900,
                "audio_lang": "cs",
                "is_alive": True,
                "metadata": {"url": "https://prehraj.to/wrong/wrong123"},
            },
        ),
    )
    candidate = PrehrajtoProvider._catalog_candidates(film)[0]
    assert candidate.match_tier == MatchTier.REJECT
    assert candidate.match_evidence["rejection_reason"] == "title_mismatch"


def test_vtt_conversion_supports_short_timestamps():
    content = b"WEBVTT\n\n00:01.000 --> 00:03.500 align:center\nHello\n"
    converted = vtt_to_srt(content)
    assert b"00:00:01,000 --> 00:00:03,500" in converted
    assert b"Hello" in converted
