#!/usr/bin/env python3
"""Corezoid GIT Call entrypoint (lang=python) — OPEN-REGISTRY PROBE (dto-mf-registry).

Movement: недетермінований збір реєстрових даних української компанії з ПУБЛІЧНИХ джерел
(clarity-project.info / opendatabot / youcontrol публічні сторінки) + сайт компанії. Повертає
жорсткі реєстрові факти (ЄДРПОУ, форма власності, статутний капітал, засновники/бенефіціари,
керівник, КВЕД, дата реєстрації, публічні судові згадки). НІЧОГО не вигадує: якщо джерело
недоступне (реєстри часто блокують датацентр-IP -> 403/429) або факту нема — поле = null, а
у sources_tried видно статус. Сирі сніпети (raw_snippets) віддаються для LLM-fallback
(процес може дожати їх синхронним litellm, коли реєстр закритий).

Контракт (git_call викликає handle(data)):
  IN:  company_name (str, опц.), edrpou (str, опц. 8 цифр), site_url (str, опц.)
  OUT: data["registry"] = {
         found, source, sources_tried[{name,status,blocked}],
         edrpou, full_name, ownership_form, capital_uah, kved, kved_text,
         registration_date, director,
         beneficiaries[{name,share_pct}], founders[{name,share_pct,capital_uah}],
         court_cases[{number,note}], raw_snippets[str], lib_status{}, budget{}, errors[]
       }

Вікно git_call <=30s: кілька швидких HTTP-запитів, кожен зі своїм таймаутом; будь-який збій
джерела -> graceful null (НЕ валить вузол). Це і є контракт (реалістичність ~60%).
"""
import re, json, time, traceback
from urllib.parse import quote

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
def _num(s):
    """'4 780 594 950,00 грн' -> 4780594950.0 ; None on failure."""
    if s is None:
        return None
    t = re.sub(r"[^0-9,\.]", "", str(s))
    if not t:
        return None
    # 1 234 567,89  ->  1234567.89   |   1,234,567.89 -> 1234567.89
    if "," in t and "." in t:
        t = t.replace(",", "") if t.rfind(",") < t.rfind(".") else t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        v = float(t)
        return int(v) if v == int(v) else v
    except Exception:
        return None


def _text(html):
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


def _get(url):
    out = {"url": url, "status": None, "blocked": False, "html": "", "err": None}
    if requests is None:
        out["err"] = "requests-missing"; return out
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": UA,
                          "Accept-Language": "uk,en;q=0.8"})
        out["status"] = r.status_code
        if r.status_code in (401, 403, 429, 451):
            out["blocked"] = True
        elif r.status_code == 200:
            out["html"] = r.text or ""
    except Exception as e:
        out["err"] = "%s: %s" % (type(e).__name__, str(e)[:160])
    return out


def _first(patterns, text, group=1):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            try:
                return m.group(group).strip()
            except Exception:
                return m.group(0).strip()
    return None


# ---------------- field extractors over visible text ----------------
def _is_pib(nm):
    if not nm or not isinstance(nm, str):
        return False
    nm = nm.strip()
    if len(nm) < 6 or len(nm) > 70:
        return False
    if re.search(r"[\d:\"'{}\[\]<>/=]", nm):
        return False
    words = nm.split()
    if len(words) < 2 or len(words) > 4:
        return False
    # every word starts uppercase Cyrillic/Latin
    for w in words:
        if not re.match(r"^[А-ЯҐЄІЇA-Z][а-яґєіїa-z'’.-]+$", w):
            return False
    bad = ["частк", "власник", "доступ", "пакет", "height", "width", "company", "компан"]
    low = nm.lower()
    if any(b in low for b in bad):
        return False
    return True


