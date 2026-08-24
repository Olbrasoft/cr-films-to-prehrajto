# AGENTS.md

## Project objective

Implement GitHub issue #1. The repository mirrors films that are playable on
`ceskarepublika.wiki` but are not yet present on the target Prehraj.to account.
It selects the best acceptable source from Prehraj.to first and SK Torrent
second, then uploads a verified copy to `filmy.prehrajto@post.cz`.

## Required context

Before changing code:

1. Read GitHub issue #1 in full.
2. Read `README.md` in this repository.
3. Inspect the latest remote `main` of both related repositories named in the
   README. Do not assume their local working trees are current or clean.
4. Reuse proven modules and tests where practical, while adapting their data
   contracts explicitly for this repository.

## Non-negotiable rules

- Do not modify the `ceskarepublika.wiki` database or existing playback URLs.
- Target only `filmy.prehrajto@post.cz`. Do not use the `email.cz` account.
- Source preference is Czech audio, Slovak audio, Czech subtitles.
- Language outranks resolution. Resolution ranks candidates only within the
  same language tier.
- Search and matching must prevent wrong-title, wrong-year, and wrong-cut
  uploads.
- The initial workflow must process at most ten films and must require manual
  dispatch. Do not enable a schedule or self-dispatch loop in the pilot.
- Persist state after every attempt so interrupted runs are resumable and
  idempotent.
- Never log or commit secrets, cookies, access tokens, passwords, or media.

## Engineering conventions

- Communicate with the user in Czech.
- Write code, comments, documentation, commits, and GitHub artifacts in
  English.
- Use Python 3.12 unless a proven sister-repository component requires
  otherwise.
- Add focused unit tests for matching, source ranking, state transitions, and
  fallback behavior. Mock network and upload operations in unit tests.
- Run tests and inspect the complete diff before committing.
