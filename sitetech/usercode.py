#!/usr/bin/env python3
"""Corezoid GIT Call entrypoint (lang=python) — DETERMINISTIC SITE TECH PROFILER (site-tech).

Movement: this node ONLY probes a site's TECHNICAL surface. No LLM. It returns hard,
reproducible engineering metrics about the site as a technical asset (domain / whois / SSL /
pages / speed / favicon / robots / sitemap / server tech / language). The Corezoid process
`dto-mf-site-tech` turns this into a `website` actor with characteristic accounts on the twin
graph, and records domain/SSL expiry dates for the calendar-reminder stub.

One git_call window is <=30s. Everything here is a handful of fast HTTP/TLS/WHOIS lookups,
each with its own timeout, so the whole probe stays well under budget. Any single lookup that
fails degrades gracefully to null fields — it NEVER crashes the node (that is the contract:
whois in particular is flaky and may be blocked on the runner).

Task data IN:
  url        (str)   site URL (its registered domain defines the whois/SSL scope)

Task data OUT (added under data.sitetech):
  domain, url, reachable, status_code,
  whois:   {registrar, created, expires, days_left},
  ssl:     {issuer, expires, days_left, valid},
  pages_count,
  favicon: {present, url},
  robots:  {present, url},
  sitemap: {present, url, urls_count},
  speed_ms, title, meta_description,
  tech_hints: {server, x_powered_by, via, cdn},
  lang,
  lib_status{}, budget{}, errors[]
"""
import re, ssl, json, time, socket, traceback
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

# ---- optional deps: degrade + REPORT instead of crashing the whole node --------------
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
    import whois as _whois
    LIB["python-whois"] = getattr(_whois, "__version__", "?")
except Exception as e:
    _whois = None; LIB["python-whois"] = "ERR:%s" % e
try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    import cryptography
    LIB["cryptography"] = getattr(cryptography, "__version__", "?")
except Exception as e:
    x509 = None; NameOID = None; LIB["cryptography"] = "ERR:%s" % e

HTTP_TIMEOUT = 12          # per-request seconds
SSL_TIMEOUT  = 10          # TLS connect seconds
WHOIS_TIMEOUT = 12         # whois socket seconds
TIME_BUDGET  = 26.0        # soft wall-clock ceiling (< 30s node budget)
UA = "dto-mf-sitetech/1.0 (+deterministic tech profiler)"

_now = lambda: datetime.now(timezone.utc)


def _norm_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def _host(u):
    return (urlparse(u).netloc or "").split("@")[-1].split(":")[0].lower()


def _reg_domain(host):
    host = (host or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


# -------------------------------------------------------------------------- homepage GET
def _homepage(url):
    """GET homepage. Returns dict with status, elapsed_ms, headers, html, err."""
    out = {"status": 0, "elapsed_ms": None, "headers": {}, "html": "", "final_url": url, "err": ""}
    if requests is None:
        out["err"] = "requests-missing"; return out
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": UA}, allow_redirects=True)
        out["status"] = r.status_code
        out["elapsed_ms"] = int(r.elapsed.total_seconds() * 1000)
        out["headers"] = {k.lower(): v for k, v in r.headers.items()}
        out["final_url"] = r.url or url
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "html" in ctype or "text" in ctype or not ctype:
            out["html"] = r.text or ""
    except Exception as e:
        out["err"] = "%s: %s" % (type(e).__name__, str(e)[:180])
    return out


def _meta_from_html(html, base_url):
    """title, meta_description, lang, favicon url — via bs4, regex fallback."""
    res = {"title": "", "meta_description": "", "lang": None, "favicon_url": None}
    if not html:
        return res
    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(html, "html.parser")
            if soup.title and soup.title.string:
                res["title"] = soup.title.get_text(strip=True)[:300]
            htag = soup.find("html")
            if htag and htag.get("lang"):
                res["lang"] = str(htag.get("lang")).split("-")[0].lower()[:8]
            md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
            if not md:
                md = soup.find("meta", attrs={"property": re.compile(r"og:description", re.I)})
            if md and md.get("content"):
                res["meta_description"] = md.get("content").strip()[:500]
            icon = None
            for link in soup.find_all("link"):
                rel = " ".join(link.get("rel") or []).lower()
                if "icon" in rel:
                    icon = link.get("href"); break
            if icon:
                res["favicon_url"] = urljoin(base_url, icon)
            return res
        except Exception:
            pass
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        res["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:300]
    m = re.search(r'<html[^>]*\blang=["\']?([a-zA-Z]{2})', html)
    if m:
        res["lang"] = m.group(1).lower()
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.I | re.S)
    if m:
        res["meta_description"] = re.sub(r"\s+", " ", m.group(1)).strip()[:500]
    m = re.search(r'<link[^>]+rel=["\'][^"\']*icon[^"\']*["\'][^>]*href=["\'](.*?)["\']', html, re.I)
    if m:
        res["favicon_url"] = urljoin(base_url, m.group(1))
    return res


# ------------------------------------------------------------------------------- robots
def _robots(base):
    out = {"present": False, "url": urljoin(base, "/robots.txt"), "sitemaps": []}
    if requests is None:
        return out
    try:
        r = requests.get(out["url"], timeout=HTTP_TIMEOUT, headers={"User-Agent": UA})
        if r.status_code == 200 and r.text:
            out["present"] = True
            for m in re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r.text):
                out["sitemaps"].append(m.strip())
    except Exception:
        pass
    return out


