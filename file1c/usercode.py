#!/usr/bin/env python3
"""Corezoid GIT Call entrypoint (lang=python, path=file1c) — 1C FILE → §3 ENTITIES.

Feeds the Corezoid skill **dto-mf-file-1c**: takes a client 1C artifact (.1CD / .xml /
.dt / .cf) and returns a FLAT list of §3 business entities (employees, counterparties,
nomenclature, documents) ready for dto-mf-writer + dto-mf-graph, plus a `document`
file-actor entity carrying Розмір/Рядків. Deterministic, non-inventing: every entity is a
real record read from the file (names/titles come straight from the DB tables).

Reuses the proven twin-parser extractor (extractor.TwinExtractor + analyze.detect_format),
so the same onec_dtools engine that powers twin-parser (1871576) is the source of truth —
we only re-shape its records into the §3 ontology contract instead of executor ops.

Contract (git_call calls handle(data)):
  IN:  source_url (str)  http(s) URL or a path the runner can read (the .1CD/.xml/...)
       caps       (obj)  optional per-class cap of materialized actors
                         default {employees:25, counterparties:25, nomenclature:25, documents:20}
       file_name  (str)  optional display name (else basename of source_url)
  OUT: data["file1c"] = {
         found (bool), format (str), file_name (str), bytes (int), kb (int),
         rows_total (int), counts {cls: full_valid_count}, shown {cls: materialized},
         org_name (str|null),
         entities [ §3 entity objects, source='1c', status='confirmed' ],
         twin_error (str|null), lib_status {}, budget {}
       }
  Also mirrors entities/count/org_name/found to top-level for easy {{...}} pickup.

Honesty: если формат не .1CD/.xml/.dt (или не парсится) → found=false, entities=[document]
только (file-actor с честным value), twin_error заполнен. Ничего не выдумываем.
Окно git_call ≤30с: файлы демо (≤27 МБ) парсятся за секунды; материализация capped.
"""
import os, sys, json, time, hashlib, tempfile, traceback
from urllib.request import urlretrieve

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# extractor.py / analyze.py live in the repo ROOT (one level up from file1c/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LIB = {}
try:
    import onec_dtools
    LIB["onec_dtools"] = getattr(onec_dtools, "__version__", "?")
except Exception as e:                       # pragma: no cover
    onec_dtools = None; LIB["onec_dtools"] = "ERR:%s" % e
try:
    import extractor as EX
    import analyze as AN
    LIB["extractor"] = "ok"
except Exception as e:                        # pragma: no cover
    EX = None; AN = None; LIB["extractor"] = "ERR:%s" % e

# canonical 1C class (extractor.canon_class) → §3 ontology class
CLS_MAP = {
    "employees": "employee",
    "counterparties": "counterparty",
    "nomenclature": "nomenclature",
    "documents": "document",
}
DEFAULT_CAPS = {"employees": 25, "counterparties": 25, "nomenclature": 25, "documents": 20}
_VALUE_HINT = {
    "employee": "Співробітник (1С)",
    "counterparty": "Контрагент (1С)",
    "nomenclature": "Номенклатура (1С)",
    "document": "Документ (1С)",
}


def _ensure_file(source):
    """Local path → as-is; http(s) URL → cache in /tmp keyed by URL hash."""
    if not str(source).startswith(("http://", "https://")):
        return source, {"dl_ms": 0, "cached": True}
    h = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
    base = os.path.basename(source.split("?", 1)[0]) or "src.1cd"
    cached = os.path.join(tempfile.gettempdir(), "file1c_%s_%s" % (h, base))
    if os.path.exists(cached) and os.path.getsize(cached) > 0:
        return cached, {"dl_ms": 0, "cached": True}
    t0 = time.time()
    tmp = cached + ".part"
    urlretrieve(source, tmp)
    os.replace(tmp, cached)
    return cached, {"dl_ms": int((time.time() - t0) * 1000), "cached": False}


def _salary_of(ex, o, d):
    """Оклад/зарплата of an employee row, if a numeric field matches (see stage_actor_data)."""
    import re
    for col in d:
        nm = (ex.fld_name(col) or "").lower()
        v = ex._val(d.get(col))
        if v in (None, ""):
            continue
        if re.search(r"оклад|зарплат|salary|оплат", nm):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _org_name(ex):
    """First organization / company name, if the base has an 'organizations' catalog."""
    try:
        for o in ex.objects():
            if o.get("cls") == "organizations":
                for row in ex.tables[o["phys"]]:
                    d = row.as_dict()
                    if ex.row_valid(o, d):
                        t = ex.row_title(o, d, 0)
                        if t:
                            return str(t)
    except Exception:
        pass
    return None


def _file_meta(path, source, fmt):
    try:
        b = os.path.getsize(path)
    except Exception:
        b = 0
    return {"file_name": os.path.basename(source.split("?", 1)[0]) or "file",
            "bytes": b, "kb": int(round(b / 1024.0)), "format": fmt}


