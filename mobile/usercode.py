#!/usr/bin/env python3
"""Corezoid GIT Call entrypoint (lang=python) — MOBILE-APP PROBE (dto-mf-mobile).

Movement: по ПУБЛІЧНІЙ ссилці на застосунок (App Store / Google Play) дістати метрики
застосунку як характеристики DTO-актора: рейтинг, кількість відгуків, версія (+дата),
кількість завантажень, назва, розробник, ціна, жанр.

Джерела (по-платформенно):
  - iOS  (apps.apple.com / itunes.apple.com):  ПУБЛІЧНИЙ iTunes Lookup API
        https://itunes.apple.com/lookup?id=<id>&country=<cc>  → чистий JSON (надійно).
  - Android (play.google.com/store/apps/details?id=<pkg>):  парсинг публічної сторінки —
        JSON-LD (aggregateRating: ratingValue/ratingCount) + regex по HTML для версії/
        завантажень (Google рендерить частину на клієнті → ці поля часто null: чесно).

НІЧОГО не вигадуємо. Якщо джерело заблокувало датацентр-IP (403/429) або поле не дістали —
поле = null, статус видно у sources_tried, а сирі сніпети (raw_snippets) віддаються процесу
для опційного LLM-fallback (dto-mf-extract дожимає рейтинг/відгуки з тексту сторінки).
Реалістичність ~60% (Google має захисти) — iOS суттєво надійніший за Android.

Контракт (git_call викликає handle(data)):
  IN:  sources (list[str]  — ссилки iOS/Android) АБО url (str) АБО ios_url/android_url
  OUT: data["mobile"] = {
         found, apps[ {
            platform ('ios'|'android'), app_id, country, url,
            found, source, sources_tried[{name,status,blocked,err}],
            title, developer, rating (float|null), reviews_count (int|null),
            version (str|null), version_date (int YYYYMMDD|null), version_date_iso (str|null),
            installs (int|null), installs_text (str|null), price (str|null), genre (str|null),
            size_bytes (int|null), size_text (str|null), content_rating (str|null),
            languages (list[str]|null), languages_count (int|null),
            raw_snippets[str], errors[]
         } ], lib_status{}, budget{}, errors[]
       }

Вікно git_call <=30s: кожен HTTP-запит зі своїм таймаутом; будь-який збій джерела ->
graceful null (НЕ валить вузол).
"""
import re, json, time, traceback
from urllib.parse import urlparse, parse_qs

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

HTTP_TIMEOUT = 8
TIME_BUDGET = 26
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------- helpers ----------------
def _get(url, headers=None):
    out = {"url": url, "status": None, "blocked": False, "text": "", "json": None, "err": None}
    if requests is None:
        out["err"] = "requests-missing"; return out
    h = {"User-Agent": UA, "Accept-Language": "uk,en;q=0.8"}
    if headers:
        h.update(headers)
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers=h)
        out["status"] = r.status_code
        if r.status_code in (401, 403, 429, 451):
            out["blocked"] = True
        elif r.status_code == 200:
            out["text"] = r.text or ""
            ct = (r.headers.get("Content-Type") or "").lower()
            if "json" in ct:
                try:
                    out["json"] = r.json()
                except Exception:
                    out["json"] = None
    except Exception as e:
        out["err"] = "%s: %s" % (type(e).__name__, str(e)[:160])
    return out


def _visible_text(html):
    if not html:
        return ""
    if BeautifulSoup:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.extract()
            return re.sub(r"[ \t\r\f\v\u00a0]+", " ", soup.get_text(" ", strip=True))
        except Exception:
            pass
    return re.sub(r"[ \t\r\f\v\u00a0]+", " ", re.sub(r"<[^>]+>", " ", html))


def _iso_to_yyyymmdd(s):
    if not s:
        return None, None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(s))
    if not m:
        return None, str(s)[:10]
    iso = "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    try:
        return int(m.group(1) + m.group(2) + m.group(3)), iso
    except Exception:
        return None, iso