# ------------------------------------------------------------------------------ sitemap
def _sitemap(base, robots_sitemaps):
    """Locate a sitemap, count page URLs. Follows one level of sitemap-index."""
    out = {"present": False, "url": None, "urls_count": 0}
    if requests is None:
        return out
    candidates = list(robots_sitemaps) or [urljoin(base, "/sitemap.xml")]
    for sm in candidates[:3]:
        try:
            r = requests.get(sm, timeout=HTTP_TIMEOUT, headers={"User-Agent": UA})
        except Exception:
            continue
        if r.status_code != 200 or not r.text:
            continue
        text = r.text
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
        if not locs:
            continue
        out["present"] = True
        out["url"] = sm
        is_index = "<sitemapindex" in text.lower()
        if is_index:
            total = 0
            for sub in locs[:3]:                      # follow up to 3 sub-sitemaps (budget)
                try:
                    rs = requests.get(sub, timeout=HTTP_TIMEOUT, headers={"User-Agent": UA})
                    total += len(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", rs.text or ""))
                except Exception:
                    pass
            out["urls_count"] = total if total else len(locs)
        else:
            out["urls_count"] = len(locs)
        return out
    return out


def _pages_from_homepage(html, base, domain):
    """Fallback page count: distinct same-domain internal links on homepage."""
    if not html:
        return 0
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    seen = set()
    for h in hrefs:
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absu = urljoin(base, h)
        p = urlparse(absu)
        if p.scheme not in ("http", "https"):
            continue
        if _reg_domain(p.netloc) != domain:
            continue
        path = (p.path or "/").rstrip("/") or "/"
        seen.add(path)
    return len(seen)


def _favicon_ok(url):
    if requests is None or not url:
        return False
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": UA}, stream=True)
        ok = r.status_code == 200 and int(r.headers.get("Content-Length", "1") or "1") != 0
        r.close()
        return ok
    except Exception:
        return False


# --------------------------------------------------------------------------------- SSL
def _ssl_probe(host):
    out = {"issuer": None, "expires": None, "days_left": None, "valid": None, "err": ""}
    if not host:
        out["err"] = "no-host"; return out
    der = None
    valid = False
    # 1) verified handshake — proves chain validity
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=SSL_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True)
                valid = True
    except Exception as e:
        out["err"] = "verify: %s" % (str(e)[:120])
        # 2) unverified fallback — still read the leaf cert for issuer/expiry
        try:
            uctx = ssl._create_unverified_context()
            with socket.create_connection((host, 443), timeout=SSL_TIMEOUT) as sock:
                with uctx.wrap_socket(sock, server_hostname=host) as ss:
                    der = ss.getpeercert(binary_form=True)
        except Exception as e2:
            out["err"] = "connect: %s" % (str(e2)[:120]); return out
    out["valid"] = valid
    if der is None:
        return out
    if x509 is not None:
        try:
            cert = x509.load_der_x509_certificate(der)
            na = getattr(cert, "not_valid_after_utc", None) or \
                cert.not_valid_after.replace(tzinfo=timezone.utc)
            out["expires"] = na.strftime("%Y-%m-%d")
            out["days_left"] = (na - _now()).days
            iss = None
            for oid in (NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME):
                a = cert.issuer.get_attributes_for_oid(oid)
                if a:
                    iss = a[0].value; break
            out["issuer"] = iss
        except Exception as e:
            out["err"] = (out["err"] + " parse:%s" % str(e)[:80]).strip()
    return out


