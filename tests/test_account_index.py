import gzip
import json

from cr_films_to_prehrajto.account_index import (
    build_index,
    build_missing_backlog,
    inventory_from_index,
    main as account_index_main,
    reconcile_deleted_index,
    reconcile_live_index,
)
from cr_films_to_prehrajto.cli import load_historical


def test_backlog_cli_reads_compressed_full_catalog(tmp_path):
    snapshot = tmp_path / "catalog.json.gz"
    index = tmp_path / "index.json"
    output = tmp_path / "missing.json"
    with gzip.open(snapshot, "wt", encoding="utf-8") as target:
        json.dump(
            {
                "snapshot_id": "snapshot",
                "films": [
                    {"cr_film_id": 1, "added_at": "2026-01-01"},
                    {"cr_film_id": 2, "added_at": "2026-02-01"},
                ],
            },
            target,
        )
    index.write_text(json.dumps({"films": {"1": {"target_video_id": "100"}}}))

    assert account_index_main(
        [
            "backlog",
            "--snapshot",
            str(snapshot),
            "--index",
            str(index),
            "--out",
            str(output),
        ]
    ) == 0
    assert [row["cr_film_id"] for row in json.loads(output.read_text())["films"]] == [
        2
    ]


def test_index_merges_historical_and_pilot_state(tmp_path):
    historical = tmp_path / "historical.json"
    historical.write_text(
        json.dumps(
            {
                "uploads": [
                    {
                        "cr_film_id": 1,
                        "prehrajto_video_id": 100,
                        "display_name": "Old film",
                        "uploaded_at": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        )
    )
    pilot = tmp_path / "pilot.json"
    pilot.write_text(
        json.dumps(
            {
                "films": {
                    "2": {
                        "upload": {
                            "target_video_id": "200",
                            "display_name": "New film",
                            "uploaded_at": "2026-02-01T00:00:00Z",
                        }
                    },
                    "3": {
                        "upload": {
                            "target_video_id": "300",
                            "display_name": "Still processing",
                            "processing_status": "pending",
                        }
                    },
                }
            }
        )
    )
    index = build_index([historical], pilot)
    assert index["film_count"] == 2
    assert index["films"]["2"]["target_video_id"] == "200"
    assert len(inventory_from_index(index)["videos"]) == 2


def test_missing_backlog_is_newest_first():
    snapshot = {
        "snapshot_id": "snapshot",
        "films": [
            {"cr_film_id": 1, "added_at": "2026-01-01T00:00:00Z"},
            {"cr_film_id": 2, "added_at": "2026-03-01T00:00:00Z"},
            {"cr_film_id": 3, "added_at": "2026-02-01T00:00:00Z"},
        ],
    }
    index = {"films": {"3": {"target_video_id": "300"}}}
    backlog = build_missing_backlog(snapshot, index)
    assert [row["cr_film_id"] for row in backlog["films"]] == [2, 1]


def test_missing_backlog_queues_only_canonical_exact_duplicate():
    duplicate = {
        "title": "Super Mario Bros. ve filmu",
        "original_title": "The Super Mario Bros. Movie",
        "year": 2023,
    }
    snapshot = {
        "films": [
            {
                **duplicate,
                "cr_film_id": 10557,
                "added_at": "2023-01-01",
                "sources": [{"url": "old"}],
            },
            {
                **duplicate,
                "cr_film_id": 17083,
                "added_at": "2023-02-01",
                "tmdb_id": 502356,
                "runtime_min": 93,
                "sources": [{"url": "one"}, {"url": "two"}],
            },
        ]
    }

    backlog = build_missing_backlog(snapshot, {"films": {}})

    assert [row["cr_film_id"] for row in backlog["films"]] == [17083]


def test_missing_backlog_treats_uploaded_exact_duplicate_as_covered():
    snapshot = {
        "films": [
            {
                "cr_film_id": 1,
                "title": "Film",
                "original_title": "Movie",
                "year": 2024,
            },
            {
                "cr_film_id": 2,
                "title": "Film",
                "original_title": "Movie",
                "year": 2024,
            },
            {
                "cr_film_id": 3,
                "title": "Film",
                "original_title": "Different Movie",
                "year": 2024,
            },
            {
                "cr_film_id": 4,
                "title": "Film",
                "original_title": "Movie",
                "year": 2023,
            },
        ]
    }

    backlog = build_missing_backlog(
        snapshot, {"films": {"1": {"target_video_id": "100"}}}
    )

    assert {row["cr_film_id"] for row in backlog["films"]} == {3, 4}


def test_account_index_is_accepted_as_historical_state(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(
        json.dumps(
            {
                "target_account": "filmy.prehrajto@post.cz",
                "films": {"42": {"target_video_id": "123"}},
            }
        )
    )
    assert load_historical([path]) == {42: "123"}


def test_production_state_is_accepted_as_historical_state(tmp_path):
    path = tmp_path / "production.json"
    path.write_text(
        json.dumps(
            {
                "films": {
                    "42": {"upload": {"target_video_id": "456"}},
                    "43": {"attempts": []},
                }
            }
        )
    )
    assert load_historical([path]) == {42: "456"}


def test_live_reconciliation_excludes_deleted_and_matches_manual_uploads():
    snapshot = {
        "films": [
            {
                "cr_film_id": 1,
                "slug": "deleted",
                "title": "Deleted Film",
                "original_title": None,
                "year": 2020,
                "runtime_min": 90,
                "original_language": "cs",
                "description": "",
            },
            {
                "cr_film_id": 2,
                "slug": "manual",
                "title": "Manual Film",
                "original_title": None,
                "year": 2024,
                "runtime_min": 100,
                "original_language": "cs",
                "description": "",
            },
        ]
    }
    index = {
        "films": {
            "1": {"target_video_id": "100", "display_name": "Deleted Film"}
        }
    }
    reconciled = reconcile_live_index(
        snapshot,
        index,
        {"videos": [{"video_id": "200", "name": "Manual Film (2024) CZ"}]},
        {"videos": [{"video_id": "100", "name": "Deleted Film (2020) CZ"}]},
    )
    assert set(reconciled["films"]) == {"2"}
    assert reconciled["films"]["2"]["target_video_id"] == "200"
    assert reconciled["inactive_films"]["1"]["status"] == "deleted"


def test_incremental_deleted_reconciliation_reactivates_only_deleted_films():
    index = {
        "schema_version": 1,
        "target_account": "filmy.prehrajto@post.cz",
        "films": {
            "1": {"target_video_id": "100", "display_name": "Deleted"},
            "2": {"target_video_id": "200", "display_name": "Active"},
        },
        "inactive_films": {
            "3": {"target_video_id": "300", "status": "absent"}
        },
        "live_inventory": {"active_video_count": 2, "deleted_video_count": 1},
    }

    reconciled = reconcile_deleted_index(
        index,
        {
            "videos": [
                {"video_id": "100", "name": "Deleted"},
                {"video_id": "300", "name": "Previously absent"},
                {"video_id": "999", "name": "Not a catalog film"},
            ]
        },
    )

    assert set(reconciled["films"]) == {"2"}
    assert reconciled["inactive_films"]["1"]["status"] == "deleted"
    assert reconciled["inactive_films"]["3"]["status"] == "deleted"
    assert reconciled["newly_deleted_film_count"] == 1
    assert reconciled["live_inventory"]["deleted_video_count"] == 3


def test_live_reconciliation_rejects_related_title_false_positive():
    snapshot = {
        "films": [
            {
                "cr_film_id": 1,
                "slug": "batman",
                "title": "Batman",
                "original_title": None,
                "year": 2022,
                "runtime_min": 120,
                "original_language": "en",
                "description": "",
            }
        ]
    }
    reconciled = reconcile_live_index(
        snapshot,
        {
            "films": {
                "1": {
                    "target_video_id": "300",
                    "display_name": "incorrect historical mapping",
                }
            }
        },
        {
            "videos": [
                {
                    "video_id": "300",
                    "name": "Batman: Duše draka (2021) CZ Dabing",
                }
            ]
        },
        {"videos": []},
    )
    assert reconciled["films"] == {}
    assert reconciled["inactive_films"]["1"]["status"] == "identity_mismatch"
