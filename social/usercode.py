#!/usr/bin/env python3
"""Corezoid GIT Call entrypoint (lang=python) — SOCIAL PROFILE PROBE (dto-mf-social).

Movement: по ПУБЛІЧНИХ посиланнях соцмереж/ботів компанії (YouTube / Telegram / Instagram /
Facebook / TikTok) дістати публічні метрики каналу: назву, підписників, к-сть дописів/відео,
перегляди. НІЧОГО НЕ ВИГАДУЄ: якщо мережа блокує датацентр-IP (login-wall / 403 / 429) або
метрики не видно публічно — поле = null, а у sources_tried видно реальний статус. Для кожної
цілі повертається raw_snippet (публічний видимий текст сторінки) — процес може за потреби
дожати його синхронним LLM (dto-mf-extract) там, де детермінований парсинг не впорався.

Детермінований bedrock (реально працює на датацентр-IP):
  * Telegram  t.me/<name>   — публічна прев'ю-сторінка: назва каналу + "N subscribers/members".
  * YouTube   /@handle,/channel/ID,/c/,/user/ — ytInitialData: назва + "N subscribers" + к-сть відео.
Мережі із login-wall (Instagram/Facebook/TikTok) — best-effort og:meta, інакше чесний null.

Контракт (git_call викликає handle(data)):
  IN:  social_urls (list|json-str)  — прямий список посилань; АБО
       sources     (obj {youtube:[],telegram:[],instagram:[],facebook:[],tiktok:[],other:[]}) від links-discovery; АБО
       url         (str)            — одне посилання.
  OUT: data["social"] = [ {
         url, platform (youtube|telegram|instagram|facebook|tiktok|unknown),
         entity_class (social-channel|bot), handle, found, blocked,
         title, subscribers (int|null), posts (int|null), views (int|null),
         verified (bool|null), needs_llm (bool),
         sources_tried[{name,status,blocked,err}], raw_snippet (str), errors[]
       } , ... ]
       data["social_summary"] = {targets, found, blocked, subscribers_total, lib_status{}, budget{}}

Вікно git_call <=30s: по ~1 HTTP на ціль, кожен зі своїм таймаутом; будь-який збій цілі ->
graceful null (НЕ валить вузол).
"""
import re, json, time, traceback
from urllib.parse import urlparse, unquote

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
MAX_TARGETS = 8
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------- helpers ----------------
def _human_num(s):
    """'1.2M' / '1,2 млн' / '12.3K' / '1 234' / '2.5 тис.' -> int|None. Non-inventing."""
    if s is None:
        return None
    t = str(s).strip().lower().replace(" ", " ")
    if not t:
        return None
    mult = 1.0
    # explicit multiplier words / suffixes
    if re.search(r"\bмлрд|\bbillion|\bbln\b|[0-9]\s*b\b", t):
        mult = 1e9
    elif re.search(r"\bмлн|\bмільйон|\bmillion|\bmln\b|[0-9]\s*m\b", t):
        mult = 1e6
    elif re.search(r"\bтис\.?|\bтисяч|\bthousand|[0-9]\s*k\b", t):
        mult = 1e3
    # grab first number (allow 1.2 / 1,2 / 1 234 / 1,234)
    m = re.search(r"(\d[\d\s.,]*\d|\d)", t)
    if not m:
        return None
    raw = m.group(1)
    if mult != 1.0:
        # decimal form like 1.2 or 1,2 -> use dot
        raw = raw.replace(" ", "").replace(",", ".")
        try:
            return int(round(float(raw) * mult))
        except Exception:
            return None
    # plain integer with space/comma thousands separators
    raw = re.sub(r"[ ,.](?=\d{3}\b)", "", raw)   # thousands sep
    raw = re.sub(r"[^\d]", "", raw)
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _get(url, cookies=None):
    out = {"url": url, "status": None, "blocked": False, "html": "", "err": None, "final": url}
    if requests is None:
        out["err"] = "requests-missing"; return out
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True,
                         headers={"User-Agent": UA, "Accept-Language": "en,uk;q=0.8",
                                  "Accept": "text/html,application/xhtml+xml"},
                         cookies=cookies or {})
        out["status"] = r.status_code
        out["final"] = r.url
        if r.status_code in (401, 403, 429, 451):
            out["blocked"] = True
        elif r.status_code == 200:
            out["html"] = r.text or ""
    except Exception as e:
        out["err"] = "%s: %s" % (type(e).__name__, str(e)[:160])
    return out


