#!/usr/bin/env python3
"""Corezoid GIT Call entrypoint (lang=python) — CF-BYPASS FETCH (fetch).

Movement: r.jina.ai-replacement, но БЕЗ jina API-key. Один URL -> чистый текст + внутр.
ссылки. Пробивает Cloudflare на TLS-слое через curl_cffi (impersonate=chrome). Это
fallback-двойник ATOM FETCH 1880062: тот же контракт вход/выход, чтобы shape-узел процесса
1880062 мог прогнать результат git_call через ту же логику и ответить OK/ERR.

  F1  нормализация url (https://, drop trailing /) + origin.
  F2  curl_cffi.get(impersonate=chrome, timeout, до 2 ретраев с backoff) — TLS-fingerprint
      Chrome проходит Cloudflare там, где обычный requests ловит 403/«Just a moment».
      Мягкий fallback на requests, если curl_cffi недоступен на раннере.
  F3  детект CF-челленджа: короткий html (<1500) И маркеры (just a moment / cloudflare /
      verifying / checking your browser / …) -> ok=false, err='bot-challenge (cloudflare)'.
  F4  trafilatura.extract -> чистый текст (fallback BeautifulSoup -> regex).
  F5  same-origin ссылки по kw-фильтру (about|team|product|contact|... укр/рус/англ), <=25.
  F6  вернуть data.fetch = {ok, url, text(<=60000), title, links[], chars, err}.

git_call window <=30s. Один HTTP GET (+ретраи) — с запасом в бюджет. Watchdog 28s гарантирует
возврат, любая ошибка -> ok=false + err (НИКОГДА не крашит узел — это контракт).

Task data IN:
  url  (str)   URL для загрузки (по нему берётся origin для same-origin фильтра ссылок)

Task data OUT (added under data.fetch):
  ok, url, text(<=60000), title, links[], chars, err, lib_status{}, budget{}
"""
import re, json, time, traceback
from urllib.parse import urlparse, urljoin, urldefrag

# ---- optional deps: degrade + REPORT instead of crashing the whole node --------------
LIB = {}
# curl_cffi — TLS-impersonate Chrome (проходит Cloudflare на TLS-слое). ГЛАВНЫЙ движок.
try:
    from curl_cffi import requests as _CREQ
    import curl_cffi as _ccffi
    LIB["curl_cffi"] = getattr(_ccffi, "__version__", "?")
except Exception as e:
    _CREQ = None; LIB["curl_cffi"] = "ERR:%s" % str(e)[:60]
# requests — мягкий fallback, если curl_cffi недоступен.
try:
    import requests as _REQ
    LIB["requests"] = getattr(_REQ, "__version__", "?")
except Exception as e:
    _REQ = None; LIB["requests"] = "ERR:%s" % str(e)[:60]
# trafilatura — чистый текст из HTML (лучшая экстракция «полезного тела»).
try:
    import trafilatura as _TRAF
    LIB["trafilatura"] = getattr(_TRAF, "__version__", "?")
except Exception as e:
    _TRAF = None; LIB["trafilatura"] = "ERR:%s" % str(e)[:60]
# BeautifulSoup — fallback экстракция текста/заголовка/ссылок.
try:
    from bs4 import BeautifulSoup
    import bs4
    LIB["beautifulsoup4"] = getattr(bs4, "__version__", "?")
except Exception as e:
    BeautifulSoup = None; LIB["beautifulsoup4"] = "ERR:%s" % str(e)[:60]
# lxml — движок парсера bs4/trafilatura (только фиксируем наличие).
try:
    import lxml as _lxml
    LIB["lxml"] = getattr(_lxml, "__version__", "?")
except Exception as e:
    LIB["lxml"] = "ERR:%s" % str(e)[:60]
# signal — жёсткий watchdog, чтобы git_call не висел > платформенного лимита.
try:
    import signal as _signal
    LIB["signal"] = "ok"
except Exception as e:
    _signal = None; LIB["signal"] = "ERR:%s" % str(e)[:60]

