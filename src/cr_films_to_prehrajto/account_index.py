from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

TARGET_ACCOUNT = "filmy.prehrajto@post.cz"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def build_index(historical_paths: list[Path], pilot_path: Path | None) -> dict:
    films: dict[str, dict] = {}
    for path in historical_paths:
        payload = _load(path)
        if payload.get("target_account") == TARGET_ACCOUNT and isinstance(
            payload.get("films"), dict
        ):
            films.update(payload["films"])
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
            films[str(int(film_id))] = {
                "target_video_id": str(upload["target_video_id"]),
                "display_name": upload.get("display_name") or "",
                "uploaded_at": upload.get("uploaded_at"),
                "source": "cr-films-to-prehrajto",
            }

    return {
        "schema_version": 1,
        "target_account": TARGET_ACCOUNT,
        "generated_at": datetime.now(UTC).isoformat(),
        "film_count": len(films),
        "films": dict(sorted(films.items(), key=lambda item: int(item[0]))),
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

    backlog_parser = subparsers.add_parser("backlog")
    backlog_parser.add_argument("--snapshot", type=Path, required=True)
    backlog_parser.add_argument("--index", type=Path, required=True)
    backlog_parser.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "index":
        payload = build_index(args.historical_state, args.pilot_state)
    elif args.command == "inventory":
        payload = inventory_from_index(_load(args.index))
    else:
        payload = build_missing_backlog(_load(args.snapshot), _load(args.index))
    _write(args.out, payload)
    count = payload.get("film_count", len(payload.get("videos", [])))
    print(f"Wrote {args.command} with {count} films")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
