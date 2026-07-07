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
| `headquarters` | PostalAddress → else visible `about-us__headquarters` | e.g. «…Kyiv, UA» / «Kyiv» |
| `industry` | visible `about-us__industry` → else JSON | e.g. «Банківська справа» |
| `founded` | visible `about-us__foundedOn` year → else JSON | int year, e.g. 1991 |
| `company_size` | visible `about-us__size` range → else staffCountRange JSON | e.g. «5 001-10 000 працівників» |
| `company_type` | visible `about-us__organizationType` → else JSON | e.g. «У приватній власності» |
| `specialties` | visible `about-us__specialties` → else JSON array | e.g. «banking і financial services» |
| `website` | visible `about-us__website` anchor → else JSON | clean URL, skips linkedin.com self-links |
| `following` | visible «N following» if present | **honest null** on company pages (rarely public) |
| `employees_list` | — | **null / people_wall=true**: `/people` is login-walled (roadmap, we do NOT invent names) |
| `needs_llm` | true only if page found but NO followers AND NO employees | out-of-band salvage hint |

**NEVER invents.** Missing field → `null`; `sources_tried[]` carries the real HTTP status;
`raw_snippet` is handed back for optional LLM salvage.

## Local test
```
python3 usercode.py https://www.linkedin.com/company/pumb
# followers≈12224 employees≈2052 title=ПУМБ headquarters="…Kyiv, UA"
# industry="Банківська справа" founded=1991 company_size="5 001-10 000 працівників"
# company_type="У приватній власності" specialties="banking і financial services" website=http://www.pumb.ua/en
```
