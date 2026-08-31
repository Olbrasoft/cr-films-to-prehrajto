import json
import subprocess
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cr_films_to_prehrajto.cli import build_parser, execute_production_incrementally
from cr_films_to_prehrajto.models import AccountVideo
from cr_films_to_prehrajto.production import (
    GitStatePusher,
    collect_target_video_ids,
    invalidate_deleted_uploads,
    verify_pending_uploads,
)


def write_pending_state(path, film_id="42", video_id="123"):
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot": {"id": "snapshot-1"},
                "films": {
                    film_id: {
                        "attempts": [],
                        "upload": {
                            "target_video_id": video_id,
                            "display_name": "Film (2024) CZ",
                            "provider": "sktorrent",
                            "source_id": "source-1",
                            "processing_status": "pending",
                        },
                    }
                },
            }
        )
    )


def test_pending_production_upload_becomes_active(tmp_path):
    path = tmp_path / "state.json"
    write_pending_state(path)
    counts = verify_pending_uploads(
        [path],
        lambda _name, video_id: (
            "active",
            AccountVideo(video_id, "Film (2024) CZ"),
        ),
    )
    payload = json.loads(path.read_text())
    assert counts == {
        "active": 1,
        "pending": 0,
        "recent_pending": 0,
        "failed": 0,
    }
    assert payload["films"]["42"]["upload"]["processing_status"] == "active"


def test_pending_czech_subtitle_is_verified_without_blocking_upload(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot": {"id": "snapshot-1"},
                "films": {
                    "42": {
                        "attempts": [],
                        "upload": {
                            "target_video_id": "777",
                            "display_name": "Film (2024) CZ titulky",
                            "processing_status": "pending",
                            "language_tier": "czech_subtitles",
                            "subtitle_verification": "pending",
                        },
                    }
                },
            }
        )
    )
    counts = verify_pending_uploads(
        [path],
        lambda _name, video_id: ("active", AccountVideo(video_id, "Film")),
        subtitle_lookup=lambda _name, _video_id: True,
    )
    saved = json.loads(path.read_text())["films"]["42"]["upload"]
    assert saved["processing_status"] == "active"
    assert saved["subtitle_verification"] == "verified"
    assert counts == {
        "active": 1,
        "pending": 0,
        "recent_pending": 0,
        "failed": 0,
    }


def test_only_recent_processing_uploads_keep_production_running(tmp_path):
    recent_path = tmp_path / "recent.json"
    old_path = tmp_path / "old.json"
    write_pending_state(recent_path, film_id="1", video_id="101")
    write_pending_state(old_path, film_id="2", video_id="102")
    recent = json.loads(recent_path.read_text())
    old = json.loads(old_path.read_text())
    recent["films"]["1"]["upload"]["uploaded_at"] = datetime.now(UTC).isoformat()
    old["films"]["2"]["upload"]["uploaded_at"] = (
        datetime.now(UTC) - timedelta(days=1)
    ).isoformat()
    recent_path.write_text(json.dumps(recent))
    old_path.write_text(json.dumps(old))

    counts = verify_pending_uploads(
        [recent_path, old_path], lambda _name, _video_id: ("pending", None)
    )

    assert counts == {
        "active": 0,
        "pending": 2,
        "recent_pending": 1,
        "failed": 0,
    }


def test_deleted_production_upload_returns_to_retryable_state(tmp_path):
    path = tmp_path / "state.json"
    write_pending_state(path)
    counts = verify_pending_uploads(
        [path],
        lambda _name, video_id: (
            "deleted",
            AccountVideo(video_id, "Film (2024) CZ"),
        ),
    )
    payload = json.loads(path.read_text())
    film = payload["films"]["42"]
    assert counts == {
        "active": 0,
        "pending": 0,
        "recent_pending": 0,
        "failed": 1,
    }
    assert "upload" not in film
    assert film["attempts"][-1]["permanent"] is True
    assert film["attempts"][-1]["source_id"] == "source-1"