def _extract_fields(text, reg):
    if not text:
        return
    # ЄДРПОУ (8 digits) — near a label to avoid random numbers
    if not reg.get("edrpou"):
        v = _first([r"ЄДРПОУ[^0-9]{0,20}(\d{8})",
                    r"EDRPOU[^0-9]{0,20}(\d{8})",
                    r"USREOU[^0-9]{0,20}(\d{8})",
                    r"код[^0-9]{0,12}(\d{8})"], text)
        if v:
            reg["edrpou"] = v
    # Statutory capital — integer part only (drop kopecks), sanity-clamp
    if reg.get("capital_uah") is None:
        m = re.search(r"(?:статутн\w*\s+капітал|authorized\s+capital|розмір\s+статутного\s+капіталу)[^0-9]{0,30}(\d[\d\s]{2,18}\d)(?:[.,]\d{2})?", text, re.I)
        if m:
            n = _num(re.sub(r"[.,]\d{2}$", "", m.group(1)))
            if n and 1e5 <= n <= 5e11:
                reg["capital_uah"] = n

    # Ownership form
    if not reg.get("ownership_form"):
        v = _first([r"(Акціонерне товариство|Приватне акціонерне товариство|Публічне акціонерне товариство|Товариство з обмеженою відповідальністю|Державне підприємство|Приватне підприємство|Joint[- ]Stock Company|Limited Liability Company)"], text)
        if v:
            reg["ownership_form"] = v
    # KVED main
    if not reg.get("kved"):
        m = re.search(r"(\d{2}\.\d{2})\s*[—\-–]?\s*([А-ЯҐЄІЇа-яґєії][^\n<]{4,80})", text)
        if m:
            reg["kved"] = m.group(1)
            reg.setdefault("kved_text", m.group(2).strip()[:80])
    # Registration date dd.mm.yyyy
    if not reg.get("registration_date"):
        v = _first([r"(?:дата\s+реєстрац\w*|registration\s+date|зареєстровано)[^0-9]{0,20}(\d{2}\.\d{2}\.\d{4})",
                    r"(\d{2}\.\d{2}\.\d{4})\s*\("], text)
        if v:
            reg["registration_date"] = v
    # Director / керівник
    if not reg.get("director"):
        v = _first([r"(?:керівник|директор|голова правління)[^А-ЯҐЄІЇ]{0,20}([А-ЯҐЄІЇ][а-яґєії']+ [А-ЯҐЄІЇ][а-яґєії']+(?: [А-ЯҐЄІЇ][а-яґєії']+)?)"], text)
        if v:
            reg["director"] = v if _is_pib(v) else None


def _clean_owner_name(nm):
    if not nm:
        return nm
    # strip common youcontrol label noise around the real org/person name
    nm = re.sub(r"^(Акціонери з великими частками|Власники крупних пакетів акцій|Учасники|Засновники)\s*", "", nm, flags=re.I)
    nm = re.sub(r"\s*(Частка|Частка \(%\)|станом на).*$", "", nm, flags=re.I).strip()
    nm = re.sub(r"\s{2,}", " ", nm).strip(" -\u2013\u2014:")
    return nm


def _extract_owners(text, reg):
    """Beneficiaries + founders with share % where present."""
    if not text:
        return
    # Beneficiary block
    for m in re.finditer(r"(?:кінцев\w*\s+бенефіціарн\w*\s+власник\w*|beneficial\s+owner)[:\-\s]{0,3}([А-ЯҐЄІЇ][а-яґєії']+ [А-ЯҐЄІЇ][а-яґєії']+(?: [А-ЯҐЄІЇ][а-яґєії']+)?)", text, re.I):
        nm = _clean_owner_name(m.group(1))
        if _is_pib(nm) and not any(b["name"] == nm for b in reg["beneficiaries"]):
            reg["beneficiaries"].append({"name": nm, "share_pct": None})
    # Founder/participant with percent:  "<Name/ТОВ ...> — 92,3423%"  or "... 92.34 %"
    for m in re.finditer(r"([А-ЯҐЄІЇA-Z][^,;\n\d]{3,70}?)\s*[—\-–]?\s*(\d{1,3}[.,]\d{1,4}|\d{1,3})\s*%", text):
        nm = _clean_owner_name(m.group(1))
        pct = _num(m.group(2))
        if len(nm) < 4 or pct is None or pct > 100:
            continue
        is_company = bool(re.search(r"(ТОВ|ПАТ|АТ|ПрАТ|ПП|LLC|Ltd|Limited|Holdings|Finance|Фінанс)", nm, re.I))
        if not (_is_pib(nm) or is_company):
            continue
        if not any(f["name"] == nm for f in reg["founders"]):
            reg["founders"].append({"name": nm, "share_pct": pct, "capital_uah": None})


