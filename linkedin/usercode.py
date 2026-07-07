#!/usr/bin/env python3
"""Corezoid GIT Call entrypoint (lang=python) — LINKEDIN COMPANY PROFILE PROBE (dto-mf-linkedin).

Movement: по ПУБЛІЧНІЙ сторінці компанії LinkedIn (linkedin.com/company/<name>) дістати публічні
характеристики компанії як DTO: підписники, к-сть співробітників, галузь, опис, ліцензія, сайт,
штаб-квартира. LinkedIn НЕ блокує датацентр-IP на company-сторінках (перевірено боєм:
linkedin.com/company/pumb -> HTTP 200, og:description «...12 224 на LinkedIn...»,
numberOfEmployees":{"value":2052). Список співробітників (/people) — за login-wall, тому НЕ
чіпаємо (honest skip; roadmap).

Детермінований bedrock (реально працює на датацентр-IP):
  * og:title       -> назва компанії ("<Name> | LinkedIn").
  * og:description -> "Послідовники <Name> | N на LinkedIn. <ліцензія>. | <опис>" -> підписники + опис + ліцензія.
  * embedded JSON  -> numberOfEmployees":{"value":N}  -> к-сть співробітників (staffCount).
  * PostalAddress  -> streetAddress/addressLocality/addressCountry -> штаб-квартира.
  * industry/website -> є не в кожному рендері -> honest null (НЕ вигадуємо).

НІЧОГО НЕ ВИГАДУЄМО: якщо поле не дістали публічно — воно = null, статус видно у sources_tried,
raw_snippet віддається процесу для опційного LLM-салважу (needs_llm). Список співробітників за
login-wall -> people_wall=true, employees_list=null (roadmap, не вигадуємо ПІБ).

Контракт (git_call викликає handle(data)):
  IN:  url | linkedin_url (str)   — пряме посилання; АБО
       company (str)              — slug (linkedin.com/company/<company>); АБО
       linkedin_urls (list|json)  — список; АБО
       sources (obj {linkedin:[]}) від links-discovery.
  OUT: data["linkedin"] = {
         url, handle, found, blocked, people_wall,
         title, followers (int|null), employees (int|null),
         industry (str|null), description (str|null), license (str|null),
         website (str|null), headquarters (str|null),
         founded (int year|null), company_type (str|null), specialties (str|null),
         company_size (str|null), following (int|null),
         employees_list (null — roadmap), verified (bool|null), needs_llm (bool),
         sources_tried[{name,status,blocked,err}], raw_snippet (str), errors[]
       }
       data["linkedin_summary"] = {found, blocked, followers, employees, lib_status{}, budget{}}

Вікно git_call <=30s: 1 HTTP на company-сторінку зі своїм таймаутом; будь-який збій -> graceful null.
"""
import re, json, time, traceback
from urllib.parse import urlparse

LIB = {}
try:
    import requests
    LIB["requests"] = getattr(requests, "__version__", "?")
except Exception as e:                       # pragma: no cover
    requests = None; LIB["requests"] = "ERR:%s" % e
try:
    from bs4 import BeautifulSoup
    import bs4
    LIB["beautifulsoup4"] = getattr(bs4, "__version__", "?")
except Exception as e:
    BeautifulSoup = None; LIB["beautifulsoup4"] = "ERR:%s" % e

