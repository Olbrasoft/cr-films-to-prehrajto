from __future__ import annotations

import hashlib
import json
from pathlib import Path


def plan_hash(plan: list[dict]) -> str:
    canonical = json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_report(plan: list[dict], report_path: Path, plan_path: Path) -> str:
    digest = plan_hash(plan)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(
            {"schema_version": 1, "plan_sha256": digest, "films": plan},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    lines = ["# Ten-film pilot dry-run", "", f"Plan SHA-256: `{digest}`", ""]
    for index, row in enumerate(plan, 1):
        film = row["film"]
        lines.extend(
            [
                f"## {index}. {film['title']} ({film.get('year') or 'unknown year'})",
                "",
                f"- CR film ID: `{film['cr_film_id']}`",
                f"- Runtime: `{film.get('runtime_min')}` minutes",
                f"- Reconciliation: `{row['reconciliation']['status']}`",
            ]
        )
        selected = row.get("selected")
        if selected:
            lines.extend(
                [
                    f"- Provider/source: `{selected['provider']}` / `{selected['source_id']}`",
                    f"- Match tier: `{selected['match_tier']}`",
                    f"- Language tier/evidence: `{selected['language_tier']}` / `{selected.get('language_evidence')}`",
                    f"- Actual resolved resolution: `{selected['resolution']}p`",
                    f"- Czech subtitle preservation: `{row.get('subtitle_handling')}`",
                ]
            )
        else:
            lines.append("- Outcome: `no_acceptable_source`")
        lines.extend(
            [
                "",
                "Candidate evidence:",
                "",
                "```json",
                json.dumps(row.get("candidates", []), ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines))
    return digest
