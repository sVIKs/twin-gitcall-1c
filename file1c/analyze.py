#!/usr/bin/env python3
"""Universal 1C artifact analyzer (gitcall-style).

Input:  one or more file paths OR URLs (downloaded automatically).
Output: per-file uniform JSON describing the structure found + metrics, so the
        "what to migrate" step can offer a PER-FILE, dynamic choice (no hardcodes,
        any number of files). Same JSON is the contract the twin-builder consumes.

Formats handled (detected by signature + extension):
  - EnterpriseData XML  (.xml)           : parsed directly (data + structure)
  - 1C file base        (.1CD)           : onec_dtools via TwinExtractor (data + structure)
  - 1C IB dump          (.dt 1CIBDmpF)   : offline inflate → structure names + data presence (approx)
  - 1C configuration    (.cf FFFFFF7F)   : container → structure object names (no data, by design)

Uniform per-file schema:
  { file, source, bytes, sha256, format, hasData, summary, notes[],
    entities: [ {key, name, kind, count, fields:[...], links:[...], migratable} ],
    metrics:  {entities, records, fields, links, currencies, registers} }
"""
import sys, os, re, json, zlib, hashlib, collections, urllib.request, tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ---------- io ----------
def fetch(src):
    """Return a local path for a path-or-URL (downloads URLs to a temp file)."""
    if re.match(r"^https?://", src, re.I):
        fd, tmp = tempfile.mkstemp(suffix="_" + os.path.basename(src.split("?")[0]))
        os.close(fd)
        urllib.request.urlretrieve(src, tmp)
        return tmp, True
    return src, False


