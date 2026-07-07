#!/usr/bin/env python3
"""Corezoid GIT Call entrypoint (lang=python) — DETERMINISTIC SITE CRAWLER (S2).

Movement: this node ONLY fetches + parses. No LLM. It proves/uses the git_call runner's
outbound web access and produces the deterministic S2 core of the Site-Migration Engine v2:

  S2a  GET same-domain HTML (requests, 15s timeout, 1 retry, cap 25, text/html only,
       one language version). Links from homepage + sitemap.xml. CANONICAL crawl order
       (sorted BFS, normalized URLs) so two runs on the same site are byte-reproducible.
  S2b  extruct -> JSON-LD / schema.org / OpenGraph / microdata -> ready entities,
       confidence 0.95, source=web-structured.
  S2c  phonenumbers + regex -> phones (+994/+380/+7/intl), email, addresses, socials.
       A value seen on 2+ pages is marked two_source (candidate for confirm). source=web-regex.
  S2d  HTML -> clean text (<=40 KB/page) as raw material for later LLM extraction.
  P4   raw snapshot: clean text per page is returned so a re-run extracts FROM the snapshot,
       never re-downloading. (In the executor these accumulate across cursor steps.)

git_call window is <=30s / <=1.4 MB. If 25 pages do not fit, this runs in CURSOR mode:
  IN  data.cursor  (opaque resume token; absent/null = start)
  OUT data.cursor + data.done  (executor loops: call -> persist batch -> call with cursor).

Task data IN:
  url        (str)   seed URL (its registered domain defines the crawl scope)
  maxPages   (int)   hard cap on pages this whole crawl (default 25, clamped 1..25)
  snapshot   (obj)   optional {url: clean_text} — if given, extraction runs from it (no GET)
  cursor     (obj)   resume token from previous call; absent/null = start

Task data OUT (added under data.crawl):
  web_ok, domain, base_lang, pages_fetched, pages[], entities[], regex_hits{},
  two_source[], text_by_url{}, cursor|null, done, lib_status{}, budget{}, errors[]
"""
import os, re, json, time, hashlib, traceback
from collections import OrderedDict

# ---- optional deps: degrade + REPORT instead of crashing the whole node -------------
LIB = {}
try:
    import requests
    LIB["requests"] = getattr(requests, "__version__", "?")
except Exception as e:                      # pragma: no cover
    requests = None; LIB["requests"] = "ERR:%s" % e
try:
    from bs4 import BeautifulSoup
    import bs4
    LIB["beautifulsoup4"] = getattr(bs4, "__version__", "?")
except Exception as e:
    BeautifulSoup = None; LIB["beautifulsoup4"] = "ERR:%s" % e
try:
    import extruct
    LIB["extruct"] = getattr(extruct, "__version__", "?")
except Exception as e:
    extruct = None; LIB["extruct"] = "ERR:%s" % e
try:
    import phonenumbers
    LIB["phonenumbers"] = getattr(phonenumbers, "__version__", "?")
except Exception as e:
    phonenumbers = None; LIB["phonenumbers"] = "ERR:%s" % e

from urllib.parse import urlparse, urljoin, urldefrag

# --------------------------------------------------------------------------------------
HARD_CAP     = 25          # never crawl more than this many pages total
TIMEOUT      = 15          # per-request seconds
RETRY        = 1           # one retry on failure
TEXT_CAP     = 40 * 1024   # <=40 KB clean text per page (S2d)
TIME_BUDGET  = 22.0        # seconds of wall time per git_call window (< 30s node budget)
BYTE_BUDGET  = 1_000_000   # ~1 MB serialized reply budget (< 1.4 MB node limit)
UA           = "dto-mf-crawl/1.0 (+deterministic S2 crawler)"
KNOWN_LANGS  = {"en","ru","uk","az","tr","de","fr","es","it","pl","ka","ar","zh","fa"}