HTTP_TIMEOUT = 10
TIME_BUDGET = 26
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------- helpers ----------------
def _human_num(s):
    """'12 224' / '12\\xa0224' / '1.2M' / '12.3K' / '1,234' -> int|None. Non-inventing."""
    if s is None:
        return None
    t = str(s).replace(" ", " ").replace(" ", " ").strip().lower()
    if not t:
        return None
    mult = 1.0
    if re.search(r"\bмлрд|\bbillion|\bbln\b|[0-9]\s*b\b", t):
        mult = 1e9
    elif re.search(r"\bмлн|\bмільйон|\bmillion|\bmln\b|[0-9]\s*m\b", t):
        mult = 1e6
    elif re.search(r"\bтис\.?|\bтисяч|\bthousand|[0-9]\s*k\b", t):
        mult = 1e3
    m = re.search(r"(\d[\d\s.,]*\d|\d)", t)
    if not m:
        return None
    raw = m.group(1)
    if mult != 1.0:
        raw = raw.replace(" ", "").replace(",", ".")
        try:
            return int(round(float(raw) * mult))
        except Exception:
            return None
    raw = re.sub(r"[ ,.](?=\d{3}\b)", "", raw)   # thousands sep
    raw = re.sub(r"[^\d]", "", raw)
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _unescape(s):
    if not s:
        return s
    s = (s.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
          .replace("&gt;", ">").replace("&lt;", "<").replace("&nbsp;", " "))
    return s


def _get(url, cookies=None):
    out = {"url": url, "status": None, "blocked": False, "html": "", "err": None, "final": url}
    if requests is None:
        out["err"] = "requests-missing"; return out
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True,
                         headers={"User-Agent": UA, "Accept-Language": "uk,en;q=0.8",
                                  "Accept": "text/html,application/xhtml+xml"},
                         cookies=cookies or {})
        out["status"] = r.status_code
        out["final"] = r.url
        if r.status_code in (401, 403, 429, 451, 999):
            out["blocked"] = True
        elif r.status_code == 200:
            out["html"] = r.text or ""
    except Exception as e:
        out["err"] = "%s: %s" % (type(e).__name__, str(e)[:160])
    return out


def _meta(html, prop):
    for pat in (r'<meta[^>]+property=["\']%s["\'][^>]+content=["\']([^"\']*)["\']' % re.escape(prop),
                r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']%s["\']' % re.escape(prop),
                r'<meta[^>]+name=["\']%s["\'][^>]+content=["\']([^"\']*)["\']' % re.escape(prop)):
        m = re.search(pat, html, re.I)
        if m:
            return _unescape(m.group(1).strip())
    return None


def _about(html, field):
    """Public guest company page has an 'about-us' <dl>: <div data-test-id="about-us__<field>">
    <dt>label</dt><dd>value</dd></div>. Return cleaned dd text (visible, no login). Non-inventing."""
    if not html:
        return None
    m = re.search(r'data-test-id="about-us__%s".*?<dd[^>]*>(.*?)</dd>' % re.escape(field), html, re.S)
    if not m:
        return None
    v = re.sub(r"<[^>]+>", " ", m.group(1))
    v = re.sub(r"\s+", " ", _unescape(v)).strip()
    return v or None


def _text(html, limit=1800):
    if not html:
        return ""
    if BeautifulSoup:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.extract()
            txt = re.sub(r"[ \t\r\f\v ]+", " ", soup.get_text(" ", strip=True))
            return txt[:limit]
        except Exception:
            pass
    return re.sub(r"[ \t\r\f\v ]+", " ", re.sub(r"<[^>]+>", " ", html))[:limit]


def _company_url(url):
    """Normalize any linkedin ref to the canonical company profile URL. Accepts a bare slug too."""
    u = str(url or "").strip()
    if not u:
        return "", ""
    if not re.search(r"linkedin\.com", u, re.I) and "/" not in u and " " not in u:
        # bare slug
        return "https://www.linkedin.com/company/%s" % u, u
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    m = re.search(r"linkedin\.com/(?:company|school|showcase)/([^/?#]+)", u, re.I)
    handle = m.group(1) if m else None
    if handle:
        return "https://www.linkedin.com/company/%s" % handle, handle
    return u, handle


def _blank(url, handle):
    return {"url": url, "handle": handle, "found": False, "blocked": False, "people_wall": True,
            "title": None, "followers": None, "employees": None, "industry": None,
            "description": None, "license": None, "website": None, "headquarters": None,
            "founded": None, "company_type": None, "specialties": None,
            "company_size": None, "following": None,
            "employees_list": None, "verified": None, "needs_llm": False,
            "sources_tried": [], "raw_snippet": "", "errors": []}


