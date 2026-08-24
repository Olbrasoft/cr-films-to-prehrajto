# cr-films-to-prehrajto

This repository implements the safe pilot from issue #1. It reconciles the
currently playable `ceskarepublika.wiki` film catalog against the live uploads
of `filmy.prehrajto@post.cz`, finds a conservative source, and can mirror at
most ten manually approved films.

The production catalog is never changed. Its only use is a read-only export to
a local JSON snapshot; discovery, ranking, download, upload, and state handling
operate on that snapshot.

## Repository source of truth

`state/account-index.json` is the canonical account index for this project. It
maps each known CR film ID to the target Prehraj.to video ID and display name.
It was bootstrapped from the authoritative `filmy.prehrajto@post.cz` shard
states in `Olbrasoft/prehrajto-to-prehrajto` and is updated from
`state/pilot.json` after every upload workflow.

`state/missing-films.json` is regenerated from that index and the latest
read-only production snapshot. Starting with the newest `films.added_at`, it
collects missing playable films until it reaches five consecutive films that
are already in the account index. It also records the complete missing count
for diagnostics, but older gaps beyond that confirmed frontier do not enter an
upload batch. These two versioned files avoid repeatedly listing every page of
the large target account and provide deterministic input for subsequent
GitHub-runner batches.

## Selection rules

The source order is fixed:

1. Search Prehraj.to and conservatively match Czech/original title, year, and
   runtime. Episode-shaped, wrong-year, wrong-runtime, and title-only ambiguous
   results do not enter the upload path.
2. Rank acceptable Prehraj.to results by Czech audio, Slovak audio, then Czech
   subtitles. Actual resolved stream resolution breaks ties inside a language
   tier.
3. Query SK Torrent only if Prehraj.to has no acceptable result or its
   candidates fail during transfer.
4. A Czech-subtitle result succeeds only after its Czech track is preserved,
   uploaded, and verified on the target.

The uploader retains the proven 6 GB GitHub-runner limit and always removes
temporary media. Display names never claim HD, 1080p, or 4K.

## Installation

Python 3.12 is the supported runtime.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest
```

`curl`, `ffmpeg`, and `ffprobe` are runtime system tools. `faster-whisper` is an
optional fallback (`pip install -e '.[whisper]'`) and is not needed by unit
tests.

## Read-only snapshot export

The exporter adds `default_transaction_read_only=on` at connection time,
immediately executes `SHOW transaction_read_only`, and exits unless the server
reports `on`. Its SQL contains only a `SELECT` over films having the same
playability predicate as the current CR film listing: at least one alive
film-owned `video_sources` row.

```bash
export DATABASE_URL='postgresql://...'
cr-films-pilot export --out data/catalog.json
unset DATABASE_URL
```

The snapshot schema is versioned and deterministic. It contains film identity,
description, identifiers, and alive unified-source metadata including subtitle
tracks. `data/*.json` is ignored because production snapshots are runtime
inputs, not source code.

## Local dry run

By default the command logs in only to inventory the target account and makes
no upload request:

```bash
export PREHRAJTO_EMAIL='filmy.prehrajto@post.cz'
export PREHRAJTO_PASSWORD='...'
export CZ_PROXY_URL='...'
export CZ_PROXY_KEY='...'
cr-films-pilot pilot \
  --snapshot data/catalog.json \
  --limit 10 \
  --mode dry-run
```

Review `artifacts/pilot-report.md`. For every selected film it includes title,
year, runtime, duplicate-account evidence, query/match evidence, provider,
language evidence, resolved resolution, and subtitle handling. The report also
contains a SHA-256 identifying the exact plan.

For parser development, `--inventory path/to/inventory.json` accepts a local
fixture and avoids account login. `--historical-state` may be repeated for
state files from related repositories; those IDs are hints and suppress an
upload only when the same ID is still present in the live account inventory.

## Manually approved upload

Upload mode requires the exact SHA-256 from the reviewed dry run and still
enforces the ten-film maximum in Python:

```bash
cr-films-pilot pilot \
  --snapshot data/catalog.json \
  --limit 10 \
  --mode upload \
  --approved-plan-sha '<reviewed SHA-256>'
```

State is atomically saved after each reconciliation, failed candidate, and
success in `state/pilot.json`. Permanent failure burns only that source ID;
transient failures and `no_acceptable_source` remain retryable. A successful
target video ID makes subsequent runs idempotent, and every run also repeats
live-account reconciliation to cover an interruption immediately after a
remote upload.

## GitHub Actions pilot

The `ten-film-pilot` workflow has only `workflow_dispatch`. It has no schedule,
push trigger, sharding, continuation, or self-dispatch. `dry-run` is the default
mode, a single concurrency group prevents overlapping pilots, and upload is a
separate conditional step guarded by the reviewed plan SHA.

Configure these repository or environment Actions secrets in GitHub settings:

- `PREHRAJTO_EMAIL` (must equal `filmy.prehrajto@post.cz`)
- `PREHRAJTO_PASSWORD`
- `CZ_PROXY_URL`
- `CZ_PROXY_KEY`
- `CR_DB_PASSWORD`
- `VPS_HOST`
- `VPS_SSH_PORT`
- `VPS_SSH_KEY`
- `VPS_KNOWN_HOSTS`

The workflow opens an SSH control-socket tunnel only for the export step,
connects to PostgreSQL through localhost, verifies read-only mode, then closes
the tunnel and removes the temporary private-key file before discovery starts.

Do not put secret values in workflow YAML, local `.env` files committed to
Git, issue comments, reports, or command-line arguments. The implementation
does not print cookies, login bodies, upload signatures, proxy keys, signed
stream URLs, or subtitle URLs. GitHub masks Actions secrets as an additional
defense, not as the primary protection.

### Pilot procedure

1. Manually dispatch `dry-run` with a limit from 1 to 10.
2. Download and review the `pilot-evidence` artifact.
3. Verify every film's identity, year, runtime, language, actual resolution,
   provider, subtitle plan, and duplicate evidence.
4. Dispatch `upload` with the same limit and reviewed plan SHA.
5. Verify the resulting target-account entries and committed pilot state.

To stop, cancel the single workflow run in GitHub Actions. There is no queued
self-continuation and no scheduled run to disable. Recovery is a fresh manual
dispatch: the state and live inventory prevent already successful films from
being uploaded twice.

## Related repositories

- [`Olbrasoft/prehrajto-to-prehrajto`](https://github.com/Olbrasoft/prehrajto-to-prehrajto)
  contains the proven Prehraj.to stream resolver, downloader, uploader, state
  persistence, sharding, and GitHub Actions patterns.
- [`Olbrasoft/prehrajto-sync`](https://github.com/Olbrasoft/prehrajto-sync)
  contains the proven SK Torrent CDN resolver, language detection, subtitle
  handling, and SK Torrent fallback upload flow.

## Safety

Never commit credentials, API keys, cookies, access tokens, database passwords,
or generated media. Runtime secrets belong in GitHub Actions secrets or local
environment variables.

The production `ceskarepublika.wiki` database is strictly read-only for this
project. It may be queried to build an input snapshot, but the pipeline must
never insert, update, delete, migrate, or otherwise mutate production data.

## License

Internal Olbrasoft project. No public license is granted.