def _text(html, limit=1600):
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


def _meta(html, prop):
    for pat in (r'<meta[^>]+property=["\']%s["\'][^>]+content=["\']([^"\']+)["\']' % re.escape(prop),
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']%s["\']' % re.escape(prop),
                r'<meta[^>]+name=["\']%s["\'][^>]+content=["\']([^"\']+)["\']' % re.escape(prop)):
        m = re.search(pat, html, re.I)
        if m:
            return _unescape(m.group(1).strip())
    return None


def _unescape(s):
    if not s:
        return s
    s = s.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&gt;", ">").replace("&lt;", "<")
    return s


def _platform_of(url):
    h = urlparse(url if re.match(r"^https?://", url, re.I) else "https://" + url).netloc.lower()
    if "youtube.com" in h or "youtu.be" in h:
        return "youtube"
    if "t.me" in h or "telegram.me" in h or "telegram.org" in h:
        return "telegram"
    if "instagram.com" in h:
        return "instagram"
    if "facebook.com" in h or "fb.com" in h or "fb.me" in h:
        return "facebook"
    if "tiktok.com" in h:
        return "tiktok"
    return "unknown"


def _norm(url):
    return url if re.match(r"^https?://", url, re.I) else "https://" + url


def _blank(url, platform):
    return {"url": url, "platform": platform, "entity_class": "social-channel", "handle": None,
            "found": False, "blocked": False, "title": None, "subscribers": None,
            "posts": None, "views": None, "verified": None, "needs_llm": False,
            "sources_tried": [], "raw_snippet": "", "errors": []}


# ---------------- per-platform extractors ----------------
def _do_telegram(url, res):
    # t.me/<name>  (bot if name endswith 'bot'); public preview page
    name = ""
    m = re.search(r"t\.me/(?:s/)?(@?[A-Za-z0-9_]+)", url)
    if m:
        name = m.group(1).lstrip("@")
    res["handle"] = name or None
    if name and name.lower().endswith("bot"):
        res["entity_class"] = "bot"
    g = _get("https://t.me/" + name if name else url)
    res["sources_tried"].append({"name": "t.me", "status": g["status"], "blocked": g["blocked"], "err": g["err"]})
    if g["blocked"]:
        res["blocked"] = True
    if not g["html"]:
        return
    html = g["html"]
    res["raw_snippet"] = _text(html)
    title = None
    mt = re.search(r'tgme_page_title[^>]*>\s*<span[^>]*>(.*?)</span>', html, re.S)
    if mt:
        title = _unescape(re.sub(r"<[^>]+>", "", mt.group(1)).strip())
    # og:title on t.me is "Telegram: Contact @x" for user/bot pages — only trust the channel title
    if not title:
        ot = _meta(html, "og:title")
        if ot and not re.match(r"(?i)^telegram", ot):
            title = ot
    if title:
        res["title"] = title[:200]
    # bot/user contact page (no channel title) -> treat as bot when handle ends with 'bot'
    if not title and res["entity_class"] != "bot":
        res["entity_class"] = "bot" if (name or "").lower().endswith("bot") else "social-channel"
    # "12 345 subscribers" / "12 345 members" / "N підписників"
    me = re.search(r'tgme_page_extra[^>]*>(.*?)</div>', html, re.S)
    extra = _text(me.group(1)) if me else ""
    sub = re.search(r"([\d\s.,]+[KMkmКМ]?)\s*(?:subscribers|members|підписник|подписчик)", extra, re.I)
    if not sub:
        sub = re.search(r"([\d\s.,]+[KMkmКМ]?)\s*(?:subscribers|members|підписник|подписчик)", res["raw_snippet"], re.I)
    if sub:
        res["subscribers"] = _human_num(sub.group(1))
    res["found"] = bool(res["title"])
    if res["found"] and res["subscribers"] is None and res["entity_class"] != "bot":
        res["needs_llm"] = True   # channel exists but count not public/parsed