PHONE_RE  = re.compile(r"(?<!\w)(\+?\d[\d\-\s().]{7,17}\d)(?!\w)")
EMAIL_RE  = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
SOCIAL_RE = re.compile(r"https?://(?:www\.)?(?:facebook|instagram|linkedin|twitter|x|youtube|t|telegram|tiktok)\.(?:com|me)/[^\s\"'<>]+", re.I)

# ---- S2c+: DETERMINISTIC financial/legal identifiers (реквізити) --------------------
# These are structured facts a bank/company publishes verbatim; regex is cheaper AND more
# accurate than an LLM. Each hit becomes a web-regex entity (account/license/contact,
# confidence 0.95) downstream. Anchored patterns keep false-positives low.
# Ukrainian IBAN = "UA" + 27 digits (29 chars); may be printed with spaces.
IBAN_RE   = re.compile(r"\bUA(?:\s?\d){27}\b", re.I)
# ЄДРПОУ (company registry code, 8 digits) — anchored to the keyword to avoid random ids.
EDRPOU_RE = re.compile(r"(?:ЄДРПОУ|ЕДРПОУ|ЄДРПО)\D{0,12}(\d{8})\b", re.I)
# МФО (bank routing code, 6 digits) — anchored to the keyword.
MFO_RE    = re.compile(r"(?:МФО)\D{0,8}(\d{6})\b", re.I)
# Ліцензія НБУ / банківська ліцензія / дозвіл — capture the phrase + number/date.
LICENSE_RE = re.compile(
    r"((?:банківська\s+ліценз\w*|ліценз\w*\s+НБУ|генеральна\s+ліценз\w*|ліценз\w*|"
    r"дозв[іi]л\w*|акредитац\w*)[^.,;\n]{0,60}?(?:№|N[o°]?)\s*[\w\-/]{1,30})",
    re.I)
# Адреса: вул./просп./бул./пров./пл. + text (Ukrainian street prefixes).
ADDRESS_RE = re.compile(
    r"((?:вул(?:иця|\.)|просп(?:ект|\.)|бул(?:ьвар|\.)|пров(?:улок|\.)|пл(?:оща|\.)|"
    r"наб(?:ережна|\.)|шосе|майдан)\.?\s*[^,;\n]{2,60})",
    re.I)

# ---- IGNORE-LIST (C2 discover): paths we KNOW are not business sections -------------
# substrings matched against the lowercased URL path; a hit drops the link + records reason.
IGNORE_SUBSTR = (
    "/login", "/signin", "/sign-in", "/log-in", "/logout", "/signout",
    "/register", "/signup", "/sign-up", "/auth",
    "/cart", "/basket", "/checkout", "/order", "/payment", "/pay",
    "/privacy", "/cookie", "/terms", "/policy", "/policies", "/legal", "/gdpr", "/agreement",
    "/search", "/sitemap", "/404", "/500", "/error",
    "/rss", "/feed", "/print", "/share", "/wp-admin", "/wp-login", "/admin",
)
# social redirects / share links (internal or external) — not business sections.
IGNORE_SOCIAL_RE = re.compile(
    r"(facebook|instagram|linkedin|twitter|youtube|youtu\.be|t\.me|telegram|tiktok|viber|whatsapp|pinterest)",
    re.I)
# non-page assets that occasionally appear as <a href> — never a "section".
IGNORE_EXT_RE = re.compile(
    r"\.(?:pdf|jpe?g|png|gif|svg|webp|ico|zip|rar|7z|docx?|xlsx?|pptx?|csv|mp4|mp3|avi|mov|apk|exe|dmg)(?:\?|#|$)",
    re.I)


def _ignore_reason(u):
    """Return a short reason string if URL must be ignored for discover, else None."""
    p = urlparse(u)
    path = (p.path or "/").lower()
    for s in IGNORE_SUBSTR:
        if s in path:
            return s.strip("/") or "root"
    if IGNORE_SOCIAL_RE.search(u):
        return "social"
    if IGNORE_EXT_RE.search(path):
        return "file"
    return None


