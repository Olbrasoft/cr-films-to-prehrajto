from dataclasses import replace

import pytest
import requests

from cr_films_to_prehrajto.models import (
    AccountVideo,
    Candidate,
    LanguageTier,
    MatchTier,
    Subtitle,
)
from cr_films_to_prehrajto.pipeline import HybridPipeline, validate_limit
from cr_films_to_prehrajto.providers.prehrajto import ProviderError
from cr_films_to_prehrajto.providers.sktorrent import SkTorrentProvider
from cr_films_to_prehrajto.state import StateStore
from cr_films_to_prehrajto.transfer import TransferError


def acceptable(provider, source_id):
    return Candidate(
        provider=provider,
        source_id=source_id,
        url="https://example/detail",
        title="Pelíšky 1999 CZ",
        language_tier=LanguageTier.CZECH_AUDIO,
        resolution=720,
        stream_url="https://example/movie.mp4?token=secret",
        match_tier=MatchTier.SOLID,
    )


class Provider:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def discover(self, film):
        self.calls += 1
        return list(self.rows)


class FailingProvider:
    def discover(self, film):
        raise ProviderError("Proxy HTTP 429")


class Transfer:
    def __init__(self, fail_first=False):
        self.calls = []
        self.fail_first = fail_first

    def transfer(self, film, candidate):
        self.calls.append(candidate.source_id)
        if self.fail_first and len(self.calls) == 1:
            raise TransferError("dead", permanent=True)
        return {
            "target_video_id": "555",
            "display_name": "Pelíšky (1999) CZ",
            "size_bytes": 10_000_000,
        }


def test_limit_cannot_exceed_ten():
    assert validate_limit(10) == 10
    with pytest.raises(ValueError):
        validate_limit(11)
    with pytest.raises(ValueError):
        validate_limit(0)


def test_production_limit_can_be_twenty_without_changing_pilot_default():
    assert validate_limit(20, 20) == 20
    with pytest.raises(ValueError):
        validate_limit(21, 20)


def test_prehrajto_wins_and_sk_is_not_called(tmp_path, film):
    state = StateStore(tmp_path / "state.json")
    prehraj = Provider([acceptable("prehrajto", "p1")])
    sk = Provider([acceptable("sktorrent", "s1")])
    pipeline = HybridPipeline(
        prehrajto=prehraj, sktorrent=sk, inventory=[], state=state
    )
    plan = pipeline.build_plan([film], 1)
    assert plan[0]["selected"]["provider"] == "prehrajto"
    assert sk.calls == 0


def test_sk_is_called_only_as_fallback(tmp_path, film):
    state = StateStore(tmp_path / "state.json")
    prehraj = Provider([])
    sk = Provider([acceptable("sktorrent", "s1")])
    plan = HybridPipeline(
        prehrajto=prehraj, sktorrent=sk, inventory=[], state=state
    ).build_plan([film], 1)
    assert plan[0]["selected"]["provider"] == "sktorrent"
    assert sk.calls == 1


def test_transient_prehrajto_failure_falls_back_to_sk(tmp_path, film):
    state = StateStore(tmp_path / "state.json")
    sk = Provider([acceptable("sktorrent", "s1")])
    plan = HybridPipeline(
        prehrajto=FailingProvider(), sktorrent=sk, inventory=[], state=state
    ).build_plan([film], 1)
    assert plan[0]["selected"]["provider"] == "sktorrent"
    assert plan[0]["provider_errors"] == [
        {"provider": "prehrajto", "reason": "Proxy HTTP 429"}
    ]


def test_transient_discovery_failure_is_not_marked_exhausted(tmp_path, film):
    state = StateStore(tmp_path / "state.json")
    state.set_snapshot("snapshot-1", None)
    pipeline = HybridPipeline(
        prehrajto=FailingProvider(),
        sktorrent=Provider([]),
        inventory=[],
        state=state,
        discovery_version="v1",
    )
    assert pipeline.build_plan(
        [film], 1, maximum=20, skip_exhausted_snapshot=True
    ) == []
    assert not state.discovery_exhausted_for_snapshot(film.cr_film_id, "v1")


def test_sk_search_decode_failure_returns_no_candidates(film):
    class BrokenSession:
        def get(self, *args, **kwargs):
            raise requests.exceptions.ContentDecodingError("broken gzip")

    provider = SkTorrentProvider(BrokenSession())
    assert provider.discover(film) == []


def test_pilot_limit_counts_only_films_with_sources(tmp_path, film):
    first = replace(film, added_at="2026-02-01T00:00:00Z")
    second = replace(
        film,
        cr_film_id=43,
        slug="second",
        title="Second",
        added_at="2026-01-01T00:00:00Z",
    )

    class ConditionalProvider:
        def discover(self, current):
            return [] if current.cr_film_id == 42 else [acceptable("sktorrent", "s1")]

    plan = HybridPipeline(
        prehrajto=Provider([]),
        sktorrent=ConditionalProvider(),
        inventory=[],
        state=StateStore(tmp_path / "state.json"),
    ).build_plan([first, second], 1)
    assert len(plan) == 1
    assert plan[0]["film"]["cr_film_id"] == 43


