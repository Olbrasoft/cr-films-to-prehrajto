# cr-films-to-prehrajto

Mirror missing playable films from the `ceskarepublika.wiki` catalog to the
Olbrasoft account `filmy.prehrajto@post.cz`.

For each film missing from the target account, the pipeline will prefer an
existing Czech or Slovak source on Prehraj.to and fall back to SK Torrent when
Prehraj.to has no acceptable source. Existing film URLs in the
`ceskarepublika.wiki` database remain unchanged.

The first implementation milestone is specified in GitHub issue #1. It must be
delivered as a safe pilot of ten films before any continuous or large-batch run
is enabled.

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

## License

Internal Olbrasoft project. No public license is granted.