# --------------------------------------------------------------------------------------
TIMEOUT     = 25            # per-request seconds (r.jina-узел тоже ставит x-timeout:25)
RETRY       = 2             # до 2 ретраев (итого до 3 попыток), как в 1880062 (fetchRetry<=2)
TEXT_CAP    = 60000         # <=60000 симв чистого текста (контракт 1880062)
LINKS_CAP   = 25            # <=25 внутр.ссылок (контракт 1880062)
IMPERSONATE = "chrome"      # локально доказано: chrome/chrome110/safari равноценно бьют CF на pumb
# Богатый браузерный UA (curl_cffi всё равно шлёт TLS Chrome; UA — для совместимости).
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
_HDRS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.6",
}

# kw-фильтр деловых разделов (укр/рус/англ) — тот же, что в shape-узле 1880062.
KW_RE = re.compile(
    r"(about|team|management|leadership|board|product|service|contact|career|vacanc|"
    r"company|pric|tarif|insur|о-?нас|про-?нас|кер[іи]вництв|команд|продукт|послуг|"
    r"контакт|вакансі|тариф|страхуванн)", re.I)

# CF/interstitial-челлендж: маркеры + короткий html — та же эвристика, что в 1880062.
CF_RE = re.compile(
    r"just a moment|performing security verification|verify you are (not a bot|human)|"
    r"checking your browser|cf-browser-verification|attention required|"
    r"enable javascript and cookies|ddos protection by|cloudflare|captcha", re.I)


def _norm_url(u):
    """https://, drop fragment, drop trailing slash — как init-узел 1880062."""
    u = str(u or "").strip()
    u = re.sub(r"[\s\x00-\x1f]", "", u)   # drop whitespace + control chars
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    u, _ = urldefrag(u)
    u = re.sub(r"/+$", "", u)
    return u


def _origin(u):
    m = re.match(r"^(https?://[^/]+)", u, re.I)
    return m.group(1) if m else ""


def _fetch(url):
    """GET one page — ЛЕСТНИЦА: curl_cffi(TLS-impersonate Chrome, до 2 ретраев+backoff)
    -> requests fallback. Пробивает Cloudflare на TLS-слое.
    Returns (status, html, err)."""
    last = "unknown"
    # Ступень 1: curl_cffi (TLS-impersonate) — ГЛАВНЫЙ движок против Cloudflare
    if _CREQ is not None:
        for attempt in range(RETRY + 1):  # 0,1,2
            try:
                r = _CREQ.get(url, impersonate=IMPERSONATE, timeout=TIMEOUT,
                              headers=_HDRS, allow_redirects=True)
                sc = getattr(r, "status_code", 0)
                if sc == 200 and (r.text or ""):
                    return (sc, r.text, "")
                if sc in (429, 503):
                    try:
                        time.sleep(min(2 ** attempt, 5))  # backoff 1s,2s
                    except Exception:
                        pass
                    last = "curl_cffi-%d" % sc
                    continue
                last = "curl_cffi-%s" % sc
                break
            except Exception as e:
                last = "curl_cffi-fail: %s" % (type(e).__name__)
                try:
                    time.sleep(min(2 ** attempt, 5))
                except Exception:
                    pass
    # Ступень 2: обычный requests — мягкий fallback (если curl_cffi нет на раннере)
    if _REQ is not None:
        try:
            r = _REQ.get(url, timeout=TIMEOUT, headers=_HDRS, allow_redirects=True)
            if r.status_code == 200 and (r.text or ""):
                return (r.status_code, r.text, "")
            last = "requests-%d" % r.status_code
        except Exception as e:
            last = "requests-fail: %s" % (type(e).__name__)
    return (0, "", last)


def _title(html):
    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(html, "html.parser")
            if soup.title:
                return soup.title.get_text(strip=True)[:300]
        except Exception:
            pass
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return (re.sub(r"\s+", " ", m.group(1)).strip()[:300] if m else "")


def _clean_text(url, html):
    """trafilatura.extract -> чистое тело; fallback BeautifulSoup -> regex-strip."""
    if _TRAF is not None:
        try:
            txt = _TRAF.extract(html, url=url, include_comments=False,
                                include_tables=True, favor_recall=True)
            if txt and txt.strip():
                return re.sub(r"\n{3,}", "\n\n", txt).strip()[:TEXT_CAP]
        except Exception:
            pass
    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for t in soup(["script", "style", "noscript", "svg"]):
                t.extract()
            txt = soup.get_text(" ", strip=True)
            return re.sub(r"\s+", " ", txt).strip()[:TEXT_CAP]
        except Exception:
            pass
    txt = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", txt).strip()[:TEXT_CAP]