def _section_key(u):
    """First path segment = the 'section' a URL belongs to; '/' -> home."""
    seg = [s for s in (urlparse(u).path or "").split("/") if s]
    return seg[0].lower() if seg else "home"


def _section_label(sec):
    """Human-ish section label for the progress line (kept short, source language-agnostic)."""
    return "головна" if sec == "home" else sec.replace("-", " ").replace("_", " ")


# KEY business-section keywords (UA/RU/EN) -> priority rank. Lower rank = crawled first
# within maxSections. Matched as substrings of the section key OR its URL path. This is
# how "walk KEY pages, not just home" is enforced deterministically from the site's own menu.
_SECTION_PRIORITY = (
    # rank 1 — реквізити / про банк / компанія / керівництво / структура / інвесторам
    (1, ("rekviz", "реквіз", "реквиз", "requisit", "detail", "about", "pro-bank", "pro-nas",
         "pro_", "probank", "company", "kompan", "компан", "про-банк", "about-us", "aboutus",
         "kerivnyt", "керівн", "руковод", "manage", "management", "board", "pravlinnya",
         "правлін", "struktur", "структ", "team", "komanda", "команда", "investor", "інвестор",
         "governance", "менеджмент", "керівництво")),
    # rank 2 — контакти / карта / відділення / банкомати / адреси / регіони
    (2, ("contact", "kontakt", "контакт", "map", "mapa", "карт", "vidilen", "відділ", "otdelen",
         "отделен", "branch", "office", "ofis", "офіс", "atm", "bankomat", "банкомат",
         "termina", "термінал", "region", "регіон", "address", "adres", "адрес", "network",
         "merezha", "мереж")),
    # rank 3 — продукти / послуги / депозити / кредити / картки / тарифи / ліцензії
    (3, ("product", "produkt", "продукт", "servic", "posluh", "послуг", "услуг", "deposit",
         "депозит", "kredyt", "credit", "кредит", "card", "kart", "карт", "tarif", "тариф",
         "price", "cin", "ціни", "license", "litsenz", "ліценз", "лиценз", "insurance",
         "strah", "страх", "biznes", "business", "бізнес", "privat", "приват")),
)


def _section_priority(sec, url=""):
    hay = (sec + " " + (urlparse(url).path if url else "")).lower()
    for rank, kws in _SECTION_PRIORITY:
        for kw in kws:
            if kw in hay:
                return rank
    return 5   # unknown sections crawled only if budget allows


def _norm_url(u):
    """Canonical URL for dedup: drop fragment, lowercase scheme+host, strip trailing slash."""
    u, _ = urldefrag(u)
    p = urlparse(u)
    host = (p.netloc or "").lower()
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    q = ("?" + p.query) if p.query else ""
    return "%s://%s%s%s" % (p.scheme.lower() or "https", host, path, q)


def _reg_domain(host):
    """registered domain (last two labels) — crude but deterministic; good for same-site test."""
    host = (host or "").lower().split(":")[0]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _lang_of_path(path):
    seg = [s for s in (path or "").split("/") if s]
    if seg and seg[0].lower() in KNOWN_LANGS:
        return seg[0].lower()
    return None


def _fetch(url):
    """GET one page. Returns (status, ctype, text, bytes, elapsed_ms, err)."""
    if requests is None:
        return (0, "", "", 0, 0, "requests-missing")
    last = "unknown"
    for attempt in range(RETRY + 1):
        t0 = time.time()
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA},
                             allow_redirects=True)
            ms = int((time.time() - t0) * 1000)
            ctype = (r.headers.get("Content-Type") or "").lower()
            body_bytes = len(r.content or b"")
            if "text/html" not in ctype and "application/xhtml" not in ctype:
                return (r.status_code, ctype, "", body_bytes, ms, "not-html")
            return (r.status_code, ctype, r.text, body_bytes, ms, "")
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:200])
    return (0, "", "", 0, 0, last)


