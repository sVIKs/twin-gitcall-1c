# social/ — Social Profile Probe (git_call path `social`)

Deterministic, non-inventing probe of a company's **public** social channels / bots
(YouTube · Telegram · Instagram · Facebook · TikTok) for channel name + public metrics
(subscribers / posts / views). Feeds the Corezoid process **`dto-mf-social`**, which turns
each result into a §3 `social-channel` / `bot` entity (parent = company) and runs it through
`dto-mf-writer` + `dto-mf-graph` (actor + accounts + edge to company, edge type 5).

## Entry point
`handle(data)` (git_call convention). Node config:
```
type=git_call, lang=python, repo=https://github.com/sVIKs/twin-gitcall-1c.git,
path=social, commit=main
```

## IN (task data) — any of:
- `social_urls` (list | JSON string) — direct list of social links
- `sources` (obj `{youtube:[],telegram:[],instagram:[],facebook:[],tiktok:[],other:[]}`) from links-discovery
- `url` (str) — a single link

## OUT (`data.social` = list) per target:
`url, platform, entity_class (social-channel|bot), handle, found, blocked, title,
subscribers (int|null), posts (int|null), views (int|null), verified, needs_llm,
sources_tried[{name,status,blocked,err}], raw_snippet, errors[]`
plus `data.social_summary {targets, found, blocked, subscribers_total, lib_status, budget}`.

## Deterministic bedrock (works from datacenter IPs)
- **Telegram** `t.me/<name>` — public preview page: channel title + "N subscribers/members".
  A bot/user contact page has no channel title → `found=false` (honest), `entity_class=bot`
  when the handle ends with `bot`.
- **YouTube** `/@handle · /channel/ID · /c/ · /user/` — `ytInitialData`: title + "N subscribers"
  + video count. Channels that **hide** the subscriber count → `subscribers=null` + `needs_llm`.

Login-walled networks (**Instagram / Facebook / TikTok**) — best-effort `og:*` meta, otherwise
honest `null` + `blocked/status` in `sources_tried`. **Nothing is invented.** `raw_snippet`
(public visible text) is handed to the process for an optional LLM salvage where deterministic
parsing cannot see the metric.

## Verified real extraction (local, real network)
- `youtube.com/@Google` → title `Google`, subscribers `14 400 000`
- `t.me/telegram` → `Telegram News`, subscribers `10 163 584`
- `t.me/durov` → `Pavel Durov`, subscribers `11 809 372`
- `youtube.com/@monobank` → title `monobank`, subscribers `null` (count hidden — honest)
- `t.me/monobank` → `found=false` (bot contact page, no channel) — honest
- `instagram.com/...` → login-wall → honest null

## Test
`python social/usercode.py "<url1>" "<url2>" ...`
(defaults: `youtube.com/@monobank` + `t.me/monobank`)