# ------------------------------------------------------------------------------- whois
def _as_dt(v):
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(v[:len(fmt) + 2].strip(), fmt).replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def _whois_probe(domain):
    out = {"registrar": None, "created": None, "expires": None, "days_left": None, "err": ""}
    if _whois is None:
        out["err"] = "python-whois-missing"; return out
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(WHOIS_TIMEOUT)
        w = _whois.whois(domain)
        reg = w.get("registrar") if isinstance(w, dict) else getattr(w, "registrar", None)
        if isinstance(reg, list):
            reg = reg[0] if reg else None
        out["registrar"] = str(reg)[:120] if reg else None
        cd = _as_dt(w.get("creation_date") if isinstance(w, dict) else getattr(w, "creation_date", None))
        ed = _as_dt(w.get("expiration_date") if isinstance(w, dict) else getattr(w, "expiration_date", None))
        if cd:
            out["created"] = cd.strftime("%Y-%m-%d")
        if ed:
            out["expires"] = ed.strftime("%Y-%m-%d")
            out["days_left"] = (ed - _now()).days
        if not (reg or cd or ed):
            out["err"] = "whois-empty"
    except Exception as e:
        out["err"] = "%s: %s" % (type(e).__name__, str(e)[:140])
    finally:
        socket.setdefaulttimeout(old)
    return out


# -------------------------------------------------------------------------------- main
def _profile(url):
    t0 = time.time()
    errors = []
    url = _norm_url(url)
    host = _host(url)
    domain = _reg_domain(host)

    home = _homepage(url)
    reachable = bool(home["status"] and home["status"] < 500 and not home["err"])
    if home["err"]:
        errors.append("homepage: " + home["err"])
    base = home["final_url"] or url

    meta = _meta_from_html(home["html"], base)

    server = home["headers"].get("server")
    xpb = home["headers"].get("x-powered-by")
    via = home["headers"].get("via")
    cdn = None
    for k in ("cf-ray", "x-amz-cf-id", "x-fastly-request-id", "x-vercel-id", "x-cache"):
        if k in home["headers"]:
            cdn = {"cf-ray": "Cloudflare", "x-amz-cf-id": "CloudFront",
                   "x-fastly-request-id": "Fastly", "x-vercel-id": "Vercel"}.get(k, home["headers"][k])
            break

    robots = _robots(base)
    sm = _sitemap(base, robots["sitemaps"])
    pages_count = sm["urls_count"] if sm["present"] and sm["urls_count"] else \
        _pages_from_homepage(home["html"], base, domain)

    fav_url = meta["favicon_url"] or urljoin(base, "/favicon.ico")
    fav_present = bool(meta["favicon_url"]) or _favicon_ok(fav_url)

    sslr = _ssl_probe(host)
    if sslr.get("err"):
        errors.append("ssl: " + sslr["err"])

    whor = _whois_probe(domain)
    if whor.get("err"):
        errors.append("whois: " + whor["err"])

    return {
        "domain": domain,
        "url": url,
        "reachable": reachable,
        "status_code": home["status"],
        "whois": {"registrar": whor["registrar"], "created": whor["created"],
                  "expires": whor["expires"], "days_left": whor["days_left"]},
        "ssl": {"issuer": sslr["issuer"], "expires": sslr["expires"],
                "days_left": sslr["days_left"], "valid": sslr["valid"]},
        "pages_count": int(pages_count or 0),
        "favicon": {"present": bool(fav_present), "url": fav_url if fav_present else None},
        "robots": {"present": robots["present"], "url": robots["url"]},
        "sitemap": {"present": sm["present"], "url": sm["url"], "urls_count": int(sm["urls_count"] or 0)},
        "speed_ms": home["elapsed_ms"],
        "title": meta["title"],
        "meta_description": meta["meta_description"],
        "tech_hints": {"server": server, "x_powered_by": xpb, "via": via, "cdn": cdn},
        "lang": meta["lang"],
        "lib_status": LIB,
        "budget": {"elapsed_s": round(time.time() - t0, 2), "time_budget_s": TIME_BUDGET},
        "errors": errors,
    }


def handle(data):
    try:
        url = data.get("url") or data.get("source_url") or ""
        if not url:
            data["sitetech"] = {"reachable": False, "errors": ["no url"], "lib_status": LIB}
            return data
        data["sitetech"] = _profile(url)
    except Exception as e:
        data["sitetech"] = {"reachable": False, "lib_status": LIB,
                            "errors": ["FATAL: %s" % e, traceback.format_exc()[:1500]]}
    return data


if __name__ == "__main__":
    import sys
    seed = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    out = handle({"url": seed})
    print(json.dumps(out["sitetech"], ensure_ascii=False, indent=2)[:6000])
