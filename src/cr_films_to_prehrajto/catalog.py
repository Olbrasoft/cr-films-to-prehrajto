from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

CATALOG_SQL = """
SELECT
    f.id AS cr_film_id,
    f.slug,
    f.title,
    f.original_title,
    f.year,
    NULLIF(f.runtime_min, 0) AS runtime_min,
    f.lang AS original_language,
    COALESCE(f.description, '') AS description,
    f.tmdb_id,
    f.imdb_id,
    COALESCE(
      jsonb_agg(
        jsonb_build_object(
          'provider', p.slug,
          'external_id', vs.external_id,
          'title', vs.title,
          'duration_sec', vs.duration_sec,
          'resolution_hint', vs.resolution_hint,
          'filesize_bytes', vs.filesize_bytes,
          'lang_class', vs.lang_class,
          'audio_lang', vs.audio_lang,
          'audio_confidence', vs.audio_confidence,
          'audio_detected_by', vs.audio_detected_by,
          'cdn', vs.cdn,
          'is_primary', vs.is_primary,
          'is_alive', vs.is_alive,
          'metadata', vs.metadata,
          'subtitles', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
              'lang', vss.lang, 'label', vss.label, 'format', vss.format,
              'url', vss.url, 'is_default', vss.is_default,
              'is_forced', vss.is_forced
            ) ORDER BY vss.id)
            FROM video_source_subtitles vss WHERE vss.source_id = vs.id
          ), '[]'::jsonb)
        ) ORDER BY p.slug, vs.is_primary DESC, vs.external_id
      ) FILTER (WHERE vs.id IS NOT NULL),
      '[]'::jsonb
    ) AS sources
FROM films f
JOIN video_sources vs ON vs.film_id = f.id AND vs.is_alive
JOIN video_providers p ON p.id = vs.provider_id
GROUP BY f.id
ORDER BY f.id
"""


class ReadOnlyViolation(RuntimeError):
    pass


def connect_read_only(dsn: str):
    return psycopg2.connect(dsn, options="-c default_transaction_read_only=on")


def assert_read_only(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SHOW transaction_read_only")
        row = cur.fetchone()
    if not row or str(row[0]).lower() != "on":
        conn.close()
        raise ReadOnlyViolation("Production database connection is not read-only")


def fetch_catalog(conn) -> list[dict[str, Any]]:
    assert_read_only(conn)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(CATALOG_SQL)
        return [dict(row) for row in cur.fetchall()]


def write_snapshot(rows: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "films": rows,
    }
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    payload["snapshot_id"] = hashlib.sha256(canonical.encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a read-only CR film snapshot")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--out", type=Path, default=Path("data/catalog.json"))
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    try:
        conn = connect_read_only(args.database_url)
    except Exception as error:  # noqa: BLE001 - database drivers expose several subclasses
        print(
            f"ERROR: database connection failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return 1
    try:
        rows = fetch_catalog(conn)
    finally:
        conn.close()
    payload = write_snapshot(rows, args.out)
    print(
        f"Exported {len(rows)} playable films; snapshot={payload['snapshot_id'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