# ---------------- source drivers ----------------
def _probe(reg, name, url):
    g = _get(url)
    reg["sources_tried"].append({"name": name, "status": g["status"],
                                 "blocked": g["blocked"], "err": g["err"]})
    if g["html"]:
        txt = _text(g["html"])
        if txt:
            reg["raw_snippets"].append(("[%s] " % name) + txt[:1400])
        _extract_fields(txt, reg)
        _extract_owners(txt, reg)
        if not reg.get("source") and (reg.get("edrpou") or reg.get("capital_uah") or reg["founders"]):
            reg["source"] = name
        return True
    return False


def _run(company_name, edrpou, site_url):
    t0 = time.time()
    reg = {"found": False, "source": None, "sources_tried": [],
           "edrpou": (edrpou or None), "full_name": (company_name or None),
           "ownership_form": None, "capital_uah": None, "kved": None, "kved_text": None,
           "registration_date": None, "director": None,
           "beneficiaries": [], "founders": [], "court_cases": [],
           "raw_snippets": [], "lib_status": LIB, "errors": [], "budget": {}}

    def budget_ok():
        return (time.time() - t0) < TIME_BUDGET

    ed = re.sub(r"\D", "", edrpou or "")
    # 1) clarity-project — by EDRPOU (public company card)
    if ed and budget_ok():
        _probe(reg, "clarity-project", "https://clarity-project.info/edr/%s" % ed)
    # 2) youcontrol public company card (UA + EN mirror)
    if ed and budget_ok():
        _probe(reg, "youcontrol", "https://youcontrol.com.ua/catalog/company_details/%s/" % ed)
    if ed and budget_ok() and reg.get("capital_uah") is None:
        _probe(reg, "youcontrol-en", "https://youcontrol.com.ua/en/catalog/company_details/%s/" % ed)
    # 3) opendatabot public
    if ed and budget_ok():
        _probe(reg, "opendatabot", "https://opendatabot.ua/c/%s" % ed)
    # 4) company own disclosure/about page (fallback source of registry facts)
    if site_url and budget_ok():
        su = site_url if site_url.startswith("http") else "https://" + site_url
        _probe(reg, "company-site", su)

    reg["found"] = bool(reg.get("edrpou") or reg.get("capital_uah") or reg["founders"]
                        or reg["beneficiaries"])
    reg["budget"] = {"elapsed_s": round(time.time() - t0, 2), "time_budget_s": TIME_BUDGET}
    # trim
    reg["raw_snippets"] = reg["raw_snippets"][:5]
    return reg


def handle(data):
    try:
        cn = data.get("company_name") or data.get("cName") or ""
        ed = data.get("edrpou") or ""
        su = data.get("site_url") or data.get("url") or ""
        if not (cn or ed or su):
            data["registry"] = {"found": False, "errors": ["no company_name/edrpou/site_url"],
                                 "lib_status": LIB, "sources_tried": []}
            return data
        data["registry"] = _run(cn, ed, su)
    except Exception as e:
        data["registry"] = {"found": False, "lib_status": LIB,
                            "errors": ["FATAL: %s" % e, traceback.format_exc()[:1500]]}
    return data


if __name__ == "__main__":
    import sys
    ed = sys.argv[1] if len(sys.argv) > 1 else "14282829"
    cn = sys.argv[2] if len(sys.argv) > 2 else "АТ ПУМБ"
    su = sys.argv[3] if len(sys.argv) > 3 else "https://about.pumb.ua"
    out = handle({"edrpou": ed, "company_name": cn, "site_url": su})
    r = out["registry"]
    r_print = dict(r); r_print["raw_snippets"] = ["<%d snippets>" % len(r.get("raw_snippets", []))]
    print(json.dumps(r_print, ensure_ascii=False, indent=2)[:5000])