def _title(html):
    if BeautifulSoup is None:
        m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
        return (m.group(1).strip()[:300] if m else "")
    try:
        soup = BeautifulSoup(html, "html.parser")
        return (soup.title.get_text(strip=True)[:300] if soup.title else "")
    except Exception:
        return ""


def _clean_text(html):
    if not html:
        return ""
    if BeautifulSoup is None:
        txt = re.sub(r"<[^>]+>", " ", html)
    else:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for t in soup(["script", "style", "noscript", "svg"]):
                t.extract()
            txt = soup.get_text(" ", strip=True)
        except Exception:
            txt = re.sub(r"<[^>]+>", " ", html)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:TEXT_CAP]


def _links(base_url, html, scope_domain, base_lang):
    """Same-registered-domain <a href> links, canonicalized, one language version, sorted."""
    out = set()
    if BeautifulSoup is None or not html:
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', html or "")
    else:
        try:
            soup = BeautifulSoup(html, "html.parser")
            hrefs = [a.get("href") for a in soup.find_all("a") if a.get("href")]
        except Exception:
            hrefs = re.findall(r'href=["\']([^"\']+)["\']', html or "")
    for h in hrefs:
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absu = _norm_url(urljoin(base_url, h))
        p = urlparse(absu)
        if p.scheme not in ("http", "https"):
            continue
        if _reg_domain(p.netloc) != scope_domain:
            continue
        lp = _lang_of_path(p.path)
        if lp is not None and base_lang is not None and lp != base_lang:
            continue          # drop other-language version (one language only)
        out.add(absu)
    return sorted(out)        # CANONICAL order for reproducibility


def _sitemap_urls(seed, scope_domain, base_lang):
    if requests is None:
        return []
    sm = _norm_url(urljoin(seed, "/sitemap.xml"))
    try:
        r = requests.get(sm, timeout=TIMEOUT, headers={"User-Agent": UA})
        if r.status_code != 200 or "xml" not in (r.headers.get("Content-Type","").lower()+"xml"):
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text or "")
        else:
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text or "")
    except Exception:
        return []
    out = set()
    for u in locs:
        nu = _norm_url(u)
        p = urlparse(nu)
        if _reg_domain(p.netloc) != scope_domain:
            continue
        lp = _lang_of_path(p.path)
        if lp is not None and base_lang is not None and lp != base_lang:
            continue
        out.add(nu)
    return sorted(out)


# -------- S2b: structured entities via extruct (JSON-LD / OG / microdata) --------------
def _entities_from_structured(url, html):
    ents = []
    if extruct is None or not html:
        # minimal fallback: parse ld+json blocks directly so S2b still yields something
        for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or "", re.I | re.S):
            try:
                obj = json.loads(m.group(1).strip())
            except Exception:
                continue
            ents += _ents_from_jsonld(obj, url)
        return ents
    try:
        data = extruct.extract(html, base_url=url,
                               syntaxes=["json-ld", "microdata", "opengraph", "rdfa"])
    except Exception:
        return ents
    for obj in data.get("json-ld", []):
        ents += _ents_from_jsonld(obj, url)
    for md in data.get("microdata", []):
        t = md.get("type") or ""
        props = md.get("properties", {})
        name = props.get("name") or props.get("legalName")
        if name:
            ents.append(_ent(_class_of(t), name, props, url))
    og = {}
    for tpl in data.get("opengraph", []):
        for k, v in (tpl.get("properties") or []):
            og[k] = v
    if og.get("og:title"):
        ents.append(_ent("organization" if og.get("og:type") in (None, "website", "business.business") else "web_page",
                         og.get("og:title"), og, url))
    return ents


def _ents_from_jsonld(obj, url):
    out = []
    if isinstance(obj, list):
        for o in obj:
            out += _ents_from_jsonld(o, url)
        return out
    if not isinstance(obj, dict):
        return out
    if "@graph" in obj:
        for o in obj["@graph"]:
            out += _ents_from_jsonld(o, url)
    t = obj.get("@type") or ""
    if isinstance(t, list):
        t = t[0] if t else ""
    name = obj.get("name") or obj.get("legalName")
    if name:
        out.append(_ent(_class_of(t), name, obj, url))
    return out