_MONTHS = {}
for _i, _mn in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul",
                          "aug", "sep", "oct", "nov", "dec"), 1):
    _MONTHS[_mn] = _i
# Ukrainian short month stems (Play may localize despite hl=en on some IPs)
for _uk, _i in (("січ", 1), ("лют", 2), ("бер", 3), ("квіт", 4), ("трав", 5),
                ("черв", 6), ("лип", 7), ("серп", 8), ("вер", 9),
                ("жовт", 10), ("лист", 11), ("груд", 12)):
    _MONTHS[_uk] = _i


def _monthname_to_yyyymmdd(s):
    """'Jun 30, 2026' / '30 Jun 2026' / '30 черв. 2026' -> (20260630, '2026-06-30')."""
    if not s:
        return None, None
    t = str(s).lower()
    mm = re.search(r"([a-zA-Zа-яіїєґ]{3,})", t)
    dd = re.search(r"\b(\d{1,2})\b", t)
    yy = re.search(r"\b(\d{4})\b", t)
    if not (mm and dd and yy):
        return None, str(s)[:12]
    stem = mm.group(1)[:4]
    mon = _MONTHS.get(stem) or _MONTHS.get(stem[:3])
    if not mon:
        return None, str(s)[:12]
    try:
        d = int(dd.group(1)); y = int(yy.group(1))
        if not (1 <= d <= 31 and 1990 <= y <= 2100):
            return None, str(s)[:12]
        return y * 10000 + mon * 100 + d, "%04d-%02d-%02d" % (y, mon, d)
    except Exception:
        return None, str(s)[:12]


def _installs_to_int(txt):
    """'10,000,000+' -> 10000000 ; '1B+' -> 1000000000 ; None on failure."""
    if not txt:
        return None
    t = str(txt).strip()
    mult = 1
    mm = re.search(r"([\d][\d.,\s\u00a0]*)\s*([KMBТтМмКк])?\s*\+?", t)
    if not mm:
        return None
    num = mm.group(1)
    suf = (mm.group(2) or "").upper()
    num = re.sub(r"[\s\u00a0,]", "", num)
    # handle "1.0" style with suffix
    try:
        val = float(num)
    except Exception:
        return None
    if suf in ("K", "К"):
        mult = 1000
    elif suf in ("M", "М"):
        mult = 1000000
    elif suf in ("B", "Т", "Б"):
        mult = 1000000000
    v = val * mult
    return int(v) if v == int(v) else int(round(v))


def _human_mb(size_bytes):
    """Bytes -> human MB string e.g. 160731136 -> '153.3 MB'. None on failure."""
    try:
        mb = float(size_bytes) / (1024.0 * 1024.0)
    except Exception:
        return None
    if mb >= 1024.0:
        return "%.2f GB" % (mb / 1024.0)
    return "%.1f MB" % mb


def _size_to_bytes(num, unit):
    """('45', 'MB') / ('1.2', 'GB') / ('512', 'КБ') -> int bytes. None on failure."""
    try:
        val = float(str(num).replace(" ", "").replace(" ", "").replace(",", "."))
    except Exception:
        return None
    u = (unit or "").strip().upper()
    mult = {"KB": 1024, "КБ": 1024, "MB": 1024 ** 2, "МБ": 1024 ** 2,
            "GB": 1024 ** 3, "ГБ": 1024 ** 3}.get(u)
    if not mult:
        return None
    return int(round(val * mult))


def _new_app(platform, url):
    return {"platform": platform, "app_id": None, "country": None, "url": url,
            "found": False, "source": None, "sources_tried": [],
            "title": None, "developer": None, "rating": None, "reviews_count": None,
            "version": None, "version_date": None, "version_date_iso": None,
            "installs": None, "installs_text": None, "price": None, "genre": None,
            "size_bytes": None, "size_text": None, "content_rating": None,
            "languages": None, "languages_count": None,
            "raw_snippets": [], "errors": []}