def handle(data, context=None):
    out = {"found": False, "format": None, "file_name": None, "bytes": 0, "kb": 0,
           "rows_total": 0, "counts": {}, "shown": {}, "org_name": None,
           "entities": [], "twin_error": None, "lib_status": LIB, "budget": {}}
    source = (data.get("source_url") or data.get("url") or data.get("file_url")
              or data.get("path") or "").strip()
    caps = data.get("caps") or {}
    if isinstance(caps, str):
        try:
            caps = json.loads(caps)
        except Exception:
            caps = {}
    C = dict(DEFAULT_CAPS)
    for k, v in (caps or {}).items():
        try:
            C[k] = int(v)
        except Exception:
            pass
    t0 = time.time()
    try:
        if EX is None or onec_dtools is None:
            raise RuntimeError("extractor/onec_dtools unavailable: %s" % LIB)
        if not source:
            raise RuntimeError("source_url required")
        path, dl = _ensure_file(source)
        out["budget"]["download"] = dl
        fmt = AN.detect_format(path)
        meta = _file_meta(path, source, fmt)
        out.update({"format": fmt, "file_name": data.get("file_name") or meta["file_name"],
                    "bytes": meta["bytes"], "kb": meta["kb"]})
        entities = []
        if fmt not in ("1cd",):
            # base wave: only .1CD is materialized to records; others → honest doc-actor + note
            out["twin_error"] = ("формат '%s' не парситься у цій хвилі (детермінований парсер "
                                 "1С підтримує .1CD); файл зафіксовано як документ-актор" % fmt)
            entities.append(_doc_actor(out, note=out["twin_error"]))
            out["entities"] = entities
            _mirror(data, out)
            return data
        # ---- .1CD : real record enumeration via the twin-parser extractor ----
        ex = EX.TwinExtractor(path)
        try:
            out["org_name"] = _org_name(ex)
            company_parent = out["org_name"] or None  # entities hang off lead-root (parent=null) for cross-source merge
            counts, shown, rows_total = {}, {}, 0
            for o in ex.objects():
                cls1c = o.get("cls")
                target = CLS_MAP.get(cls1c)
                if not target:
                    continue
                cap = C.get(cls1c, 0)
                mat = 0
                full = 0
                try:
                    rows = list(ex.tables[o["phys"]])
                except Exception:
                    rows = []
                for ri, row in enumerate(rows):
                    d = row.as_dict()
                    if not ex.row_valid(o, d):
                        continue
                    full += 1
                    rows_total += 1
                    if mat >= cap:
                        continue
                    title = str(ex.row_title(o, d, ri) or "").strip()
                    if not title:
                        continue
                    std = {}
                    for col, key in (("_CODE", "code"), ("_NUMBER", "number"),
                                     ("_DATE_TIME", "date")):
                        v = ex._val(d.get(col)) if col in d else None
                        if v not in (None, ""):
                            std[key] = v
                    accounts = {}
                    value = _VALUE_HINT.get(target, target)
                    if target == "employee":
                        sal = _salary_of(ex, o, d)
                        if sal:
                            accounts["Оклад"] = sal
                    if target == "document":
                        num = std.get("number") or ""
                        dt = std.get("date") or ""
                        value = ("Документ 1С %s %s" % (num, dt)).strip() or value
                    ent = {"entity": target, "title": title[:120], "value": value[:200],
                           "confidence": 0.95, "source": "1c", "source_url": out["file_name"],
                           "source_ref": out["file_name"], "status": "confirmed",
                           "parent": company_parent, "accounts": accounts, "links": []}
                    entities.append(ent)
                    mat += 1
                counts[cls1c] = counts.get(cls1c, 0) + full
                shown[cls1c] = shown.get(cls1c, 0) + mat
            out["counts"] = counts
            out["shown"] = shown
            out["rows_total"] = rows_total
        finally:
            ex.close()
        # file-actor (document) carrying Розмір(KB) + Рядків(total)
        entities.append(_doc_actor(out))
        out["entities"] = entities
        out["found"] = True
    except Exception as e:
        out["twin_error"] = "%s: %s" % (type(e).__name__, e)
        out["trace"] = traceback.format_exc()[-800:]
        if not out["entities"]:
            out["entities"] = [_doc_actor(out, note=out["twin_error"])]
    out["budget"]["total_ms"] = int((time.time() - t0) * 1000)
    _mirror(data, out)
    return data


def _doc_actor(out, note=None):
    parts = []
    if out.get("format"):
        parts.append("Файл 1С (%s)" % out["format"])
    if out.get("rows_total"):
        parts.append("%d рядків" % out["rows_total"])
    if out.get("counts"):
        parts.append(", ".join("%s:%d" % (k, v) for k, v in out["counts"].items()))
    if note:
        parts.append(note)
    value = "; ".join(parts) or "Файл клієнта"
    acc = {"Розмір": out.get("kb", 0)}
    if out.get("rows_total"):
        acc["Рядків"] = out["rows_total"]
    return {"entity": "document", "title": (out.get("file_name") or "Файл 1С")[:120],
            "value": value[:200], "confidence": 0.95, "source": "1c",
            "source_url": out.get("file_name"), "source_ref": out.get("file_name"),
            "status": "confirmed", "parent": out.get("org_name") or None,
            "accounts": acc, "links": []}


def _mirror(data, out):
    data["file1c"] = out
    data["entities"] = out["entities"]
    data["entN"] = len(out["entities"])
    data["cName"] = out.get("org_name") or ""
    data["found"] = out["found"]
    data["twin_error"] = out.get("twin_error")


# git_call convention: some runners call usercode(data), some handle(data)
def usercode(data, context=None):
    return handle(data, context)


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else \
        "/Users/sviks/Work/Claude/ai-smart-migration/port-1c/1c-demo/employees-demo-bases/Radchenko_1Cv8.1CD"
    r = handle({"source_url": src})
    f = r["file1c"]
    print("found=%s format=%s file=%s kb=%s rows=%s" % (f["found"], f["format"], f["file_name"], f["kb"], f["rows_total"]))
    print("counts:", json.dumps(f["counts"], ensure_ascii=False), "shown:", json.dumps(f["shown"], ensure_ascii=False))
    print("org_name:", f["org_name"], "entities:", len(f["entities"]))
    from collections import Counter
    print("by class:", dict(Counter(e["entity"] for e in f["entities"])))
    for e in f["entities"][:12]:
        print("  -", e["entity"], "|", e["title"], "|", e["value"][:40], "| acc", e["accounts"])