def _class_of(t):
    t = (t or "").lower()
    if "organization" in t or "corporation" in t or "localbusiness" in t or "bank" in t:
        return "organization"
    if t == "person":
        return "person"
    if "product" in t or "offer" in t:
        return "product"
    if "place" in t or "postaladdress" in t:
        return "place"
    return t or "thing"


def _ent(cls, name, raw, url):
    val = {}
    if isinstance(raw, dict):
        for k in ("telephone", "email", "address", "url", "description", "sameAs",
                  "og:description", "og:url", "streetAddress", "addressLocality"):
            if raw.get(k):
                val[k] = raw[k]
    return {"entity": cls, "title": str(name)[:300], "value": val,
            "source": "web-structured", "confidence": 0.95, "source_url": url}


# -------- S2c: regex/phonenumbers contacts -------------------------------------------
def _contacts(url, text, html):
    phones, emails, socials = set(), set(), set()
    blob = (text or "") + " " + (html or "")
    for m in EMAIL_RE.findall(blob):
        emails.add(m.lower())
    for m in SOCIAL_RE.findall(html or ""):
        socials.add(m)
    if phonenumbers is not None:
        for region in ("AZ", "UA", "RU", None):
            try:
                for mt in phonenumbers.PhoneNumberMatcher(text or "", region):
                    phones.add(phonenumbers.format_number(
                        mt.number, phonenumbers.PhoneNumberFormat.E164))
            except Exception:
                pass
    else:
        for m in PHONE_RE.findall(text or ""):
            digits = re.sub(r"[^\d+]", "", m)
            if 9 <= len(digits.lstrip("+")) <= 15:
                phones.add(digits)
    return sorted(phones), sorted(emails), sorted(socials)


def _financial_legal(text, html):
    """DETERMINISTIC реквізити: IBAN / ЄДРПОУ / МФО / ліцензії / адреси.
    Returns dict of sorted unique lists. Cheaper + more accurate than the LLM for
    verbatim structured facts. Consumers map these to account/license/contact entities."""
    blob = (text or "") + " " + (html or "")
    ibans, edrpou, mfo, lic, addr = set(), set(), set(), set(), set()
    for m in IBAN_RE.findall(blob):
        norm = re.sub(r"\s+", "", m).upper()
        if re.match(r"^UA\d{27}$", norm):
            ibans.add(norm)
    for m in EDRPOU_RE.findall(blob):
        edrpou.add(m if isinstance(m, str) else m[0])
    for m in MFO_RE.findall(blob):
        mfo.add(m if isinstance(m, str) else m[0])
    for m in LICENSE_RE.findall(text or ""):     # text only — HTML markup pollutes phrases
        s = re.sub(r"\s+", " ", (m if isinstance(m, str) else m[0])).strip()
        if len(s) >= 6:
            lic.add(s[:120])
    for m in ADDRESS_RE.findall(text or ""):
        s = re.sub(r"\s+", " ", (m if isinstance(m, str) else m[0])).strip()
        if len(s) >= 6:
            addr.add(s[:120])
    return {"ibans": sorted(ibans), "edrpou": sorted(edrpou), "mfo": sorted(mfo),
            "licenses": sorted(lic), "addresses": sorted(addr)}