# ---------------- iOS: iTunes Lookup API ----------------
def _detect_country(url, default="us"):
    m = re.search(r"apps\.apple\.com/([a-z]{2})/", url or "")
    if m:
        return m.group(1)
    return default


def _probe_ios(url):
    app = _new_app("ios", url)
    m = re.search(r"/id(\d+)", url or "") or re.search(r"[?&]id=(\d+)", url or "")
    if not m:
        app["errors"].append("no-apple-id-in-url"); return app
    app_id = m.group(1)
    app["app_id"] = app_id
    country = _detect_country(url)
    app["country"] = country
    # try requested country, then us as fallback
    tried = [country] + (["us"] if country != "us" else [])
    for cc in tried:
        lk = "https://itunes.apple.com/lookup?id=%s&country=%s" % (app_id, cc)
        g = _get(lk)
        app["sources_tried"].append({"name": "itunes-lookup(%s)" % cc,
                                     "status": g["status"], "blocked": g["blocked"], "err": g["err"]})
        j = g["json"]
        if not j and g["text"]:
            try:
                j = json.loads(g["text"])
            except Exception:
                j = None
        if j and isinstance(j, dict) and j.get("resultCount", 0) > 0:
            r = j["results"][0]
            app["country"] = cc
            app["source"] = "itunes-api"
            app["title"] = r.get("trackName")
            app["developer"] = r.get("sellerName") or r.get("artistName")
            av = r.get("averageUserRating")
            app["rating"] = round(float(av), 2) if isinstance(av, (int, float)) else None
            rc = r.get("userRatingCount")
            app["reviews_count"] = int(rc) if isinstance(rc, (int, float)) else None
            app["version"] = r.get("version")
            yyyymmdd, iso = _iso_to_yyyymmdd(r.get("currentVersionReleaseDate") or r.get("releaseDate"))
            app["version_date"] = yyyymmdd
            app["version_date_iso"] = iso
            # Apple does NOT publish install counts -> honest null
            app["installs"] = None
            price = r.get("formattedPrice")
            app["price"] = price if price else ("Free" if r.get("price") == 0 else None)
            app["genre"] = r.get("primaryGenreName")
            # size (iTunes publishes fileSizeBytes as a string)
            fsb = r.get("fileSizeBytes")
            if fsb is not None:
                try:
                    app["size_bytes"] = int(fsb)
                    app["size_text"] = _human_mb(app["size_bytes"])
                except Exception:
                    pass
            # content / age rating
            app["content_rating"] = r.get("contentAdvisoryRating") or r.get("trackContentRating")
            # languages (ISO2A list)
            langs = r.get("languageCodesISO2A")
            if isinstance(langs, list):
                app["languages"] = langs
                app["languages_count"] = len(langs)
            app["found"] = app["rating"] is not None or app["reviews_count"] is not None or bool(app["version"])
            return app
    return app


# ---------------- Android: Google Play page ----------------
def _iter_jsonld(html):
    out = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         html or "", re.S | re.I):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
            out.append(obj)
        except Exception:
            continue
    return out