def test_one_source_failure_advances_to_next(tmp_path, film):
    state = StateStore(tmp_path / "state.json")
    prehraj = Provider([acceptable("prehrajto", "p1"), acceptable("prehrajto", "p2")])
    transfer = Transfer(fail_first=True)
    pipeline = HybridPipeline(
        prehrajto=prehraj,
        sktorrent=Provider([]),
        inventory=[],
        state=state,
        transfer=transfer,
    )
    plan = pipeline.build_plan([film], 1)
    pipeline.execute(plan)
    assert transfer.calls == ["p1", "p2"]
    assert state.uploaded(film.cr_film_id)
    assert state.burned_source_ids(film.cr_film_id) == {"p1"}


def test_rerun_after_success_is_idempotent(tmp_path, film):
    state = StateStore(tmp_path / "state.json")
    state.record_success(film.cr_film_id, {"target_video_id": "555"})
    prehraj = Provider([acceptable("prehrajto", "p1")])
    plan = HybridPipeline(
        prehrajto=prehraj, sktorrent=Provider([]), inventory=[], state=state
    ).build_plan([film], 1)
    assert plan == []
    assert prehraj.calls == 0


def test_production_upload_stays_pending_until_remote_verification(tmp_path, film):
    state = StateStore(tmp_path / "state.json")
    pipeline = HybridPipeline(
        prehrajto=Provider([acceptable("prehrajto", "p1")]),
        sktorrent=Provider([]),
        inventory=[],
        state=state,
        transfer=Transfer(),
        defer_processing_verification=True,
    )
    pipeline.execute(pipeline.build_plan([film], 1, maximum=20))
    assert state.film(film.cr_film_id)["upload"]["processing_status"] == "pending"


def test_state_is_valid_after_every_attempt(tmp_path, film):
    path = tmp_path / "state.json"
    state = StateStore(path)
    state.record_attempt(film.cr_film_id, {"source_id": "p1", "permanent": False})
    reloaded = StateStore(path)
    assert reloaded.film(film.cr_film_id)["attempts"][0]["source_id"] == "p1"


def test_attempt_persists_to_remote_callback_with_snapshot(tmp_path, film):
    persisted = []
    state = StateStore(tmp_path / "state.json", on_persist=persisted.append)
    state.set_snapshot("snapshot-1", None)
    state.record_reconciliation(film.cr_film_id, "missing", {})
    assert persisted == []
    state.record_attempt(
        film.cr_film_id,
        {"status": "no_acceptable_source", "discovery_exhausted": True},
    )
    assert persisted == [state.path]
    assert state.discovery_exhausted_for_snapshot(film.cr_film_id)


def test_production_skips_discovery_exhausted_for_current_snapshot(tmp_path, film):
    state = StateStore(tmp_path / "state.json")
    state.set_snapshot("snapshot-1", None)
    state.record_attempt(
        film.cr_film_id,
        {"status": "no_acceptable_source", "discovery_exhausted": True},
    )
    prehraj = Provider([acceptable("prehrajto", "p1")])
    plan = HybridPipeline(
        prehrajto=prehraj,
        sktorrent=Provider([]),
        inventory=[],
        state=state,
    ).build_plan([film], 1, maximum=20, skip_exhausted_snapshot=True)
    assert plan == []
    assert prehraj.calls == 0


def test_new_discovery_version_retries_previous_exhaustion(tmp_path, film):
    state = StateStore(tmp_path / "state.json")
    state.set_snapshot("snapshot-1", None)
    state.record_attempt(
        film.cr_film_id,
        {
            "status": "no_acceptable_source",
            "discovery_exhausted": True,
            "discovery_version": "old",
        },
    )
    prehraj = Provider([acceptable("prehrajto", "p1")])
    plan = HybridPipeline(
        prehrajto=prehraj,
        sktorrent=Provider([]),
        inventory=[],
        state=state,
        discovery_version="new",
    ).build_plan([film], 1, maximum=20, skip_exhausted_snapshot=True)
    assert len(plan) == 1
    assert prehraj.calls == 1


def test_partial_subtitle_upload_is_repaired_without_second_video(tmp_path, film):
    state = StateStore(tmp_path / "state.json")
    state.record_attempt(
        film.cr_film_id,
        {
            "provider": "prehrajto",
            "source_id": "p1",
            "status": "failed",
            "partial_target_video_id": "777",
            "permanent": False,
        },
    )
    candidate = acceptable("prehrajto", "p1")
    candidate.language_tier = LanguageTier.CZECH_SUBTITLES
    candidate.subtitles = [Subtitle("cs", "https://example/sub.vtt")]

    class RepairTransfer:
        def __init__(self):
            self.repaired = []

        def repair_subtitle(self, film, selected, video_id):
            self.repaired.append((selected.source_id, video_id))
            return {
                "target_video_id": video_id,
                "display_name": "Pelíšky",
                "size_bytes": None,
            }

        def transfer(self, film, selected):
            raise AssertionError("video must not be uploaded twice")

    transfer = RepairTransfer()
    pipeline = HybridPipeline(
        prehrajto=Provider([candidate]),
        sktorrent=Provider([]),
        inventory=[AccountVideo("777", "Pelíšky (1999) CZ titulky")],
        state=state,
        transfer=transfer,
    )
    plan = pipeline.build_plan([film], 1)
    pipeline.execute(plan)
    assert transfer.repaired == [("p1", "777")]
    assert state.uploaded(film.cr_film_id)
