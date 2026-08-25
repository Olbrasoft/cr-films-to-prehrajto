import json
import subprocess
from dataclasses import replace

from cr_films_to_prehrajto.cli import execute_production_incrementally
from cr_films_to_prehrajto.models import AccountVideo
from cr_films_to_prehrajto.production import GitStatePusher, verify_pending_uploads


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
    assert counts == {"active": 1, "pending": 0, "failed": 0}
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
    assert counts == {"active": 1, "pending": 0, "failed": 0}


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
    assert counts == {"active": 0, "pending": 0, "failed": 1}
    assert "upload" not in film
    assert film["attempts"][-1]["permanent"] is True
    assert film["attempts"][-1]["source_id"] == "source-1"


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