def test_pending_verification_can_run_lookups_concurrently(tmp_path):
    path = tmp_path / "state.json"
    payload = {
        "schema_version": 1,
        "snapshot": {"id": "snapshot-1"},
        "films": {},
    }
    for film_id in range(8):
        payload["films"][str(film_id)] = {
            "attempts": [],
            "upload": {
                "target_video_id": str(100 + film_id),
                "display_name": f"Film {film_id}",
                "processing_status": "pending",
            },
        }
    path.write_text(json.dumps(payload))
    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def lookup(_name, video_id):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return "active", AccountVideo(video_id, "Film")

    counts = verify_pending_uploads([path], lookup, workers=4)

    assert maximum_active == 4
    assert counts == {
        "active": 8,
        "pending": 0,
        "recent_pending": 0,
        "failed": 0,
    }


def test_stale_pending_verification_is_bounded_and_rotates(tmp_path):
    path = tmp_path / "state.json"
    old_upload = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    payload = {
        "schema_version": 1,
        "snapshot": {"id": "snapshot-1"},
        "films": {
            str(film_id): {
                "attempts": [],
                "upload": {
                    "target_video_id": str(100 + film_id),
                    "display_name": f"Film {film_id}",
                    "processing_status": "pending",
                    "uploaded_at": old_upload,
                },
            }
            for film_id in range(10)
        },
    }
    path.write_text(json.dumps(payload))
    checked: list[str] = []

    def lookup(_name, video_id):
        checked.append(video_id)
        return "pending", None

    verify_pending_uploads([path], lookup, stale_limit=4)
    first_batch = set(checked)
    verify_pending_uploads([path], lookup, stale_limit=4)
    second_batch = set(checked[4:])

    assert len(checked) == 8
    assert len(first_batch) == 4
    assert len(second_batch) == 4
    assert first_batch.isdisjoint(second_batch)


def test_git_state_pusher_commits_and_pushes_only_its_state(tmp_path):
    state_path = tmp_path / "production-shard-0.json"
    calls = []

    def fake_run(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[:3] == ("diff", "--cached", "--quiet") else 0)

    pusher = GitStatePusher(state_path, 0)
    pusher._run = fake_run
    pusher(state_path)
    assert calls[0] == ("add", "--", str(state_path))
    assert calls[2][:2] == ("commit", "-m")
    assert calls[3] == ("push", "origin", "HEAD:main")


def test_git_state_pusher_staggers_retry_by_shard(tmp_path, monkeypatch):
    state_path = tmp_path / "production-shard-3.json"
    push_results = iter((1, 1, 0))
    calls = []
    delays = []

    def fake_run(*args):
        calls.append(args)
        if args[:2] == ("push", "origin"):
            return subprocess.CompletedProcess(args, next(push_results))
        if args[:3] == ("diff", "--cached", "--quiet"):
            return subprocess.CompletedProcess(args, 1)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(
        "cr_films_to_prehrajto.production.time.sleep", delays.append
    )
    pusher = GitStatePusher(state_path, 3, retries=3)
    pusher._run = fake_run

    pusher(state_path)

    assert calls.count(("push", "origin", "HEAD:main")) == 3
    assert calls.count(("pull", "--rebase", "origin", "main")) == 2
    assert delays == [2.5, 3.5]


def test_production_workflow_continues_after_partial_shard_failure():
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/production.yml"
    ).read_text()

    assert "needs.upload.result != 'cancelled'" in workflow
    assert "needs.upload.result == 'success'" not in workflow
    assert "for attempt in $(seq 1 30)" in workflow


