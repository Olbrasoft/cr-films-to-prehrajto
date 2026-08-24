import json

from cr_films_to_prehrajto.account_index import (
    build_index,
    build_missing_backlog,
    inventory_from_index,
)
from cr_films_to_prehrajto.cli import load_historical


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
                    }
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
