import pytest
import requests

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


def test_vtt_conversion_supports_short_timestamps():
    content = b"WEBVTT\n\n00:01.000 --> 00:03.500 align:center\nHello\n"
    converted = vtt_to_srt(content)
    assert b"00:00:01,000 --> 00:00:03,500" in converted
    assert b"Hello" in converted