def _walk_find_app(obj):
    """find a dict that looks like a SoftwareApplication with aggregateRating."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            t = cur.get("@type") or cur.get("type")
            ts = t if isinstance(t, str) else (" ".join(t) if isinstance(t, list) else "")
            if ("Application" in ts) or ("aggregateRating" in cur):
                return cur
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _probe_android(url):
    app = _new_app("android", url)
    q = parse_qs(urlparse(url or "").query)
    pkg = (q.get("id") or [None])[0]
    if not pkg:
        m = re.search(r"[?&]id=([\w\.\-]+)", url or "")
        pkg = m.group(1) if m else None
    if not pkg:
        app["errors"].append("no-package-id-in-url"); return app
    app["app_id"] = pkg
    gl = re.search(r"[?&]gl=([A-Za-z]{2})", url or "")
    app["country"] = (gl.group(1).upper() if gl else "US")
    page = "https://play.google.com/store/apps/details?id=%s&hl=en&gl=%s" % (pkg, app["country"])
    g = _get(page)
    app["sources_tried"].append({"name": "play-page", "status": g["status"],
                                 "blocked": g["blocked"], "err": g["err"]})
    html = g["text"]
    if not html:
        return app
    app["source"] = "play-jsonld"
    # 1) JSON-LD (rating / reviews / name / author)
    for obj in _iter_jsonld(html):
        node = _walk_find_app(obj)
        if not node:
            continue
        if not app["title"]:
            app["title"] = node.get("name")
        au = node.get("author")
        if au and not app["developer"]:
            app["developer"] = au.get("name") if isinstance(au, dict) else (au if isinstance(au, str) else None)
        ar = node.get("aggregateRating") or {}
        if isinstance(ar, dict):
            rv = ar.get("ratingValue")
            if rv is not None and app["rating"] is None:
                try:
                    app["rating"] = round(float(rv), 2)
                except Exception:
                    pass
            rc = ar.get("ratingCount") or ar.get("reviewCount")
            if rc is not None and app["reviews_count"] is None:
                try:
                    app["reviews_count"] = int(float(str(rc).replace(",", "")))
                except Exception:
                    pass
        op = node.get("offers")
        if isinstance(op, list) and op:
            op = op[0]
        if isinstance(op, dict) and not app["price"]:
            pr = op.get("price")
            if pr in ("0", 0, "0.0"):
                app["price"] = "Free"
            elif pr is not None:
                app["price"] = str(pr) + " " + str(op.get("priceCurrency") or "")
        cat = node.get("applicationCategory") or node.get("genre")
        if cat and not app["genre"]:
            app["genre"] = cat if isinstance(cat, str) else None
        cr = node.get("contentRating")
        if cr and not app["content_rating"]:
            app["content_rating"] = cr if isinstance(cr, str) else None

    # 2) regex fallbacks over raw HTML (Google renders much client-side -> often null)
    if app["rating"] is None:
        m = re.search(r'\[\[\[(\d(?:\.\d+)?)\]', html)  # ds rating blob
        if m:
            try:
                v = float(m.group(1))
                if 0 < v <= 5:
                    app["rating"] = round(v, 2)
            except Exception:
                pass
    # reviews_count \u2014 extra regex over the ds blob near the rating (JSON-LD already tried)
    if app["reviews_count"] is None:
        for pat in (r'(\d[\d,\.\s]*[KMBkmb]?)\s*(?:reviews|reviewers|\u0432\u0456\u0434\u0433\u0443\u043a)',
                    r'"ratingCount"\s*:\s*"?(\d[\d,\.]*)"?',
                    r'\[\s*(\d{3,})\s*,\s*\[\d(?:\.\d+)?\]\]'):
            m = re.search(pat, html, re.I)
            if m:
                rc = _installs_to_int(m.group(1)) if re.search(r'[KMBkmb]', m.group(1)) \
                    else None
                if rc is None:
                    try:
                        rc = int(re.sub(r"[^\d]", "", m.group(1)))
                    except Exception:
                        rc = None
                if rc:
                    app["reviews_count"] = rc
                    break
    # installs
    if app["installs"] is None:
        m = re.search(r'>\s*([\d][\d,\.\s]*[KMBkmb]?\+)\s*</div>\s*<div[^>]*>\s*(?:Downloads|\u0417\u0430\u0432\u0430\u043d\u0442\u0430\u0436)', html)
        if not m:
            m = re.search(r'"(\d[\d.,]*[KMB]?\+?)"[^\]]*\bDownloads\b', html)
        if not m:
            m = re.search(r'([\d][\d,\. ]*[KMBkmb]?\+)\s*(?:downloads|Downloads)', html)
        if not m:
            m = re.search(r'([\d][\d,\.]*[KMB]?\+)\s*downloads', _visible_text(html), re.I)
        if m:
            app["installs_text"] = m.group(1).strip()
            app["installs"] = _installs_to_int(app["installs_text"])
    # version \u2014 Play embeds it in the ds blob: [[["2.335.06"]],[[[35 (verName then verCode)
    if app["version"] is None:
        m = re.search(r'\[\[\["([0-9]+(?:\.[0-9]+){1,3})"\]\]\s*,\s*\[\[\[\d', html)
        if not m:
            m = re.search(r'Current Version.*?([0-9]+(?:\.[0-9]+){1,3})', html, re.S)
        if not m:
            m = re.search(r'\[\["([0-9]+(?:\.[0-9]+){1,3})"\]\]\s*,\s*"[0-9]', html)
        if not m:
            m = re.search(r'(?:Version|\u0412\u0435\u0440\u0441\u0456\u044f)[^\d]{0,40}?"([0-9]+(?:\.[0-9]+){1,3})"', html)
        if m:
            app["version"] = m.group(1)
    # updated date -> version_date : Play HTML "Updated on</div><div ...>Jun 30, 2026</div>"
    if app["version_date"] is None:
        m = re.search(r'(?:Updated on|\u041e\u043d\u043e\u0432\u043b\u0435\u043d\u043e)\s*</div>\s*<div[^>]*>\s*'
                      r'([A-Za-z\u0410-\u042f\u0430-\u044f\u0456\u0457\u0454\u0491]{3,}\.?\s+\d{1,2},?\s*\d{4}'
                      r'|\d{1,2}\s+[A-Za-z\u0410-\u042f\u0430-\u044f\u0456\u0457\u0454\u0491]{3,}\.?\s*\d{4})', html)
        if not m:
            m = re.search(r'(?:Updated on|\u041e\u043d\u043e\u0432\u043b\u0435\u043d\u043e).{0,40}?'
                          r'([A-Za-z\u0410-\u042f\u0430-\u044f\u0456\u0457\u0454\u0491]{3,}\.?\s+\d{1,2},?\s*\d{4})', html, re.S)
        if m:
            yyyymmdd, iso = _monthname_to_yyyymmdd(m.group(1))
            if yyyymmdd:
                app["version_date"] = yyyymmdd
                app["version_date_iso"] = iso
        if app["version_date"] is None:
            m = re.search(r'"(\d{4}-\d{2}-\d{2})T', html)
            if m:
                yyyymmdd, iso = _iso_to_yyyymmdd(m.group(1))
                app["version_date"] = yyyymmdd
                app["version_date_iso"] = iso
    # size \u2014 Play sometimes shows "45 MB" / "1.2 GB" / "512 \u041a\u0411" near "Size"/"\u0420\u043e\u0437\u043c\u0456\u0440"
    if app["size_text"] is None:
        m = re.search(r'(?:Size|\u0420\u043e\u0437\u043c\u0456\u0440)[^\d]{0,40}?([\d.,]+)\s*(MB|GB|\u041a\u0411|\u041c\u0411|\u0413\u0411)', html)
        if not m:
            m = re.search(r'"([\d.,]+)\s*(MB|GB|\u041c\u0411|\u0413\u0411)"', html)
        if not m:
            m = re.search(r'([\d.,]+)\s*(MB|GB|\u041a\u0411|\u041c\u0411|\u0413\u0411)', _visible_text(html))
        if m:
            app["size_text"] = (m.group(1) + " " + m.group(2)).strip()
            app["size_bytes"] = _size_to_bytes(m.group(1), m.group(2))
    # content_rating \u2014 JSON-LD may have set it; else "Rated for N+" / PEGI / USK / ESRB
    if app["content_rating"] is None:
        m = (re.search(r'Rated for (\d+\+)', html)
             or re.search(r'\b(PEGI\s*\d+)\b', html)
             or re.search(r'\b(USK[:\s]*\d+)\b', html)
             or re.search(r'\b(Everyone(?:\s*\d+\+)?|Teen|Mature\s*17\+)\b', html))
        if m:
            app["content_rating"] = m.group(1).strip()
    # genre \u2014 JSON-LD may have set it; else genreId / category text in the blob
    if app["genre"] is None:
        m = (re.search(r'"genreId"\s*:\s*"([^"]+)"', html)
             or re.search(r'/store/apps/category/([A-Z_]+)"', html))
        if m:
            app["genre"] = m.group(1)

    # raw snippet for optional LLM-fallback
    vt = _visible_text(html)
    if vt:
        app["raw_snippets"].append(vt[:1600])
    app["found"] = (app["rating"] is not None or app["reviews_count"] is not None
                    or bool(app["version"]))
    return app


# ---------------- dispatch ----------------
def _detect_platform(url):
    host = (urlparse(url or "").netloc or "").lower()
    if "apple.com" in host or "itunes.apple" in host:
        return "ios"
    if "play.google" in host or "market.android" in host:
        return "android"
    # bare package id -> android
    if re.match(r"^[a-z][\w]*(\.[\w]+){2,}$", (url or "").strip()):
        return "android"
    return None


def _collect_sources(data):
    urls = []
    src = data.get("sources")
    if isinstance(src, str):
        try:
            src = json.loads(src)
        except Exception:
            src = [src]
    if isinstance(src, list):
        for u in src:
            if u:
                urls.append(str(u))
    for k in ("url", "ios_url", "android_url"):
        v = data.get(k)
        if v:
            urls.append(str(v))
    # dedup, keep order
    seen = set(); out = []
    for u in urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out


def _run(urls):
    t0 = time.time()
    apps = []
    errors = []
    for u in urls:
        if (time.time() - t0) >= TIME_BUDGET:
            errors.append("time-budget hit, remaining sources skipped")
            break
        plat = _detect_platform(u)
        try:
            if plat == "ios":
                apps.append(_probe_ios(u))
            elif plat == "android":
                apps.append(_probe_android(u))
            else:
                a = _new_app("unknown", u)
                a["errors"].append("unrecognized-store-url")
                apps.append(a)
        except Exception as e:
            a = _new_app(plat or "unknown", u)
            a["errors"].append("FATAL: %s" % e)
            a["errors"].append(traceback.format_exc()[:600])
            apps.append(a)
    found = any(a.get("found") for a in apps)
    return {"found": found, "apps": apps, "lib_status": LIB, "errors": errors,
            "budget": {"elapsed_s": round(time.time() - t0, 2), "time_budget_s": TIME_BUDGET}}


def handle(data):
    try:
        urls = _collect_sources(data)
        if not urls:
            data["mobile"] = {"found": False, "apps": [], "lib_status": LIB,
                              "errors": ["no sources/url provided"], "budget": {}}
            return data
        data["mobile"] = _run(urls)
    except Exception as e:
        data["mobile"] = {"found": False, "apps": [], "lib_status": LIB,
                          "errors": ["FATAL: %s" % e, traceback.format_exc()[:1500]], "budget": {}}
    return data


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        test_urls = sys.argv[1:]
    else:
        test_urls = [
            "https://apps.apple.com/ua/app/id1287492353",              # monobank iOS
            "https://play.google.com/store/apps/details?id=com.ftband.mono",  # monobank Android
        ]
    out = handle({"sources": test_urls})
    m = out["mobile"]
    for a in m["apps"]:
        a2 = dict(a); a2["raw_snippets"] = ["<%d snippets>" % len(a.get("raw_snippets", []))]
        print(json.dumps(a2, ensure_ascii=False, indent=2))
        print("  >> %s: genre=%r developer=%r version=%r size_text=%r "
              "content_rating=%r languages_count=%r installs=%r reviews_count=%r" % (
                  a.get("platform"), a.get("genre"), a.get("developer"), a.get("version"),
                  a.get("size_text"), a.get("content_rating"), a.get("languages_count"),
                  a.get("installs"), a.get("reviews_count")))
    print("FOUND:", m["found"], "BUDGET:", m["budget"], "LIB:", m["lib_status"])
