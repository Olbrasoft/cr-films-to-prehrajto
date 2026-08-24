from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import requests

from .catalog import main as export_main
from .models import AccountVideo, Film
from .pipeline import HybridPipeline, validate_limit
from .providers.prehrajto import PrehrajtoProvider, inventory_account, login
from .providers.sktorrent import SkTorrentProvider
from .report import write_report
from .state import StateStore
from .transfer import TransferService


def load_historical(paths: list[Path]) -> dict[int, str]:
    mappings = {}
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text())
        if isinstance(payload.get("films"), dict):
            for film_id, upload in payload["films"].items():
                video_id = upload.get("target_video_id")
                if video_id is not None:
                    mappings[int(film_id)] = str(video_id)
            continue
        for upload in payload.get("uploads", []):
            film_id = upload.get("cr_film_id")
            video_id = upload.get("prehrajto_video_id") or upload.get("target_video_id")
            if film_id is not None and video_id is not None:
                mappings[int(film_id)] = str(video_id)
    return mappings


def load_inventory(path: Path) -> list[AccountVideo]:
    payload = json.loads(path.read_text())
    rows = payload.get("videos", payload) if isinstance(payload, dict) else payload
    return [
        AccountVideo(str(row["video_id"]), row["name"], row.get("url")) for row in rows
    ]


def run_pilot(args) -> int:
    validate_limit(args.limit)
    snapshot = json.loads(args.snapshot.read_text())
    films = [Film.from_dict(row) for row in snapshot["films"]]
    state = StateStore(args.state)
    state.set_snapshot(snapshot["snapshot_id"], snapshot.get("exported_at"))

    email = os.environ.get("PREHRAJTO_EMAIL", "")
    password = os.environ.get("PREHRAJTO_PASSWORD", "")
    authenticated = None
    if args.inventory:
        inventory = load_inventory(args.inventory)
    else:
        authenticated = login(email, password)
        inventory = inventory_account(authenticated)
        if args.inventory_out:
            args.inventory_out.parent.mkdir(parents=True, exist_ok=True)
            args.inventory_out.write_text(
                json.dumps(
                    {
                        "videos": [
                            {
                                "video_id": item.video_id,
                                "name": item.name,
                                "url": item.url,
                            }
                            for item in inventory
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )

    provider_session = requests.Session()
    prehrajto = PrehrajtoProvider(
        proxy_url=os.environ.get("CZ_PROXY_URL", ""),
        proxy_key=os.environ.get("CZ_PROXY_KEY", ""),
        session=provider_session,
        min_gap_seconds=float(os.environ.get("CZ_PROXY_MIN_GAP_SECONDS", "5")),
        allow_direct=os.environ.get("PREHRAJTO_ALLOW_DIRECT", "").lower()
        in {"1", "true", "yes"},
        use_whisper=os.environ.get("ENABLE_WHISPER", "").lower()
        in {"1", "true", "yes"},
    )
    sktorrent = SkTorrentProvider(
        requests.Session(),
        use_whisper=os.environ.get("ENABLE_WHISPER", "").lower()
        in {"1", "true", "yes"},
    )
    transfer = None
    temporary = None
    if args.mode == "upload":
        if authenticated is None:
            authenticated = login(email, password)
        temporary = tempfile.TemporaryDirectory(prefix="cr-films-pilot-")
        transfer = TransferService(authenticated, Path(temporary.name))

    pipeline = HybridPipeline(
        prehrajto=prehrajto,
        sktorrent=sktorrent,
        inventory=inventory,
        state=state,
        transfer=transfer,
        historical=load_historical(args.historical_state),
    )
    try:
        plan = pipeline.build_plan(films, args.limit)
        digest = write_report(plan, args.report, args.plan)
        print(f"Pilot plan contains {len(plan)} films; plan_sha256={digest}")
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with Path(github_output).open("a") as output:
                output.write(f"plan_sha256={digest}\n")
                output.write(f"film_count={len(plan)}\n")
        if args.mode == "dry-run":
            return 0
        if not args.approved_plan_sha or args.approved_plan_sha != digest:
            raise ValueError(
                "Upload requires the exact SHA-256 of the reviewed dry-run plan"
            )
        pipeline.execute(plan)
        return 0
    finally:
        if temporary is not None:
            temporary.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cr-films-pilot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser(
        "export", help="Export the read-only catalog snapshot"
    )
    export.add_argument("--database-url")
    export.add_argument("--out", type=Path, default=Path("data/catalog.json"))

    pilot = subparsers.add_parser("pilot", help="Plan or execute the manual pilot")
    pilot.add_argument("--snapshot", type=Path, required=True)
    pilot.add_argument("--inventory", type=Path)
    pilot.add_argument("--inventory-out", type=Path)
    pilot.add_argument("--state", type=Path, default=Path("state/pilot.json"))
    pilot.add_argument("--report", type=Path, default=Path("artifacts/pilot-report.md"))
    pilot.add_argument("--plan", type=Path, default=Path("artifacts/pilot-plan.json"))
    pilot.add_argument("--historical-state", action="append", type=Path, default=[])
    pilot.add_argument("--limit", type=int, default=10)
    pilot.add_argument("--mode", choices=("dry-run", "upload"), default="dry-run")
    pilot.add_argument("--approved-plan-sha")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "export":
        forwarded = ["--out", str(args.out)]
        if args.database_url:
            forwarded.extend(["--database-url", args.database_url])
        return export_main(forwarded)
    return run_pilot(args)


if __name__ == "__main__":
    raise SystemExit(main())