def test_target_video_count_is_monotonic_across_upload_state_transitions(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(
        json.dumps(
            {
                "films": {
                    "1": {
                        "upload": {"target_video_id": "101"},
                        "attempts": [
                            {"partial_target_video_id": "100"},
                            {"deleted_target_video_id": "99"},
                        ],
                    }
                }
            }
        )
    )
    second.write_text(
        json.dumps(
            {
                "films": {
                    "2": {
                        "attempts": [
                            {"partial_target_video_id": 102},
                            {"deleted_target_video_id": "101"},
                        ]
                    }
                }
            }
        )
    )

    assert collect_target_video_ids([first, second]) == {"99", "100", "101", "102"}


def test_deleted_active_production_upload_returns_to_retryable_state(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot": {"id": "snapshot-1"},
                "films": {
                    "1": {
                        "attempts": [],
                        "upload": {
                            "target_video_id": "100",
                            "provider": "prehrajto",
                            "source_id": "source-1",
                            "processing_status": "active",
                        },
                    },
                    "2": {
                        "attempts": [],
                        "upload": {"target_video_id": "200"},
                    },
                },
            }
        )
    )

    assert invalidate_deleted_uploads([state_path], {"100", "999"}) == 1
    payload = json.loads(state_path.read_text())
    deleted = payload["films"]["1"]
    assert "upload" not in deleted
    assert deleted["attempts"][-1]["deleted_target_video_id"] == "100"
    assert deleted["attempts"][-1]["discovery_exhausted"] is False
    assert payload["films"]["2"]["upload"]["target_video_id"] == "200"


def test_production_workflow_stops_without_new_uploads_after_full_prefetch():
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/production.yml"
    ).read_text()

    assert "count-production-targets" in workflow
    assert "fromJSON(steps.progress.outputs.new_uploads) > 0" in workflow
    assert "fromJSON(needs.prefetch.outputs.remaining) > 0" in workflow
    assert "fromJSON(needs.prefetch.outputs.newly_queued || '0') > 0" in workflow
    assert "newly_queued_total=$((newly_queued_total + newly_queued))" in workflow
    assert "fromJSON(steps.deleted.outputs.newly_deleted_films || '0') > 0" in workflow
    assert "inventory-account" in workflow
    assert "reconcile-deleted" in workflow
    assert workflow.count("needs.upload.result != 'cancelled'") >= 4
    assert "steps.verification.outputs.recent_pending" not in workflow
    assert "steps.backlog.outputs.after) <" not in workflow


def test_production_discovers_and_executes_each_film_incrementally(tmp_path, film):
    second = replace(film, cr_film_id=43, slug="second", title="Second")

    class Pipeline:
        def __init__(self):
            self.events = []

        def build_plan(self, films, limit, **kwargs):
            self.events.append(("discover", [item.cr_film_id for item in films]))
            assert limit == 1
            assert kwargs["skip_exhausted_snapshot"] is True
            if not films:
                return []
            selected = films[0]
            return [
                {
                    "film": selected.to_dict(),
                    "reconciliation": {"status": "missing"},
                    "selected": None,
                    "candidates": [],
                }
            ]

        def execute(self, plan):
            self.events.append(("upload", plan[0]["film"]["cr_film_id"]))

    pipeline = Pipeline()
    plan = execute_production_incrementally(
        pipeline,
        [film, second],
        2,
        tmp_path / "report.md",
        tmp_path / "plan.json",
        shard_id=0,
        num_shards=2,
    )

    assert pipeline.events == [
        ("discover", [42, 43]),
        ("upload", 42),
        ("discover", [43]),
        ("upload", 43),
    ]
    assert [row["film"]["cr_film_id"] for row in plan] == [42, 43]
    assert len(json.loads((tmp_path / "plan.json").read_text())["films"]) == 2


def test_production_cli_defaults_to_eight_shards():
    args = build_parser().parse_args(
        [
            "production",
            "--snapshot",
            "missing.json",
            "--inventory",
            "inventory.json",
            "--state",
            "state.json",
            "--report",
            "report.md",
            "--plan",
            "plan.json",
            "--shard-id",
            "0",
        ]
    )

    assert args.num_shards == 8
