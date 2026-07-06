# registry/ — Open-Registry Probe (git_call path `registry`)

Deterministic, non-inventing probe of Ukrainian **public** registries for a company's
governance/ownership/registry facts. Feeds the Corezoid process **`dto-mf-registry`**, which
turns the result into §3 entities (company/capital/owner/founder/shareholder/legal-case) and
runs them through `dto-mf-writer` + `dto-mf-graph`.

## Entry point
`handle(data)` (git_call convention). Node config:
```
type=git_call, lang=python, repo=https://github.com/sVIKs/twin-gitcall-1c.git,
path=registry, commit=main
```

## IN (task data)
- `company_name` (str, optional)
- `edrpou` (str, optional — 8-digit ЄДРПОУ; the strongest key)
- `site_url` (str, optional — company site, used as a fallback registry-fact source)

## OUT (`data.registry`)
`found, source, sources_tried[{name,status,blocked}], edrpou, full_name, ownership_form,
capital_uah, kved, kved_text, registration_date, director, beneficiaries[{name,share_pct}],
founders[{name,share_pct,capital_uah}], court_cases[{number,note}], raw_snippets[str],
lib_status{}, budget{}, errors[]`

## Sources (best-effort, honest status)
`clarity-project.info` · `youcontrol.com.ua` (UA+EN) · `opendatabot.ua` · the company site.
Registries frequently block datacenter IPs (403/429) — each block is reported in
`sources_tried`, the field stays `null`, and `raw_snippets` are handed to the process for an
optional LLM-fallback extraction. **Nothing is invented.**

## Verified real extraction (local, PUMB ЄДРПОУ 14282829)
- edrpou `14282829`, ownership_form `JOINT STOCK COMPANY`, capital_uah `4780594950`,
  kved `64.19`, registration_date `23.12.1991`, director `Черненко Сергій Павлович`
- founders: `ТОВ «СКМ ФІНАНС»` 92.34%, `SCM HOLDINGS LIMITED` 7.66%, `ЗАТ «ТК МГЗ»` 8.62%
- source `youcontrol` (clarity-project → 403 blocked, reported honestly)

## Test
`python registry/usercode.py <edrpou> "<company_name>" "<site_url>"`