# --------------------------------------------------------------------------------------
def _crawl(seed, max_pages, snapshot, cursor):
    t_start = time.time()
    errors = []
    seed = _norm_url(seed)
    scope_domain = _reg_domain(urlparse(seed).netloc)

    # resume or start
    if cursor and isinstance(cursor, dict) and cursor.get("queue") is not None:
        queue    = list(cursor.get("queue") or [])
        visited  = list(cursor.get("visited") or [])
        base_lang = cursor.get("base_lang")
        seen_c    = cursor.get("seen") or {}
        pages_done = int(cursor.get("pages_done") or 0)
    else:
        queue, visited, base_lang, seen_c, pages_done = [seed], [], None, {}, 0

    visited_set = set(visited)
    pages, entities, text_by_url = [], [], OrderedDict()
    ph_all, em_all, so_all = OrderedDict(), OrderedDict(), OrderedDict()
    fl_all = {k: OrderedDict() for k in ("ibans", "edrpou", "mfo", "licenses", "addresses")}
    web_ok = False
    out_bytes = 0

    def _fl_merge(txt, html, u):
        for k, vals in _financial_legal(txt, html).items():
            for x in vals:
                fl_all[k].setdefault(x, []).append(u)

    # snapshot re-run: extract from provided text, no GET (P4 reproducibility)
    if snapshot and isinstance(snapshot, dict) and snapshot:
        for u, txt in snapshot.items():
            entities += _entities_from_structured(u, "")   # no html; ld+json unlikely in text
            p, e, s = _contacts(u, txt, "")
            for x in p: ph_all.setdefault(x, []).append(u)
            for x in e: em_all.setdefault(x, []).append(u)
            for x in s: so_all.setdefault(x, []).append(u)
            _fl_merge(txt, "", u)
            text_by_url[u] = txt[:TEXT_CAP]
        return _assemble(True, scope_domain, base_lang, pages, entities, ph_all,
                         em_all, so_all, text_by_url, None, True, errors, t_start, "snapshot", fl_all)

    # seed sitemap into queue on cold start (canonical order preserved)
    if pages_done == 0 and len(visited) == 0:
        for u in _sitemap_urls(seed, scope_domain, base_lang):
            if u not in queue:
                queue.append(u)

    while queue and pages_done < min(max_pages, HARD_CAP):
        if (time.time() - t_start) > TIME_BUDGET or out_bytes > BYTE_BUDGET:
            break
        url = queue.pop(0)
        if url in visited_set:
            continue
        visited_set.add(url); visited.append(url)
        status, ctype, html, nbytes, ms, err = _fetch(url)
        rec = {"url": url, "status": status, "content_type": ctype,
               "bytes": nbytes, "elapsed_ms": ms, "title": "", "err": err}
        if err or not html:
            errors.append("%s -> %s" % (url, err or "empty"))
            pages.append(rec); pages_done += 1
            continue
        web_ok = True
        rec["title"] = _title(html)
        pages.append(rec); pages_done += 1

        if base_lang is None:
            m = re.search(r'<html[^>]*\blang=["\']?([a-zA-Z]{2})', html)
            base_lang = (m.group(1).lower() if m else _lang_of_path(urlparse(url).path))

        entities += _entities_from_structured(url, html)
        txt = _clean_text(html)
        text_by_url[url] = txt
        p, e, s = _contacts(url, txt, html)
        for x in p: ph_all.setdefault(x, []).append(url)
        for x in e: em_all.setdefault(x, []).append(url)
        for x in s: so_all.setdefault(x, []).append(url)
        _fl_merge(txt, html, url)

        for lk in _links(url, html, scope_domain, base_lang):
            if lk not in visited_set and lk not in queue:
                queue.append(lk)
        out_bytes = len(json.dumps(text_by_url)) + len(json.dumps(entities))

    done = (not queue) or (pages_done >= min(max_pages, HARD_CAP))
    nxt_cursor = None
    if not done:
        nxt_cursor = {"queue": queue, "visited": visited, "base_lang": base_lang,
                      "seen": seen_c, "pages_done": pages_done}
    return _assemble(web_ok, scope_domain, base_lang, pages, entities, ph_all,
                     em_all, so_all, text_by_url, nxt_cursor, done, errors, t_start, "crawl", fl_all)


def _twosrc(d):
    return sorted([k for k, urls in d.items() if len(set(urls)) >= 2])


