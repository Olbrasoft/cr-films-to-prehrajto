from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from .matching import YEAR_RE, normalize_title
from .models import AccountVideo, Film

TARGET_ACCOUNT = "filmy.prehrajto@post.cz"
ACCOUNT_STATUS_NOISE_RE = re.compile(
    r"\.(?:mp4|mkv|avi|webm)\b|\(\s*zpracovává se\s*\)", re.IGNORECASE
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def build_index(historical_paths: list[Path], pilot_path: Path | None) -> dict:
    films: dict[str, dict] = {}
    inactive_films: dict[str, dict] = {}
    live_inventory = None
    for path in historical_paths:
        payload = _load(path)
        if payload.get("target_account") == TARGET_ACCOUNT and isinstance(
            payload.get("films"), dict
        ):
            films.update(payload["films"])
            inactive_films.update(payload.get("inactive_films", {}))
            live_inventory = payload.get("live_inventory") or live_inventory
            continue
        for upload in payload.get("uploads", []):
            film_id = upload.get("cr_film_id")
            video_id = upload.get("prehrajto_video_id") or upload.get("target_video_id")
            if film_id is None or video_id is None:
                continue
            row = {
                "target_video_id": str(video_id),
                "display_name": upload.get("display_name")
                or upload.get("title")
                or "",
                "uploaded_at": upload.get("uploaded_at"),
                "source": "prehrajto-to-prehrajto",
            }
            key = str(int(film_id))
            if key not in films or (row["uploaded_at"] or "") >= (
                films[key].get("uploaded_at") or ""
            ):
                films[key] = row

    if pilot_path and pilot_path.exists():
        for film_id, state in _load(pilot_path).get("films", {}).items():
            upload = state.get("upload") or {}
            if not upload.get("target_video_id"):
                continue
            if upload.get("processing_status") == "pending":
                continue
            films[str(int(film_id))] = {
                "target_video_id": str(upload["target_video_id"]),
                "display_name": upload.get("display_name") or "",
                "uploaded_at": upload.get("uploaded_at"),
                "source": "cr-films-to-prehrajto",
            }
            inactive_films.pop(str(int(film_id)), None)

    payload = {
        "schema_version": 1,
        "target_account": TARGET_ACCOUNT,
        "generated_at": datetime.now(UTC).isoformat(),
        "film_count": len(films),
        "films": dict(sorted(films.items(), key=lambda item: int(item[0]))),
        "inactive_film_count": len(inactive_films),
        "inactive_films": dict(
            sorted(inactive_films.items(), key=lambda item: int(item[0]))
        ),
    }
    if live_inventory:
        payload["live_inventory"] = live_inventory
    return payload


def reconcile_live_index(
    snapshot: dict,
    index: dict,
    inventory: dict,
    deleted_inventory: dict,
) -> dict:
    active = [AccountVideo(**row) for row in inventory.get("videos", [])]
    active_by_id = {str(video.video_id): video for video in active}
    deleted_ids = {
        str(row["video_id"]) for row in deleted_inventory.get("videos", [])
    }
    catalog_films = {
        str(row["cr_film_id"]): Film.from_dict(row)
        for row in snapshot.get("films", [])
    }

    def is_exact_account_match(film: Film, video: AccountVideo) -> bool:
        aliases = {
            normalize_title(alias)
            for alias in (film.title, film.original_title)
            if alias
        }
        clean_name = ACCOUNT_STATUS_NOISE_RE.sub(" ", video.name)
        match = YEAR_RE.search(clean_name)
        candidate_year = int(match.group(1)) if match else None
        base_name = normalize_title(YEAR_RE.sub(" ", clean_name))
        year_matches = (
            not film.year
            or not candidate_year
            or film.year == candidate_year
        )
        return base_name in aliases and year_matches

    films = {}
    inactive_films = {}
    for film_id, row in index.get("films", {}).items():
        if str(row.get("source", "")).startswith("live-account-"):
            continue
        target_video_id = str(row["target_video_id"])
        if target_video_id in active_by_id:
            film = catalog_films.get(str(int(film_id)))
            if film is None or is_exact_account_match(
                film, active_by_id[target_video_id]
            ):
                films[str(int(film_id))] = row
            else:
                inactive_films[str(int(film_id))] = {
                    **row,
                    "status": "identity_mismatch",
                    "live_display_name": active_by_id[target_video_id].name,
                }
        else:
            inactive_films[str(int(film_id))] = {
                **row,
                "status": "deleted" if target_video_id in deleted_ids else "absent",
            }
    for film_id, row in index.get("inactive_films", {}).items():
        target_video_id = str(row["target_video_id"])
        if target_video_id in active_by_id:
            film = catalog_films.get(str(int(film_id)))
            if film is None or is_exact_account_match(
                film, active_by_id[target_video_id]
            ):
                restored = {
                    key: value
                    for key, value in row.items()
                    if key not in {"status", "live_display_name"}
                }
                films[str(int(film_id))] = restored
            else:
                inactive_films[str(int(film_id))] = {
                    **row,
                    "status": "identity_mismatch",
                    "live_display_name": active_by_id[target_video_id].name,
                }
        else:
            inactive_films[str(int(film_id))] = {
                **row,
                "status": "deleted" if target_video_id in deleted_ids else "absent",
            }

    inventory_by_token = defaultdict(list)
    for video in active:
        for token in set(normalize_title(video.name).split()):
            inventory_by_token[token].append(video)

    def plausible_inventory(film: Film) -> list[AccountVideo]:
        plausible = {}
        for alias in (film.title, film.original_title):
            tokens = normalize_title(alias or "").split()
            if not tokens:
                continue
            rarest = min(tokens, key=lambda token: len(inventory_by_token[token]))
            for video in inventory_by_token[rarest]:
                plausible[video.video_id] = video
        return list(plausible.values())

    for row in snapshot.get("films", []):
        film = Film.from_dict(row)
        key = str(film.cr_film_id)
        if key in films:
            continue
        candidates = plausible_inventory(film)
        exact = []
        for video in candidates:
            if is_exact_account_match(film, video):
                exact.append(video)
        if exact:
            selected = max(exact, key=lambda video: int(video.video_id))
            films[key] = {
                "target_video_id": selected.video_id,
                "display_name": selected.name,
                "uploaded_at": None,
                "source": "live-account-exact-title-year",
            }
            inactive_films.pop(key, None)

    return {
        "schema_version": 1,
        "target_account": TARGET_ACCOUNT,
        "generated_at": datetime.now(UTC).isoformat(),
        "film_count": len(films),
        "films": dict(sorted(films.items(), key=lambda item: int(item[0]))),
        "inactive_film_count": len(inactive_films),
        "inactive_films": dict(
            sorted(inactive_films.items(), key=lambda item: int(item[0]))
        ),
        "live_inventory": {
            "active_video_count": len(active_by_id),
            "deleted_video_count": len(deleted_ids),
            "reconciled_at": datetime.now(UTC).isoformat(),
        },
    }


def inventory_from_index(index: dict) -> dict:
    return {
        "videos": [
            {
                "video_id": row["target_video_id"],
                "name": row["display_name"],
                "url": None,
            }
            for row in index["films"].values()
        ]
    }


def build_missing_backlog(snapshot: dict, index: dict) -> dict:
    uploaded = {int(film_id) for film_id in index["films"]}
    films = [
        row
        for row in snapshot.get("films", [])
        if int(row["cr_film_id"]) not in uploaded
    ]
    films.sort(
        key=lambda row: (
            row.get("added_at") or row.get("created_at") or "",
            int(row["cr_film_id"]),
        ),
        reverse=True,
    )
    return {
        "schema_version": 1,
        "snapshot_id": snapshot.get("snapshot_id"),
        "generated_at": datetime.now(UTC).isoformat(),
        "film_count": len(films),
        "films": films,
    }


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index")
    index_parser.add_argument(
        "--historical-state", action="append", type=Path, default=[]
    )
    index_parser.add_argument("--pilot-state", type=Path)
    index_parser.add_argument("--out", type=Path, required=True)

    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--index", type=Path, required=True)
    inventory_parser.add_argument("--out", type=Path, required=True)

    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("--snapshot", type=Path, required=True)
    reconcile_parser.add_argument("--index", type=Path, required=True)
    reconcile_parser.add_argument("--inventory", type=Path, required=True)
    reconcile_parser.add_argument("--deleted-inventory", type=Path, required=True)
    reconcile_parser.add_argument("--out", type=Path, required=True)

    backlog_parser = subparsers.add_parser("backlog")
    backlog_parser.add_argument("--snapshot", type=Path, required=True)
    backlog_parser.add_argument("--index", type=Path, required=True)
    backlog_parser.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "index":
        payload = build_index(args.historical_state, args.pilot_state)
    elif args.command == "inventory":
        payload = inventory_from_index(_load(args.index))
    elif args.command == "reconcile":
        payload = reconcile_live_index(
            _load(args.snapshot),
            _load(args.index),
            _load(args.inventory),
            _load(args.deleted_inventory),
        )
    else:
        payload = build_missing_backlog(_load(args.snapshot), _load(args.index))
    _write(args.out, payload)
    count = payload.get("film_count", len(payload.get("videos", [])))
    print(f"Wrote {args.command} with {count} films")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
