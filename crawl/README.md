# dto-mf-crawl

Deterministic site crawler for the **Site-Migration Engine v2** (S2 core), packaged as a
Corezoid **git_call** node (`lang=python`, entry `handle(data)` in repo root, `path=""`).

No LLM. This node fetches same-domain HTML and produces the deterministic S2 core:

- **S2a** GET pages (requests, 15s timeout, 1 retry, cap 25, text/html only, one language),
  links from homepage + `sitemap.xml`, canonical sorted BFS order (reproducible).
- **S2b** `extruct` → JSON-LD / schema.org / OpenGraph / microdata → entities,
  `confidence 0.95`, `source=web-structured`.
- **S2c** `phonenumbers` + regex → phones (+994/+380/+7/intl), email, socials; value on
  2+ pages → `two_source` (confirm candidate), `source=web-regex`.
- **S2d** HTML → clean text (≤40 KB/page) as raw material for later LLM extraction.
- **P4** clean text per page is the raw snapshot — re-run extracts from `snapshot`, no re-download.

git_call window ≤30s / ≤1.4 MB → **cursor mode**: pass `data.cursor` back until `data.done`.

```
handle({url, maxPages?, snapshot?, cursor?}) -> data.crawl = {web_ok, entities[], regex_hits{},
   text_by_url{}, pages[], cursor|null, done, lib_status{}, budget{}, errors[]}
```

Local test: `python usercode.py https://example.com 3`