def _assemble(web_ok, domain, base_lang, pages, entities, ph, em, so,
              text_by_url, cursor, done, errors, t_start, mode, fl=None):
    fl = fl or {}
    def _flkeys(k):
        return sorted((fl.get(k) or {}).keys())
    return {
        "web_ok": bool(web_ok),
        "mode": mode,
        "domain": domain,
        "base_lang": base_lang,
        "pages_fetched": len([p for p in pages if p.get("status") == 200]),
        "pages": pages,
        "entities": entities,
        "regex_hits": {
            "phones": sorted(ph.keys()),
            "emails": sorted(em.keys()),
            "socials": sorted(so.keys()),
            "ibans": _flkeys("ibans"),
            "edrpou": _flkeys("edrpou"),
            "mfo": _flkeys("mfo"),
            "licenses": _flkeys("licenses"),
            "addresses": _flkeys("addresses"),
        },
        "two_source": {
            "phones": _twosrc(ph), "emails": _twosrc(em), "socials": _twosrc(so),
            "ibans": _twosrc(fl.get("ibans") or {}), "edrpou": _twosrc(fl.get("edrpou") or {}),
            "mfo": _twosrc(fl.get("mfo") or {}),
        },
        "text_by_url": dict(text_by_url),
        "cursor": cursor,
        "done": done,
        "lib_status": LIB,
        "budget": {"elapsed_s": round(time.time() - t_start, 2),
                   "time_budget_s": TIME_BUDGET, "byte_budget": BYTE_BUDGET},
        "errors": errors,
    }


# ======================================================================================
# C2. discover(url) — estimate volume + honest denominator N (base sections after ignore)
# ======================================================================================
def _discover(seed, max_sections=HARD_CAP):
    t0 = time.time()
    max_sections = max(1, min(HARD_CAP, int(max_sections or HARD_CAP)))
    seed = _norm_url(seed)
    scope_domain = _reg_domain(urlparse(seed).netloc)
    status, ctype, html, nbytes, ms, err = _fetch(seed)
    if err or not html:
        return {"mode": "discover", "web_ok": False, "domain": scope_domain,
                "pages_total": 0, "page_urls": [], "sections": [], "ignored": [],
                "base_lang": None, "title": "",
                "errors": ["homepage %s -> %s" % (seed, err or "empty")],
                "lib_status": LIB, "budget": {"elapsed_s": round(time.time() - t0, 2)}}

    base_lang = None
    m = re.search(r'<html[^>]*\blang=["\']?([a-zA-Z]{2})', html)
    if m:
        base_lang = m.group(1).lower()

    # all same-domain, one-language, canonical links from the homepage (+ sitemap)
    links = list(_links(seed, html, scope_domain, base_lang))
    for u in _sitemap_urls(seed, scope_domain, base_lang):
        if u not in links:
            links.append(u)
    links = sorted(set(links))

    kept, ignored = [], []
    for u in links:
        reason = _ignore_reason(u)
        if reason:
            ignored.append({"url": u, "reason": reason})
        else:
            kept.append(u)

    # collapse KEPT links to BASE SECTIONS: one shallowest representative per first-segment.
    # homepage itself is always section #0 (richest structured data lives there).
    by_section = OrderedDict()
    by_section["home"] = seed
    for u in kept:
        sec = _section_key(u)
        if sec == "home":
            continue
        cur = by_section.get(sec)
        if cur is None or (len(urlparse(u).path) < len(urlparse(cur).path)):
            by_section[sec] = u

    # PRIORITY: with a maxSections cap, KEY business sections (реквізити / про банк /
    # керівництво / контакти-відділення / продукти) must survive over random ones.
    # Deterministic sort by (rank, section-name); "home" is always kept first.
    non_home = [s for s in by_section.keys() if s != "home"]
    non_home.sort(key=lambda s: (_section_priority(s, by_section[s]), s))
    ordered = ["home"] + non_home
    sections = ordered[:max_sections]
    page_urls = [by_section[s] for s in sections]
    section_labels = [_section_label(s) for s in sections]

    return {
        "mode": "discover", "web_ok": True, "domain": scope_domain,
        "base_lang": base_lang, "title": _title(html),
        "pages_total": len(page_urls),          # <- HONEST progress DENOMINATOR
        "page_urls": page_urls,                  # aligned with sections/labels
        "sections": section_labels,
        "ignored": ignored,                      # {url, reason} — what the ignore-list removed
        "links_seen": len(links),
        "errors": [],
        "lib_status": LIB,
        "budget": {"elapsed_s": round(time.time() - t0, 2)},
    }