# ---------------- extraction ----------------
def _extract_followers(desc, html):
    """og:description «Послідовники X | 12 224 на LinkedIn. …» / «X | 12,224 followers»."""
    for src in (desc or "", html or ""):
        m = re.search(r"([\d][\d\s  .,]*)\s*(?:на LinkedIn|followers|подписчик|підписник)", src, re.I)
        if m:
            n = _human_num(m.group(1))
            if n:
                return n
    return None


def _extract_employees(html):
    """embedded JSON numberOfEmployees":{"value":N} / numberOfEmployees:N / staffCount:N."""
    for pat in (r'numberOfEmployees"\s*:\s*\{\s*"value"\s*:\s*(\d+)',
                r'numberOfEmployees"?\s*:\s*(\d+)',
                r'staffCount"?\s*:\s*(\d+)',
                r'"employeeCount"\s*:\s*(\d+)'):
        m = re.search(pat, html)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None


def _extract_description(desc):
    """Strip the «Послідовники X | N на LinkedIn. » prefix -> real company description text."""
    if not desc:
        return None, None
    license_ = None
    ml = (re.search(r"(?:^|[|.]\s+)([А-ЯҐЄІЇ][^|]*?ліценз[^|]*?\d{4}\s*р?\.?)", desc)
          or re.search(r"(?:^|[|.]\s+)([А-ЯҐЄІЇ][^|]*?ліценз[^|]*)", desc)
          or re.search(r"(?:^|[|.]\s+)([A-Z][^|]*?licen[cs]e[^|]*)", desc))
    if ml:
        license_ = ml.group(1).strip(" .|") or None
    body = re.sub(r"^\s*(?:Послідовники|Followers|Подписчики|Підписники)[^|]*\|\s*[\d\s .,]+\s*(?:на LinkedIn|followers)\s*\.?\s*",
                  "", desc, flags=re.I)
    body = body.strip(" .|")
    return (body[:600] or None), license_


def _extract_hq(html):
    street = re.search(r'"streetAddress"\s*:\s*"([^"]*)"', html)
    city = re.search(r'"addressLocality"\s*:\s*"([^"]*)"', html)
    region = re.search(r'"addressRegion"\s*:\s*"([^"]*)"', html)
    country = re.search(r'"addressCountry"\s*:\s*"([^"]*)"', html)
    parts = []
    for m in (street, city, region, country):
        if m and m.group(1).strip():
            v = _unescape(m.group(1).strip())
            if v not in parts:
                parts.append(v)
    if parts:
        return ", ".join(parts)
    return _about(html, "headquarters")   # visible guest fallback (e.g. "Kyiv")


def _extract_industry(html):
    """Public about-us block first (visible guest render), then embedded JSON."""
    a = _about(html, "industry")
    if a:
        return a[:200]
    for pat in (r'"industryName"\s*:\s*"([^"]+)"',
                r'"industry"\s*:\s*"([^"]+)"',
                r'"industries"\s*:\s*\[\s*"([^"]+)"'):
        m = re.search(pat, html)
        if m:
            return _unescape(m.group(1).strip())
    return None


def _extract_website(html):
    """about-us website anchor text (clean URL), then embedded JSON. Skip linkedin.com self-links."""
    m = re.search(r'data-test-id="about-us__website".*?<a[^>]*>(.*?)</a>', html, re.S)
    if m:
        v = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _unescape(m.group(1)))).strip()
        v = re.split(r"\s", v)[0] if v else v
        if v and "linkedin.com" not in v and re.match(r"^https?://", v, re.I):
            return v
    for pat in (r'"companyPageUrl"\s*:\s*"([^"]+)"',
                r'"websiteUrl"\s*:\s*"([^"]+)"',
                r'"website"\s*:\s*"(https?://[^"]+)"'):
        m = re.search(pat, html)
        if m and "linkedin.com" not in m.group(1):
            return _unescape(m.group(1).strip())
    return None


