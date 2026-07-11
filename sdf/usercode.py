#!/usr/bin/env python3
"""Corezoid GIT Call entrypoint (lang=python) — SDF: structured-data-first product extract.

Крон-камень A1: детерминированное извлечение товаров из structured data ДО LLM.
Каскад: JSON-LD (schema.org Product/ProductGroup/ItemList/Offer) -> Microdata
(itemtype schema.org/Product) -> OpenGraph/meta. LLM НЕ вызывается — это отдельный
fallback в процессе site-run, когда data.sdf.count == 0.

Почему git_call, а не api-узел: raw HTML e-commerce страниц 1-3 МБ; api-узел упирается в
лимит task-size (1 МБ) и http_resp_size (2 МБ). git_call фетчит и парсит В КОНТЕЙНЕРЕ и
возвращает ТОЛЬКО компактные entities (~единицы КБ). Фетч — тот же CF-bypass curl_cffi
(impersonate=chrome), что и в fetch/usercode.py, поэтому от процесса fetch НЕ зависим.

Task data IN:
  url          (str)  URL страницы товара/каталога
  business_hint(str)  контекст-родитель (опц.)
  source_url   (str)  переопределение source_url в entities (опц., по умолчанию = url)

Task data OUT (added under data.sdf):
  ok(bool), count(int), sources{jsonld,microdata,og}, entities[], err, lib_status{}
  entity = {class,key_fields,title,value,confidence,source_url,parent,accounts,
            attrs,source_quote,image_url,links}  — контракт site-run INLINE parse.
"""
import re, json, time, traceback
from urllib.parse import urlparse

LIB = {}
try:
    from curl_cffi import requests as _CREQ
    import curl_cffi as _ccffi
    LIB["curl_cffi"] = getattr(_ccffi, "__version__", "?")
except Exception as e:
    _CREQ = None; LIB["curl_cffi"] = "ERR:%s" % str(e)[:60]
try:
    import requests as _REQ
    LIB["requests"] = getattr(_REQ, "__version__", "?")
except Exception as e:
    _REQ = None; LIB["requests"] = "ERR:%s" % str(e)[:60]

IMPERSONATE = "chrome"
TIMEOUT = 22
RETRY = 2
_HDRS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk,ru;q=0.9,en;q=0.8",
}

MAX_ENTITIES = 300


def _norm_url(u):
    u = re.sub(r"[\s\x00-\x1f]", "", str(u or ""))
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u.rstrip("/")


def _fetch(url):
    """GET one page — curl_cffi(TLS-impersonate Chrome) -> requests fallback."""
    last = "unknown"
    if _CREQ is not None:
        for attempt in range(RETRY + 1):
            try:
                r = _CREQ.get(url, impersonate=IMPERSONATE, timeout=TIMEOUT,
                              headers=_HDRS, allow_redirects=True)
                sc = getattr(r, "status_code", 0)
                if sc == 200 and (r.text or ""):
                    return (sc, r.text, "")
                if sc in (429, 503):
                    try: time.sleep(min(2 ** attempt, 5))
                    except Exception: pass
                    last = "curl_cffi-%d" % sc; continue
                last = "curl_cffi-%s" % sc; break
            except Exception as e:
                last = "curl_cffi-fail:%s" % type(e).__name__
                try: time.sleep(min(2 ** attempt, 5))
                except Exception: pass
    if _REQ is not None:
        try:
            r = _REQ.get(url, timeout=TIMEOUT, headers=_HDRS, allow_redirects=True)
            if r.status_code == 200 and (r.text or ""):
                return (r.status_code, r.text, "")
            last = "requests-%d" % r.status_code
        except Exception as e:
            last = "requests-fail:%s" % type(e).__name__
    return (0, "", last)


# ---------------- schema.org helpers ----------------
def _type_is(t, name):
    if t is None:
        return False
    arr = t if isinstance(t, list) else [t]
    for v in arr:
        v = str(v).lower()
        seg = v.split("/")[-1].split("#")[-1]
        if seg == name.lower():
            return True
    return False


def _img_of(o):
    im = o.get("image")
    if not im:
        return ""
    if isinstance(im, list):
        im = im[0] if im else ""
    if isinstance(im, dict):
        im = im.get("url") or im.get("contentUrl") or ""
    return str(im)[:500]


def _brand_of(o):
    b = o.get("brand")
    if not b:
        return ""
    if isinstance(b, list):
        b = b[0] if b else ""
    if isinstance(b, dict):
        return str(b.get("name") or "")
    return str(b)