# ======================================================================================
# C2. page(url) — one page: clean text (<=40 KB) + structured entities + same-domain links
# ======================================================================================
def _page(url):
    t0 = time.time()
    url = _norm_url(url)
    scope_domain = _reg_domain(urlparse(url).netloc)
    status, ctype, html, nbytes, ms, err = _fetch(url)
    if err or not html:
        return {"mode": "page", "web_ok": False, "url": url, "title": "",
                "text": "", "structured_entities": [], "links": [],
                "regex_hits": {"phones": [], "emails": [], "socials": []},
                "errors": ["%s -> %s" % (url, err or "empty")],
                "budget": {"elapsed_s": round(time.time() - t0, 2)}}
    base_lang = None
    m = re.search(r'<html[^>]*\blang=["\']?([a-zA-Z]{2})', html)
    if m:
        base_lang = m.group(1).lower()
    txt = _clean_text(html)
    ents = _entities_from_structured(url, html)
    ph, em, so = _contacts(url, txt, html)
    fl = _financial_legal(txt, html)              # IBAN/ЄДРПОУ/МФО/ліцензії/адреси
    links = _links(url, html, scope_domain, base_lang)
    return {
        "mode": "page", "web_ok": True, "url": url, "title": _title(html),
        "text": txt,                              # <=40 KB clean text (chunk_text for C1)
        "structured_entities": ents,              # web-structured, conf 0.95 (two-source vs LLM)
        "regex_hits": {"phones": ph, "emails": em, "socials": so,
                       "ibans": fl["ibans"], "edrpou": fl["edrpou"], "mfo": fl["mfo"],
                       "licenses": fl["licenses"], "addresses": fl["addresses"]},
        "links": links,
        "errors": [],
        "budget": {"elapsed_s": round(time.time() - t0, 2),
                   "bytes": nbytes, "fetch_ms": ms},
    }


def handle(data):
    try:
        url = data.get("url") or data.get("source_url") or ""
        mode = (data.get("mode") or "").strip().lower()
        if not url:
            data["crawl"] = {"web_ok": False, "errors": ["no url"], "lib_status": LIB,
                             "done": True, "cursor": None, "mode": mode or "crawl"}
            return data
        if mode == "discover":                    # C2: volume estimate + honest N
            data["crawl"] = _discover(url, data.get("maxSections") or HARD_CAP)
            return data
        if mode == "page":                        # C2: single page fetch+parse
            data["crawl"] = _page(url)
            return data
        # default: full BFS crawl (unchanged behaviour, back-compatible)
        try:
            max_pages = int(data.get("maxPages") or 25)
        except Exception:
            max_pages = 25
        max_pages = max(1, min(HARD_CAP, max_pages))
        snapshot = data.get("snapshot") or None
        cursor = data.get("cursor") or None
        data["crawl"] = _crawl(url, max_pages, snapshot, cursor)
    except Exception as e:
        data["crawl"] = {"web_ok": False, "done": True, "cursor": None,
                         "lib_status": LIB,
                         "errors": ["FATAL: %s" % e, traceback.format_exc()[:1500]]}
    return data


if __name__ == "__main__":
    import sys
    seed = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    mp = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    out = handle({"url": seed, "maxPages": mp})
    c = out["crawl"]
    print(json.dumps({k: (v if k != "text_by_url" else {u: t[:120] for u, t in v.items()})
                      for k, v in c.items()}, ensure_ascii=False, indent=2)[:6000])