def _extract_founded(html):
    """about-us foundedOn (visible year) first, then foundedOn.year JSON / visible 'Founded <year>'."""
    a = _about(html, "foundedOn")
    if a:
        m = re.search(r"(\d{4})", a)
        if m:
            y = int(m.group(1))
            if 1700 <= y <= 2100:
                return y
    for pat in (r'"foundedOn"\s*:\s*\{?\s*"year"\s*:\s*(\d{4})',
                r'"companyFoundedOn"[^}]*?(\d{4})',
                r'(?:Founded|Засновано|Основана|Основано)\D{0,12}(\d{4})'):
        m = re.search(pat, html)
        if m:
            try:
                y = int(m.group(1))
                if 1700 <= y <= 2100:
                    return y
            except Exception:
                pass
    return None


def _extract_company_type(html):
    """about-us organizationType (visible, e.g. 'У приватній власності'/'Privately Held'), then JSON."""
    a = _about(html, "organizationType")
    if a:
        return a[:120]
    for pat in (r'"companyType"\s*:\s*\{?[^}]*?"localizedName"\s*:\s*"([^"]+)"',
                r'"companyType"\s*:\s*"([^"]+)"'):
        m = re.search(pat, html)
        if m:
            return _unescape(m.group(1).strip())
    return None


def _extract_specialties(html):
    """about-us specialties (visible list) first, then embedded JSON array."""
    a = _about(html, "specialties")
    if a:
        return a[:600]
    for pat in (r'"specialities"\s*:\s*\[([^\]]*)\]',
                r'"specialties"\s*:\s*\[([^\]]*)\]'):
        m = re.search(pat, html)
        if m and m.group(1).strip():
            items = re.findall(r'"([^"]+)"', m.group(1))
            if items:
                return _unescape(", ".join(x.strip() for x in items if x.strip()))[:600] or None
    return None


def _extract_company_size(html):
    """about-us size (visible range, e.g. '5 001-10 000 працівників') first, then JSON/visible."""
    a = _about(html, "size")
    if a:
        return a[:120]
    m = re.search(r'"staffCountRange"\s*:\s*\{\s*"start"\s*:\s*(\d+)\s*,\s*"end"\s*:\s*(\d+)', html)
    if m:
        return "%s-%s employees" % (m.group(1), m.group(2))
    m = re.search(r'"staffCountRange"\s*:\s*\{\s*"start"\s*:\s*(\d+)', html)
    if m:
        return "%s+ employees" % m.group(1)
    m = re.search(r'(\d[\d,\s]*\s*-\s*\d[\d,\s]*)\s*(?:employees|співробітник|сотрудник)', html, re.I)
    if m:
        return re.sub(r"\s+", "", m.group(1)) + " employees"
    m = re.search(r'(\d[\d,\s]*\+)\s*(?:employees|співробітник|сотрудник)', html, re.I)
    if m:
        return re.sub(r"\s+", "", m.group(1)) + " employees"
    return None


def _extract_following(html):
    """Rare on company pages -> null if absent."""
    m = re.search(r"([\d][\d\s.,]*)\s*(?:following|стежить|подписок|відстежує)", html, re.I)
    if m:
        return _human_num(m.group(1))
    return None