def _offer_data(offers):
    r = {"price": "", "currency": "", "availability": ""}
    if not offers:
        return r
    o = offers[0] if isinstance(offers, list) and offers else offers
    if not isinstance(o, dict):
        return r
    p = o.get("price")
    if p in (None, "") and o.get("lowPrice") is not None:
        p = o.get("lowPrice")
    if p in (None, "") and o.get("priceSpecification"):
        ps = o["priceSpecification"]
        ps = ps[0] if isinstance(ps, list) and ps else ps
        if isinstance(ps, dict):
            p = ps.get("price")
    r["price"] = "" if p is None else str(p)
    r["currency"] = str(o.get("priceCurrency") or "")
    av = str(o.get("availability") or "")
    if av:
        r["availability"] = av.split("/")[-1]
    return r


def _price_num(s):
    s = re.sub(r"[^0-9.,]", "", str(s or "")).replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def _make_product(o, via, url, parent_hint):
    name = str(o.get("name") or o.get("title") or "").strip()
    if not name:
        return None
    od = _offer_data(o.get("offers"))
    acc = {}
    pn = _price_num(od["price"])
    if pn is not None:
        acc["Ціна"] = pn
    attrs = {}
    if od["currency"]:
        attrs["Валюта"] = od["currency"]
    if od["availability"]:
        attrs["Наявність"] = od["availability"]
    if o.get("sku"):
        attrs["SKU"] = str(o["sku"])
    br = _brand_of(o)
    if br:
        attrs["Бренд"] = br
    if o.get("category"):
        attrs["Категорія"] = str(o["category"])[:120]
    desc = str(o.get("description") or "")[:300]
    return {
        "class": "product",
        "key_fields": {"title": name[:250]},
        "title": name[:250],
        "value": desc or od["price"] or name[:250],
        "confidence": 1.0,
        "source_url": url,
        "parent": br or parent_hint,
        "accounts": acc,
        "attrs": attrs,
        "source_quote": ("[%s] %s%s" % (via, name, (" — %s %s" % (od["price"], od["currency"])) if od["price"] else ""))[:300],
        "image_url": _img_of(o),
        "links": [],
    }


def _extract_jsonld(html, url, parent_hint):
    out, n = [], {"jsonld": 0}
    seen = set()

    def push(e):
        if not e:
            return
        k = (e["class"] + "|" + (e.get("title") or "")).lower()
        if not e.get("title") or k in seen:
            return
        seen.add(k)
        out.append(e)

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
            return
        if not isinstance(o, dict):
            return
        if isinstance(o.get("@graph"), list):
            walk(o["@graph"])
        t = o.get("@type")
        if _type_is(t, "product") or _type_is(t, "productgroup") or _type_is(t, "vehicle"):
            e = _make_product(o, "json-ld", url, parent_hint)
            if e:
                n["jsonld"] += 1
                push(e)
            hv = o.get("hasVariant")
            if hv:
                arr = hv if isinstance(hv, list) else [hv]
                gp = e["title"] if e else parent_hint
                for v in arr[:MAX_ENTITIES]:
                    ve = _make_product(v, "json-ld/variant", url, parent_hint)
                    if ve:
                        ve["parent"] = gp
                        n["jsonld"] += 1
                        push(ve)
        if _type_is(t, "itemlist"):
            items = o.get("itemListElement")
            if isinstance(items, list):
                for it in items:
                    node = it.get("item") if isinstance(it, dict) and it.get("item") else it
                    if isinstance(node, dict) and (_type_is(node.get("@type"), "product") or _type_is(node.get("@type"), "productgroup")):
                        pe = _make_product(node, "json-ld/itemlist", url, parent_hint)
                        if pe:
                            n["jsonld"] += 1
                            push(pe)

    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
        raw = m.group(1)
        raw = raw.replace("<![CDATA[", "").replace("]]>", "").strip()
        if raw and ord(raw[0]) == 65279:
            raw = raw[1:]
        parsed = None
        try:
            parsed = json.loads(raw)
        except Exception:
            try:
                a, b = raw.find("{"), raw.rfind("}")
                if a >= 0 and b > a:
                    parsed = json.loads(raw[a:b + 1])
            except Exception:
                parsed = None
        if parsed is not None:
            walk(parsed)
    return out, n["jsonld"]