def _do_youtube(url, res):
    m = re.search(r"(?:youtube\.com/(?:(@[A-Za-z0-9_.-]+)|channel/([A-Za-z0-9_-]+)|c/([^/?#]+)|user/([^/?#]+)))", url)
    handle = None
    if m:
        handle = m.group(1) or m.group(2) or unquote(m.group(3) or "") or unquote(m.group(4) or "")
    res["handle"] = handle
    # hl=en for stable strings; CONSENT cookie to skip EU consent wall
    u = _norm(url)
    sep = "&" if "?" in u else "?"
    g = _get(u + sep + "hl=en&gl=US", cookies={"CONSENT": "YES+cb", "SOCS": "CAI"})
    res["sources_tried"].append({"name": "youtube", "status": g["status"], "blocked": g["blocked"], "err": g["err"]})
    if g["blocked"]:
        res["blocked"] = True
    if not g["html"]:
        return
    html = g["html"]
    title = _meta(html, "og:title")
    if title:
        res["title"] = title[:200]
    res["raw_snippet"] = _text(html)
    # subscribers from ytInitialData / metadata
    sub = None
    for pat in (r'"subscriberCountText":\s*{[^}]*"simpleText":\s*"([^"]+)"',
                r'"subscriberCountText":\s*{[^}]*"content":\s*"([^"]+)"',
                r'"metadataParts":\[\{"text":\{"content":"([^"]*subscriber[^"]*)"',
                r'([\d.,]+[KMB]?)\s*subscribers'):
        mm = re.search(pat, html, re.I)
        if mm:
            sub = mm.group(1); break
    if sub:
        res["subscribers"] = _human_num(sub)
    # video count
    vc = re.search(r'"videosCountText".*?"([\d.,\s]+)\s*videos?"', html, re.I) or \
         re.search(r'([\d.,\s]+)\s*videos?\b', res["raw_snippet"], re.I)
    if vc:
        res["posts"] = _human_num(vc.group(1))
    # total channel views (usually only on /about)
    vw = re.search(r'([\d.,\s]+)\s*views\b', res["raw_snippet"], re.I)
    if vw and res["posts"] is None:
        pass  # a bare "views" on main page is a single-video figure; skip to avoid inventing
    res["found"] = bool(res["title"])
    if res["found"] and res["subscribers"] is None:
        res["needs_llm"] = True


def _do_og_only(url, res, platform):
    """Instagram / Facebook / TikTok — login-walled; best-effort og meta, else honest null."""
    g = _get(_norm(url), cookies={"CONSENT": "YES+"} )
    res["sources_tried"].append({"name": platform, "status": g["status"], "blocked": g["blocked"], "err": g["err"]})
    if g["blocked"]:
        res["blocked"] = True
    if not g["html"]:
        return
    html = g["html"]
    res["raw_snippet"] = _text(html)
    title = _meta(html, "og:title")
    desc = _meta(html, "og:description") or ""
    if title:
        res["title"] = title[:200]
    if platform == "instagram":
        # "1,234 Followers, 56 Following, 78 Posts - ..."
        mf = re.search(r"([\d\s.,]+[KMkm]?)\s*Followers", desc, re.I)
        if mf:
            res["subscribers"] = _human_num(mf.group(1))
        mp = re.search(r"([\d\s.,]+[KMkm]?)\s*Posts", desc, re.I)
        if mp:
            res["posts"] = _human_num(mp.group(1))
    elif platform == "tiktok":
        # HTML JSON: "followerCount":1234  ;  og desc: "... Followers ..."
        mf = re.search(r'"followerCount":\s*(\d+)', html)
        if mf:
            res["subscribers"] = int(mf.group(1))
        else:
            md = re.search(r"([\d\s.,]+[KMkm]?)\s*Followers", desc, re.I)
            if md:
                res["subscribers"] = _human_num(md.group(1))
        mv = re.search(r'"heartCount":\s*(\d+)', html) or re.search(r'"videoCount":\s*(\d+)', html)
        if mv:
            res["posts"] = int(mv.group(1))
    elif platform == "facebook":
        mf = re.search(r"([\d\s.,]+[KMkm]?)\s*(?:people follow this|followers|підписник)", desc + " " + res["raw_snippet"], re.I)
        if mf:
            res["subscribers"] = _human_num(mf.group(1))
    res["found"] = bool(res["title"])
    if res["found"] and res["subscribers"] is None:
        res["needs_llm"] = True