def _links(origin, html):
    """same-origin <a href>, kw-фильтр деловых разделов, dedup, <=25 — как shape 1880062."""
    hrefs = []
    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(html, "html.parser")
            hrefs = [a.get("href") for a in soup.find_all("a") if a.get("href")]
        except Exception:
            hrefs = re.findall(r'href=["\']([^"\']+)["\']', html or "")
    else:
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', html or "")
    out, seen = [], {}
    for h in hrefs:
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absu = urljoin(origin + "/", h)
        absu, _ = urldefrag(absu)
        absu = re.sub(r"/+$", "", absu)
        if not absu.startswith(("http://", "https://")):
            continue
        if origin and not absu.startswith(origin):
            continue
        if seen.get(absu):
            continue
        seen[absu] = 1
        if KW_RE.search(absu):
            out.append(absu)
        if len(out) >= LINKS_CAP:
            break
    return out


def _do_fetch(raw_url):
    t0 = time.time()
    url = _norm_url(raw_url)
    origin = _origin(url)
    if not url:
        return {"ok": False, "url": "", "text": "", "title": "", "links": [],
                "chars": 0, "err": "no url", "lib_status": LIB,
                "budget": {"elapsed_s": round(time.time() - t0, 2)}}

    status, html, err = _fetch(url)
    if err or not html:
        return {"ok": False, "url": url, "text": "", "title": "", "links": [],
                "chars": 0, "err": err or "empty content", "lib_status": LIB,
                "budget": {"elapsed_s": round(time.time() - t0, 2), "status": status}}

    # CF-челлендж: короткий html + маркеры (та же эвристика, что shape-узел 1880062)
    low = html.lower()
    challenge = (len(html) < 1500) and bool(CF_RE.search(low))
    if challenge:
        return {"ok": False, "url": url, "text": "", "title": "", "links": [],
                "chars": len(html),
                "err": "bot-challenge/interstitial (cloudflare)",
                "lib_status": LIB,
                "budget": {"elapsed_s": round(time.time() - t0, 2), "status": status}}

    text = _clean_text(url, html)
    title = _title(html)
    links = _links(origin, html)
    return {
        "ok": bool(text),
        "url": url,
        "text": text,
        "title": title,
        "links": links,
        "chars": len(html),
        "err": "" if text else "empty content",
        "lib_status": LIB,
        "budget": {"elapsed_s": round(time.time() - t0, 2), "status": status},
    }


def handle(data):
    # WATCHDOG: жёсткий таймаут 28с (< платформенного лимита git_call) — гарантирует ВОЗВРАТ.
    _armed = False
    if _signal is not None:
        def _wd(signum, frame):
            raise TimeoutError("git_call watchdog: exceeded 28s")
        try:
            _signal.signal(_signal.SIGALRM, _wd)
            _signal.alarm(28)
            _armed = True
        except Exception:
            _armed = False
    try:
        url = data.get("url") or data.get("source_url") or ""
        data["fetch"] = _do_fetch(url)
    except TimeoutError as e:
        data["fetch"] = {"ok": False, "url": str(data.get("url") or ""), "text": "",
                         "title": "", "links": [], "chars": 0,
                         "err": "WATCHDOG: %s" % str(e), "lib_status": LIB}
    except Exception as e:
        data["fetch"] = {"ok": False, "url": str(data.get("url") or ""), "text": "",
                         "title": "", "links": [], "chars": 0,
                         "err": "FATAL: %s" % e, "lib_status": LIB,
                         "trace": traceback.format_exc()[:1200]}
    finally:
        if _armed:
            try:
                _signal.alarm(0)
            except Exception:
                pass
    return data


if __name__ == "__main__":
    import sys
    seed = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    out = handle({"url": seed})
    f = out["fetch"]
    print(json.dumps({k: (v if k != "text" else (v[:200] + "…"))
                      for k, v in f.items()}, ensure_ascii=False, indent=2)[:4000])
