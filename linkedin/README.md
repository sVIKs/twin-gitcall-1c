# linkedin — LinkedIn company-profile probe (git_call path `linkedin`)

Deterministic public-profile scraper for `linkedin.com/company/<name>`. LinkedIn does **not**
block datacenter IPs on company pages (verified: `linkedin.com/company/pumb` → HTTP 200), so this
runs det-first with **no LLM** in the normal path.

## Contract (`handle(data)`)

**IN** (first available wins): `url` / `linkedin_url` (str) · `company` (bare slug) ·
`linkedin_urls` (list|json-str) · `sources.linkedin[]` (from links-discovery).

**OUT** `data["linkedin"]`:

| field | source | note |
|---|---|---|
| `title` | og:title minus ` | LinkedIn` | company name |
| `followers` | og:description «… N на LinkedIn / N followers» | int, verified pumb=12224 |
| `employees` | embedded `numberOfEmployees":{"value":N}` / staffCount | int, verified pumb≈2052 |
| `description` | og:description tail | text |
| `license` | og:description sentence with «ліценз…» | e.g. «Банківська ліцензія НБУ №8…» |
| `headquarters` | PostalAddress (streetAddress/locality/country) | e.g. «…Kyiv, UA» |
| `industry` / `website` | embedded JSON if present | **honest null** when not in render |
| `employees_list` | — | **null / people_wall=true**: `/people` is login-walled (roadmap, we do NOT invent names) |
| `needs_llm` | true only if page found but NO followers AND NO employees | out-of-band salvage hint |

**NEVER invents.** Missing field → `null`; `sources_tried[]` carries the real HTTP status;
`raw_snippet` is handed back for optional LLM salvage.

## Local test
```
python3 usercode.py https://www.linkedin.com/company/pumb
# followers=12224 employees=2052 title=ПУМБ headquarters="…Kyiv, UA"
```