def _extract_microdata(html, url, parent_hint):
    out, cnt = [], 0
    scopes = list(re.finditer(r'itemtype=["\'][^"\']*schema\.org/Product["\']', html, re.I))
    for sm in scopes[:50]:
        chunk = html[sm.start():sm.start() + 8000]

        def prop(name):
            r = re.search(r'itemprop=["\']%s["\'][^>]*content=["\']([^"\']*)["\']' % name, chunk, re.I)
            if r:
                return r.group(1)
            r2 = re.search(r'itemprop=["\']%s["\'][^>]*>([^<]{1,200})<' % name, chunk, re.I)
            return r2.group(1).strip() if r2 else ""
        nm = prop("name")
        if not nm:
            continue
        acc = {}
        pn = _price_num(prop("price"))
        if pn is not None:
            acc["Ціна"] = pn
        attrs = {}
        cur = prop("priceCurrency")
        if cur:
            attrs["Валюта"] = cur
        av = prop("availability")
        if av:
            attrs["Наявність"] = av.split("/")[-1]
        cnt += 1
        out.append({
            "class": "product", "key_fields": {"title": nm[:250]}, "title": nm[:250],
            "value": (prop("description")[:300] or prop("price") or nm[:250]),
            "confidence": 0.9, "source_url": url, "parent": prop("brand") or parent_hint,
            "accounts": acc, "attrs": attrs, "source_quote": ("[microdata] %s" % nm)[:300],
            "image_url": prop("image")[:500], "links": [],
        })
    return out, cnt


def _extract_og(html, url, parent_hint):
    def meta(prop):
        r = re.search(r'<meta[^>]*(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)["\']' % re.escape(prop), html, re.I)
        if r:
            return r.group(1)
        r2 = re.search(r'<meta[^>]*content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(prop), html, re.I)
        return r2.group(1) if r2 else ""
    og_type = meta("og:type").lower()
    title = meta("og:title")
    p_amt = meta("product:price:amount") or meta("og:price:amount")
    p_cur = meta("product:price:currency") or meta("og:price:currency")
    if not title or not ("product" in og_type or p_amt):
        return [], 0
    acc = {}
    pn = _price_num(p_amt)
    if pn is not None:
        acc["Ціна"] = pn
    attrs = {}
    if p_cur:
        attrs["Валюта"] = p_cur
    av = meta("product:availability")
    if av:
        attrs["Наявність"] = av
    return [{
        "class": "product", "key_fields": {"title": title[:250]}, "title": title[:250],
        "value": (meta("og:description")[:300] or p_amt or title[:250]),
        "confidence": 0.7, "source_url": url, "parent": meta("og:site_name") or parent_hint,
        "accounts": acc, "attrs": attrs, "source_quote": ("[og] %s" % title)[:300],
        "image_url": meta("og:image")[:500], "links": [],
    }], 1


def _run_sdf(data):
    url = _norm_url(data.get("url") or data.get("curUrl") or data.get("source_url") or "")
    src_url = str(data.get("source_url") or url)
    parent_hint = str(data.get("business_hint") or data.get("cName") or "")
    res = {"ok": False, "count": 0, "sources": {"jsonld": 0, "microdata": 0, "og": 0},
           "entities": [], "err": "", "lib_status": LIB}
    if not url:
        res["err"] = "no url"
        return res
    sc, html, ferr = _fetch(url)
    if not html:
        res["err"] = "fetch failed: %s" % ferr
        return res
    # tier 1: JSON-LD
    ents, c1 = _extract_jsonld(html, src_url, parent_hint)
    res["sources"]["jsonld"] = c1
    # tier 2: microdata (only if JSON-LD empty)
    if not ents:
        ents, c2 = _extract_microdata(html, src_url, parent_hint)
        res["sources"]["microdata"] = c2
    # tier 3: OpenGraph (only if still empty)
    if not ents:
        ents, c3 = _extract_og(html, src_url, parent_hint)
        res["sources"]["og"] = c3
    ents = ents[:MAX_ENTITIES]
    res["entities"] = ents
    res["count"] = len(ents)
    res["ok"] = len(ents) > 0
    return res


def handle(data):
    if not isinstance(data, dict):
        data = {}
    try:
        data["sdf"] = _run_sdf(data)
    except Exception as e:
        data["sdf"] = {"ok": False, "count": 0, "sources": {"jsonld": 0, "microdata": 0, "og": 0},
                       "entities": [], "err": "fatal:%s" % str(e)[:200],
                       "trace": traceback.format_exc()[-400:], "lib_status": LIB}
    return data


# local test
if __name__ == "__main__":
    import sys
    d = {"url": sys.argv[1] if len(sys.argv) > 1 else "https://www.manduka.com/products/prolite-yoga-mat",
         "business_hint": "Manduka"}
    out = handle(d)["sdf"]
    print("ok=%s count=%s sources=%s err=%s" % (out["ok"], out["count"], out["sources"], out["err"]))
    for e in out["entities"][:5]:
        print("  •", e["title"], "| acc:", e["accounts"], "| attrs:", e["attrs"])
    print("  ...(+%d)" % max(0, out["count"] - 5))