def _profile(url):
    platform = _platform_of(url)
    res = _blank(_norm(url), platform)
    try:
        if platform == "telegram":
            _do_telegram(url, res)
        elif platform == "youtube":
            _do_youtube(url, res)
        elif platform in ("instagram", "facebook", "tiktok"):
            _do_og_only(url, res, platform)
        else:
            g = _get(_norm(url))
            res["sources_tried"].append({"name": "generic", "status": g["status"], "blocked": g["blocked"], "err": g["err"]})
            if g["html"]:
                res["raw_snippet"] = _text(g["html"])
                res["title"] = _meta(g["html"], "og:title")
                res["found"] = bool(res["title"])
                res["needs_llm"] = res["found"]
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
        if x and x not in urls and re.search(r"[A-Za-z0-9]\.[A-Za-z]", x):
            urls.append(x)

    su = data.get("social_urls")
    if isinstance(su, str):
        try:
            su = json.loads(su)
        except Exception:
            su = [su] if su.strip() else []
    if isinstance(su, list):
        for u in su:
            add(u)
    src = data.get("sources")
    if isinstance(src, str):
        try:
            src = json.loads(src)
        except Exception:
            src = {}
    if isinstance(src, dict):
        for k in ("youtube", "telegram", "instagram", "facebook", "tiktok", "other"):
            v = src.get(k)
            if isinstance(v, list):
                for u in v:
                    add(u)
    add(data.get("url"))
    # keep only social-ish or explicitly passed; cap
    return urls[:MAX_TARGETS]


def handle(data):
    t0 = time.time()
    try:
        urls = _collect(data)
        results = []
        for u in urls:
            if (time.time() - t0) >= TIME_BUDGET:
                r = _blank(_norm(u), _platform_of(u))
                r["errors"].append("time-budget-exhausted")
                results.append(r)
                continue
            results.append(_profile(u))
        found = sum(1 for r in results if r["found"])
        blocked = sum(1 for r in results if r["blocked"])
        subs_total = sum((r["subscribers"] or 0) for r in results)
        data["social"] = results
        data["social_summary"] = {"targets": len(results), "found": found, "blocked": blocked,
                                   "subscribers_total": subs_total, "lib_status": LIB,
                                   "budget": {"elapsed_s": round(time.time() - t0, 2),
                                              "time_budget_s": TIME_BUDGET}}
    except Exception as e:
        data["social"] = []
        data["social_summary"] = {"targets": 0, "found": 0, "blocked": 0, "subscribers_total": 0,
                                   "lib_status": LIB, "errors": ["FATAL: %s" % e,
                                                                 traceback.format_exc()[:1200]]}
    return data


if __name__ == "__main__":
    import sys
    test_urls = sys.argv[1:] or ["https://www.youtube.com/@monobank", "https://t.me/monobank"]
    out = handle({"social_urls": test_urls})
    for r in out["social"]:
        rp = dict(r); rp["raw_snippet"] = "<%d chars>" % len(r.get("raw_snippet", ""))
        print(json.dumps(rp, ensure_ascii=False))
    print("SUMMARY:", json.dumps(out["social_summary"], ensure_ascii=False))
