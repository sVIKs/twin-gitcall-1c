# dto-mf-site-tech

Deterministic **technical** profiler for the Site-Migration Engine v2, packaged as a Corezoid
**git_call** node (`lang=python`, entry `handle(data)` in `sitetech/usercode.py`, `path=sitetech`).

No LLM. It probes a site as a *technical asset* and returns hard, reproducible engineering
metrics — used by the process `dto-mf-site-tech` to build a `website` actor with characteristic
accounts on the twin graph (class `website`, ontology 614717), and to seed the domain/SSL
expiry calendar-reminder stub.

## Contract

```
handle({url}) -> data.sitetech = {
  domain, url, reachable, status_code,
  whois:   {registrar, created, expires, days_left},
  ssl:     {issuer, expires, days_left, valid},
  pages_count,                       # from sitemap.xml (index-aware) else homepage internal links
  favicon: {present, url},
  robots:  {present, url},
  sitemap: {present, url, urls_count},
  speed_ms,                          # requests.elapsed on homepage GET
  title, meta_description,
  tech_hints: {server, x_powered_by, via, cdn},
  lang,
  lib_status{}, budget{}, errors[]
}
```

Every probe (whois / SSL / robots / sitemap / homepage) has its own timeout and **degrades to
null on failure — never crashes the node**. whois in particular is flaky / may be blocked on the
runner; that surfaces as `whois.*: null` + an entry in `errors[]` + `lib_status["python-whois"]`.

git_call window ≤30s; the whole probe is a handful of fast HTTP/TLS/WHOIS lookups (soft budget 26s).

Deps (`requirements.txt`, installed by the runner on repo pull): `requests`, `beautifulsoup4`,
`python-whois`, `cryptography`.

Local test: `python usercode.py https://pumb.ua`