def _profile(url):
    cu, handle = _company_url(url)
    res = _blank(cu, handle)
    if not cu:
        res["errors"].append("empty-url"); return res
    try:
        g = _get(cu)
        res["sources_tried"].append({"name": "linkedin/company", "status": g["status"],
                                     "blocked": g["blocked"], "err": g["err"]})
        if g["blocked"]:
            res["blocked"] = True
        if not g["html"]:
            return res
        html = g["html"]
        res["raw_snippet"] = _text(html)
        title = _meta(html, "og:title")
        if title:
            res["title"] = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title).strip()[:200]
        desc = _meta(html, "og:description") or ""
        res["followers"] = _extract_followers(desc, html)
        res["employees"] = _extract_employees(html)
        body, license_ = _extract_description(desc)
        res["description"] = body
        res["license"] = license_
        res["headquarters"] = _extract_hq(html)
        res["industry"] = _extract_industry(html)
        res["website"] = _extract_website(html)
        res["founded"] = _extract_founded(html)
        res["company_type"] = _extract_company_type(html)
        res["specialties"] = _extract_specialties(html)
        res["company_size"] = _extract_company_size(html)
        res["following"] = _extract_following(html)
        res["found"] = bool(res["title"])
        # людський список співробітників за login-wall -> НЕ вигадуємо
        res["people_wall"] = True
        res["employees_list"] = None
        # needs_llm лише якщо є сторінка, але детермінований парсинг не дав НІ підписників НІ співробітників
        if res["found"] and res["followers"] is None and res["employees"] is None:
            res["needs_llm"] = True
    except Exception as e:
        res["errors"].append("%s: %s" % (type(e).__name__, str(e)[:160]))
    return res


# ---------------- target collection ----------------
def _collect(data):
    urls = []

    def add(x):
        if not x:
            return
        x = str(x).strip()
        if x and x not in urls:
            urls.append(x)

    lu = data.get("linkedin_urls")
    if isinstance(lu, str):
        try:
            lu = json.loads(lu)
        except Exception:
            lu = [lu] if lu.strip() else []
    if isinstance(lu, list):
        for u in lu:
            add(u)
    src = data.get("sources")
    if isinstance(src, str):
        try:
            src = json.loads(src)
        except Exception:
            src = {}
    if isinstance(src, dict):
        v = src.get("linkedin")
        if isinstance(v, list):
            for u in v:
                add(u)
        elif isinstance(v, str):
            add(v)
    add(data.get("linkedin_url"))
    add(data.get("url"))
    add(data.get("company"))
    # keep only linkedin-ish or bare slug
    keep = []
    for u in urls:
        if re.search(r"linkedin\.com", u, re.I) or re.match(r"^[A-Za-z0-9][A-Za-z0-9\-_%]*$", u):
            keep.append(u)
    return keep[:1]   # company profile = 1 ціль на прогон


def handle(data):
    t0 = time.time()
    try:
        urls = _collect(data)
        if not urls:
            data["linkedin"] = _blank("", None)
            data["linkedin"]["errors"].append("no-linkedin-url")
            data["linkedin_summary"] = {"found": 0, "blocked": 0, "followers": None,
                                        "employees": None, "lib_status": LIB,
                                        "budget": {"elapsed_s": round(time.time() - t0, 2)}}
            return data
        res = _profile(urls[0])
        data["linkedin"] = res
        data["linkedin_summary"] = {"found": 1 if res["found"] else 0,
                                    "blocked": 1 if res["blocked"] else 0,
                                    "followers": res["followers"], "employees": res["employees"],
                                    "lib_status": LIB,
                                    "budget": {"elapsed_s": round(time.time() - t0, 2),
                                               "time_budget_s": TIME_BUDGET}}
    except Exception as e:
        data["linkedin"] = _blank("", None)
        data["linkedin"]["errors"].append("FATAL: %s" % e)
        data["linkedin_summary"] = {"found": 0, "blocked": 0, "followers": None, "employees": None,
                                    "lib_status": LIB, "errors": [traceback.format_exc()[:1200]]}
    return data


if __name__ == "__main__":
    import sys
    tgt = sys.argv[1] if len(sys.argv) > 1 else "https://www.linkedin.com/company/pumb"
    out = handle({"url": tgt})
    r = dict(out["linkedin"]); r["raw_snippet"] = "<%d chars>" % len(out["linkedin"].get("raw_snippet", ""))
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("SUMMARY:", json.dumps(out["linkedin_summary"], ensure_ascii=False))