def sha16(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()[:16]


def detect_format(path):
    with open(path, "rb") as f:
        head = f.read(8)
    ext = (os.path.splitext(path)[1] or "").lower().lstrip(".")
    if head[:5] == b"<?xml" or ext == "xml":
        return "xml"
    if head[:8] == b"1CDBMSV8":
        return "1cd"
    if head[:8] == b"1CIBDmpF":
        return "dt"
    if head[:4] == b"\xff\xff\xff\x7f":
        return "cf"            # 1Cv8 container (configuration)
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if ext in ("1cd", "dt", "cf", "cfe", "xml", "xlsx", "csv"):
        return {"cfe": "cf"}.get(ext, ext)
    return "unknown"


# ---------- helpers ----------
def _local(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def scan_inflate(raw, min_chunk=200):
    """Scan raw bytes, raw-inflate (wbits=-15) every deflate stream found.
    Robust to unknown stream offsets (1C .cf/.dt containers)."""
    out, i, n = [], 0, len(raw)
    while i < n - 4:
        try:
            d = zlib.decompressobj(-15)
            chunk = d.decompress(raw[i:])
            if len(chunk) > min_chunk:
                out.append(chunk)
                i += (n - i) - len(d.unused_data)
                continue
        except Exception:
            pass
        i += 1
    return b"".join(out)


_META_RE = re.compile(
    r"(Справочник|Документ|РегистрСведений|РегистрНакопления|РегистрБухгалтерии|"
    r"ПланВидовХарактеристик|Перечисление|БизнесПроцесс|Задача|ПланСчетов|Константа)\."
    r"([A-Za-zА-Яа-яЁёІіЇїЄєҐґ_][\w]{1,60})")
_KIND_MAP = {"Справочник": "catalog", "Документ": "document", "РегистрСведений": "info_register",
             "РегистрНакопления": "accum_register", "РегистрБухгалтерии": "acc_register",
             "ПланВидовХарактеристик": "char_plan", "Перечисление": "enum",
             "БизнесПроцесс": "bp", "Задача": "task", "ПланСчетов": "coa", "Константа": "const"}
_FIO_RE = re.compile(
    r"[А-ЯЁІЇЄҐ][а-яёіїєґ]+ [А-ЯЁІЇЄҐ][а-яёіїєґ]+ "
    r"[А-ЯЁІЇЄҐ][а-яёіїєґ]+(?:ович|евич|йович|ьич|ич|овна|ївна|евна|инична|ічна|ична)")


def metadata_objects(text):
    """Distinct metadata object names found in decompressed config text (universal)."""
    seen = collections.OrderedDict()
    for kind_ru, name in _META_RE.findall(text):
        key = kind_ru + "." + name
        if key not in seen:
            seen[key] = {"key": key, "name": name, "kind": _KIND_MAP.get(kind_ru, "object"), "kind_ru": kind_ru}
    return list(seen.values())


def _empty(file, source, fmt, **extra):
    r = {"file": os.path.basename(file), "source": source, "bytes": os.path.getsize(file),
         "sha256": sha16(file), "format": fmt, "hasData": False, "summary": "", "notes": [],
         "entities": [], "metrics": {"entities": 0, "records": 0, "fields": 0, "links": 0,
                                     "currencies": 0, "registers": 0}}
    r.update(extra)
    return r


# ---------- per-format analyzers ----------
def analyze_xml(path, source):
    r = _empty(path, source, "xml-enterprisedata")
    root = ET.parse(path).getroot()
    body = None
    for el in root.iter():
        if _local(el.tag) == "Body":
            body = el
            break
    body = body if body is not None else root
    groups = collections.OrderedDict()   # entity tag -> list of element
    for child in list(body):
        tag = _local(child.tag)
        groups.setdefault(tag, []).append(child)
    total_records, total_fields, total_links = 0, 0, 0
    for tag, els in groups.items():
        fields, links = collections.OrderedDict(), 0
        for el in els:
            for leaf in el.iter():
                lt = _local(leaf.tag)
                if lt == "Ссылка":
                    links += 1
                elif leaf.text and leaf.text.strip() and lt != tag:
                    fields[lt] = True
        kind_ru = tag.split(".", 1)[0] if "." in tag else "Объект"
        name = tag.split(".", 1)[1] if "." in tag else tag
        r["entities"].append({
            "key": tag, "name": name, "kind": _KIND_MAP.get(kind_ru, "object"),
            "count": len(els), "fields": list(fields.keys()), "links": links, "migratable": True})
        total_records += len(els)
        total_fields += len(fields)
        total_links += links
    r["hasData"] = total_records > 0
    r["metrics"] = {"entities": len(r["entities"]), "records": total_records,
                    "fields": total_fields, "links": total_links, "currencies": 0, "registers": 0}
    r["summary"] = "EnterpriseData XML: %d сутностей, %d записів, %d зв'язків" % (
        len(r["entities"]), total_records, total_links)
    return r


def _table_count(ex, phys):
    try:
        return len(ex.tables[phys])
    except Exception:
        try:
            import onec_dtools
            with open(ex.path, "rb") as f2:
                return sum(1 for _ in onec_dtools.DatabaseReader(f2).tables[phys])
        except Exception:
            return None


def analyze_1cd(path, source):
    import extractor as EX
    r = _empty(path, source, "1cd")
    ex = EX.TwinExtractor(path)
    total_records, total_fields, total_links = 0, 0, 0
    for o in ex.objects():
        cnt = _table_count(ex, o["phys"])
        flds = [f["title"] for f in o["form_fields"]]
        links = [{"name": rr["name"], "via": rr["col"]} for rr in o["rref"]]
        kind = {"ref": "catalog", "doc": "document", "vt": "doc_rows", "info": "info_register"}.get(o["kind"], o["kind"])
        r["entities"].append({
            "key": o["scope"], "name": o["title"], "kind": kind,
            "count": cnt if cnt is not None else 0, "fields": flds,
            "links": [l["name"] for l in links], "migratable": True})
        total_records += cnt or 0
        total_fields += len(flds)
        total_links += len(links)
    regs = ex.registers()
    for rg in regs:
        cnt = _table_count(ex, rg["phys"])
        r["entities"].append({
            "key": "reg%d" % rg["idx"], "name": rg["name"], "kind": "accum_register",
            "count": cnt if cnt is not None else 0,
            "fields": [n for _, n, _ in rg["resources"]], "links": list(rg["dim_names"].values()),
            "migratable": True})
        total_records += cnt or 0
    currencies = len(ex.currency_names())
    ex.close()
    r["hasData"] = total_records > 0
    r["metrics"] = {"entities": len(r["entities"]), "records": total_records,
                    "fields": total_fields, "links": total_links,
                    "currencies": currencies, "registers": len(regs)}
    r["summary"] = "1CD: %d об'єктів, %d записів, %d регістрів, %d валют" % (
        len(r["entities"]), total_records, len(regs), currencies)
    return r


def _analyze_container(path, source, fmt):
    """cf / dt: decompress container, enumerate structure objects (universal),
    detect data presence (FIO / record markers) offline."""
    raw = open(path, "rb").read()
    big = scan_inflate(raw)
    t16 = big.decode("utf-16-le", "ignore")
    t8 = big.decode("utf-8", "ignore")
    objs = metadata_objects(t8) or metadata_objects(t16)
    # data presence (approx, offline): FIO + record-bearing subsystems
    fio = set(_FIO_RE.findall(t16)) | set(_FIO_RE.findall(t8))
    has_people = len(fio) > 0
    r = _empty(path, source, fmt)
    for o in objs:
        r["entities"].append({"key": o["key"], "name": o["name"], "kind": o["kind"],
                              "count": None, "fields": [], "links": [], "migratable": True})
    r["hasData"] = has_people and fmt != "cf"
    r["metrics"] = {"entities": len(objs), "records": (len(fio) if r["hasData"] else 0),
                    "fields": 0, "links": 0, "currencies": 0,
                    "registers": sum(1 for o in objs if "register" in o["kind"])}
    if fmt == "cf":
        r["summary"] = "Конфігурація (.cf): %d об'єктів структури, даних немає (by design)" % len(objs)
        r["notes"].append("Тільки структура — для даних потрібен .1CD/.dt/.xml")
    else:
        r["summary"] = "Дамп ІБ (.dt): %d об'єктів структури; знайдено ~%d ФІО (приблизно, офлайн)" % (
            len(objs), len(fio))
        r["notes"].append("Точні дані по таблицях — через платформу 1С (вивантаження в EnterpriseData) або повний розбір сторінок")
    return r


def analyze(src):
    path, tmp = fetch(src)
    try:
        fmt = detect_format(path)
        if fmt == "xml":
            return analyze_xml(path, src)
        if fmt == "1cd":
            return analyze_1cd(path, src)
        if fmt in ("dt", "cf"):
            return _analyze_container(path, src, fmt)
        r = _empty(path, src, fmt)
        r["summary"] = "Непідтримуваний/невідомий формат: %s" % fmt
        r["notes"].append("zip → розпакувати; інші → завантажити .1CD/.dt/.cf/.xml")
        return r
    finally:
        if tmp:
            try: os.unlink(path)
            except Exception: pass


if __name__ == "__main__":
    srcs = sys.argv[1:]
    if not srcs:
        print("usage: analyze.py <file-or-url> [<file-or-url> ...]", file=sys.stderr)
        sys.exit(2)
    results = []
    for s in srcs:
        try:
            results.append(analyze(s))
        except Exception as e:
            results.append({"source": s, "error": "%s: %s" % (type(e).__name__, e)})
    print(json.dumps(results if len(results) > 1 else results[0], ensure_ascii=False, indent=2))
