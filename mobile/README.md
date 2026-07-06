# mobile/ — Mobile-App Probe (git_call path `mobile`)

Deterministic, non-inventing probe of a mobile app's **public** store metrics, feeding the
Corezoid process **`dto-mf-mobile`**, which turns the result into `mobile-app` §3 entities
(one per platform) and runs them through `dto-mf-writer` + `dto-mf-graph`.

## Entry point
`handle(data)` (git_call convention). Node config:
```
type=git_call, lang=python, repo=https://github.com/sVIKs/twin-gitcall-1c.git,
path=mobile, commit=main
```

## IN (task data)
- `sources` (list[str] | JSON-string) — App Store / Google Play links (iOS and/or Android)
- or `url` / `ios_url` / `android_url` (str)

## OUT (`data.mobile`)
`found, apps[{platform, app_id, country, url, found, source, sources_tried[], title,
developer, rating(float|null), reviews_count(int|null), version(str|null),
version_date(int YYYYMMDD|null), version_date_iso, installs(int|null), installs_text,
price, genre, raw_snippets[], errors[]}], lib_status, budget, errors`

## Per-platform strategy (why)
- **iOS** — the **public iTunes Lookup API** `https://itunes.apple.com/lookup?id=<id>&country=<cc>`
  (app id parsed from `.../id<digits>`; country from `apps.apple.com/<cc>/…`, fallback `us`).
  Clean JSON: `averageUserRating`, `userRatingCount`, `version`, `currentVersionReleaseDate`,
  `trackName`, `sellerName`, `formattedPrice`, `primaryGenreName`. Very reliable (~95%).
  Apple does **not** publish install counts → `installs` is honest `null`.
- **Android** — parse the public Play page `play.google.com/store/apps/details?id=<pkg>`.
  `rating` / `reviews_count` from JSON-LD (`aggregateRating`), `installs` from the
  server-rendered `…>NN…+</div><div>Downloads</div>` block. `version` is client-rendered on
  Play and usually absent from the server HTML → honest `null`. Reliable ~70-85% for the
  headline metrics.

Chose **direct requests + parsing** (task option B) over `google-play-scraper` /
`app-store-scraper`: the iTunes API + JSON-LD already yield real data reliably and avoid
third-party-library fragility inside the git_call runner. If a source blocks the datacenter IP
(403/429), the field stays `null`, the block is reported in `sources_tried`, and
`raw_snippets` (Android page text) are handed back for optional LLM-fallback extraction by the
process. **Nothing is invented.**

## Verified real extraction (local venv, 2026-07-07)
- monobank **iOS** `id1287005205`: rating `4.88`, reviews `929160`, version `9.1`
  (`20260704`), Free · Finance · developer `UNIVERSAL BANK, PRAT`.
- monobank **Android** `com.ftband.mono`: rating `4.9`, reviews `1137203`, installs
  `10000000` (`10M+`), Free · FINANCE. version → null (client-rendered, honest).
- PUMB **iOS** `id1373626840`: rating `4.78`, reviews `48025`, version `2.335.3` (`20260627`).

## Test
`python mobile/usercode.py "<store-url-1>" "<store-url-2>" …`
(default args probe monobank iOS + Android)
