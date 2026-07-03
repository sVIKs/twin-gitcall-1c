#!/usr/bin/env python3
"""Corezoid GIT Call entrypoint (lang=python) — STATELESS PARSER / SEQUENCER.

This node ONLY parses and sequences. It never calls Simulator. The Corezoid process
is the executor + state machine: it takes the batch of typed tasks this node returns,
creates/fills/links them in Simulator (api nodes), stores ref→id in its state process,
then calls this node AGAIN with the returned cursor to get the next batch — looping
until `done`, so the whole graph + accounts + links get built across many ≤30s steps.

One call == one bounded window (≤ ~1.4 MB serialized OR ≤ ~25 s, whichever first),
so it always fits inside the GIT Call 30 s budget and never returns more than ~1 MB.

Tasks come out in strict dependency order (so an executor can apply them as-is):
  create_form → create_actor → fill_actor → create_account/transfer → create_link
For .1CD this is the extractor's staged cursor; for EnterpriseData XML and structure
containers (.cf/.dt) the (small) task set is ordered and returned in a single window.

Task `data` IN:
  source_url (str)  http(s) link or a path the runner can read
  scope      (str)  "full" (structure+data) | "structure" (forms only)   default "full"
  cursor     (obj)  opaque resume token from the previous call; absent/null = start

Task `data` OUT (added):
  tasks   (list) the next batch of typed task objects  (≤ ~1 MB, dependency-ordered)
  cursor  (obj)  pass this back verbatim on the next call
  done    (bool) true ⇒ no more tasks; the executor stops looping and reconciles
  format  (str)  detected artifact format (echoed for the executor/logs)
  count   (int)  len(tasks) this batch
  twin_error (str) present only on failure
"""
import os, sys, json, hashlib, tempfile, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extractor as EX
import analyze as AN
import build_twin as BT   # op_stream_xml + fetch_to_temp (download/cache helper)

STAGES = EX.STAGES
N_STAGES = len(STAGES)
# priority for non-staged formats (xml/cf/dt) — same dependency order the stages encode
_PRIO = {"create_form": 0, "create_actor": 1, "fill_actor": 2,
         "create_account": 3, "transfer": 4, "create_link": 5}


def _ensure_file(source):
    """Local path → as-is. http(s) URL → cache in /tmp keyed by URL hash (download only
    when cold), so repeated cursor calls don't re-download the same artifact each step."""
    if not str(source).startswith(("http://", "https://")):
        return source
    h = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
    base = os.path.basename(source.split("?", 1)[0]) or "src"
    cached = os.path.join(tempfile.gettempdir(), "twinsrc_%s_%s" % (h, base))
    if os.path.exists(cached) and os.path.getsize(cached) > 0:
        return cached
    tmp = BT.fetch_to_temp(source)          # streams the download to a temp file
    try:
        os.replace(tmp, cached); return cached
    except Exception:
        return tmp


def _next_stage(si, scope):
    """Advance si to the next stage that can yield under the current scope (skip None
    stages and, in structure scope, every non-forms stage). Returns the stage index
    (== N_STAGES when exhausted)."""
    while si < N_STAGES:
        name, fn = STAGES[si]
        if fn is None:
            si += 1; continue
        if scope == "structure" and not name.startswith("forms"):
            si += 1; continue
        break
    return si


def _window_1cd(path, scope, cursor, classes=None):
    si = _next_stage((cursor or {}).get("si", 0), scope)
    if si >= N_STAGES:
        return [], {"fmt": "1cd", "si": si}, True
    sc = (cursor or {}).get("sc")
    out = EX.window(path, STAGES[si][0], sc, classes=classes)
    if out["done"]:
        nsi = _next_stage(si + 1, scope)
        return out["tasks"], {"fmt": "1cd", "si": nsi, "sc": None}, (nsi >= N_STAGES)
    return out["tasks"], {"fmt": "1cd", "si": si, "sc": out["next_cursor"]}, False


def _window_oneshot(path, fmt, scope, rep):
    """xml / cf / dt: produce the whole (small) ordered task set in a single window."""
    if fmt.startswith("xml"):
        raw = [op for (_s, op) in BT.op_stream_xml(path)]
    else:  # cf / dt — structure only
        raw = [{"op": "create_form", "ref": e["key"], "title": e["name"],
                "color": "#868e96", "fields": []} for e in rep["entities"]]
    if scope == "structure":
        raw = [op for op in raw if op.get("op") == "create_form"]
    raw.sort(key=lambda o: _PRIO.get(o.get("op"), 9))
    return raw


def _find_field(d, keys):
    """Return the first non-empty value of any column whose name contains one of `keys`."""
    for k, v in d.items():
        kl = str(k).lower()
        if any(t in kl for t in keys):
            s = str(v).strip()
            if s and s not in ("None", "b''"):
                return s
    return ""


def _people_rows(path):
    """Extract real employees from the 1C «Сотрудники» catalog: {code, fio, position, salary}.
    Universal: matches any employees/staff/persons catalog. `position`/`salary` are pulled from
    columns whose names hint at a role/оклад when the schema carries them; minimal bases (only
    name+code, like this demo) fall back to a generic «Співробітник» position and salary 0 — so the
    same contract works on richer bases where СотрудникиОрганизаций.Должность / начисления exist."""
    ex = EX.TwinExtractor(path)
    out = []
    for o in ex.objects():
        low = (o.get("title") or "").lower()
        if any(k in low for k in ("сотруд", "работн", "персон", "employee", "кадр")):
            for row in ex.tables[o["phys"]]:
                d = row.as_dict() if hasattr(row, "as_dict") else {}
                fio = (d.get("_DESCRIPTION") or d.get("_Description") or "").strip()
                code = (d.get("_CODE") or d.get("_Code") or "").strip()
                if not fio:
                    continue
                position = _find_field(d, ("должност", "posad", "position", "role")) or "Співробітник"
                salary_s = _find_field(d, ("оклад", "зарплат", "salary", "оплат"))
                try:
                    salary = int(float(salary_s)) if salary_s else 0
                except ValueError:
                    salary = 0
                out.append({"code": code, "fio": fio, "position": position, "salary": salary})
            break
    return out


def _isnum(v):
    try:
        float(v); return str(v) not in ("None", "")
    except (TypeError, ValueError):
        return False


def _document_rows(path, limit_docs=3):
    """Extract 1C documents WITH their line items (декомпозиция): each document → {number, date, lines:[{lineno, amount}]}.
    Pairs a document header table (_DocumentNN) with its tabular-section (_DocumentNN_VT*), so each line becomes a
    separate posting (not one aggregate total). `amount` = the line's sum column (max numeric field: qty*price=sum)."""
    ex = EX.TwinExtractor(path)
    objs = list(ex.objects())
    # tabular-section table (has lines): phys like _DocumentNN_VT*
    vt = next((o for o in objs if "_VT" in str(o.get("phys") or "") and any(
        k in (o.get("title") or "").lower() for k in ("накладн", "документ", "заказ", "приход", "расход", "оплат"))), None)
    if not vt:
        return []
    title = (vt.get("title") or "Документ").split(" — ")[0].split(" —")[0]
    docs = {}
    for row in ex.tables[vt["phys"]]:
        try:
            d = row.as_dict()
        except Exception:
            continue
        lineno = ""
        for k, v in d.items():
            if "lineno" in str(k).lower():
                lineno = str(v)
        nums = [float(v) for k, v in d.items() if _isnum(v) and "lineno" not in str(k).lower()]
        if not nums:
            continue
        amount = max(nums)                       # line sum column
        docs.setdefault("000000001", []).append({"lineno": lineno or str(len(docs.get("000000001", [])) + 1), "amount": amount})
    out = []
    for num, lines in list(docs.items())[:limit_docs]:
        out.append({"doc_type": title, "number": num, "lines": lines,
                    "total": round(sum(l["amount"] for l in lines), 2)})
    return out


def usercode(data, context=None):
    try:
        source = data.get("source_url") or data.get("source")
        if not source:
            data["twin_error"] = "missing source_url"; data["done"] = True; return data
        scope = (data.get("scope") or "full").lower()
        if scope not in ("full", "structure"):
            scope = "full"
        cursor = data.get("cursor") or None
        path = _ensure_file(source)
        if (data.get("mode") or "").lower() == "people":
            people = _people_rows(path)
            data["tasks"] = people          # reply returns the tasks[] param → carries employees back
            data["employees"] = people
            data["cursor"] = {}; data["done"] = True; data["format"] = "people"
            data["count"] = len(people); data.pop("twin_error", None); return data
        if (data.get("mode") or "").lower() == "analyze":
            rep = AN.analyze(path)
            m = (rep.get("metrics") or {})
            metrics = {"entities": int(m.get("entities", 0) or 0), "records": int(m.get("records", 0) or 0),
                       "links": int(m.get("links", 0) or 0), "registers": int(m.get("registers", 0) or 0),
                       "fields": int(m.get("fields", 0) or 0), "fmt": rep.get("format", "")}
            planned = {}
            if AN.detect_format(path) == "1cd":     # v2: per-class planned counts (scope-UI + STRUCT-гейт)
                ex = EX.TwinExtractor(path)
                try: planned = ex.class_counts()
                finally: ex.close()
            data["tasks"] = [metrics]       # reply carries it via the tasks[] param
            data["metrics"] = metrics
            data["planned"] = planned
            data["cursor"] = {}; data["done"] = True; data["format"] = "analyze"
            data["count"] = 1; data.pop("twin_error", None); return data
        if (data.get("mode") or "").lower() == "baseline":
            # v2: эталон для reconcile — агрегаты регистров per (актор, счёт) на стороне ФАЙЛА
            ex = EX.TwinExtractor(path)
            try:
                agg = ex.aggregate_accounts()
                base = [{"ref": k[0], "name": k[1],
                         "amount": round(v["amount"], 4), "currency": v["currency"]}
                        for k, v in sorted(agg.items())][:5000]
            finally:
                ex.close()
            data["tasks"] = base
            data["baseline"] = base
            data["cursor"] = {}; data["done"] = True; data["format"] = "baseline"
            data["count"] = len(base); data.pop("twin_error", None); return data
        if (data.get("mode") or "").lower() == "documents":
            docs = _document_rows(path)
            data["tasks"] = docs           # reply carries documents+lines via the tasks[] param
            data["documents"] = docs
            data["cursor"] = {}; data["done"] = True; data["format"] = "documents"
            data["count"] = len(docs); data.pop("twin_error", None); return data
        fmt = (cursor or {}).get("fmt") or AN.detect_format(path)
        # v2: scope_classes — выбор пользователем структур для переноса (список/CSV)
        sc_raw = data.get("scope_classes")
        if isinstance(sc_raw, str):
            classes = set(x.strip() for x in sc_raw.split(",") if x.strip()) or None
        elif isinstance(sc_raw, (list, tuple)):
            classes = set(sc_raw) or None
        else:
            classes = None

        if fmt == "1cd":
            tasks, next_cursor, done = _window_1cd(path, scope, cursor, classes=classes)
        else:
            if cursor and cursor.get("done"):
                tasks, next_cursor, done = [], cursor, True
            else:
                rep = AN.analyze(path) if not fmt.startswith("xml") else {"entities": []}
                tasks = _window_oneshot(path, fmt, scope, rep)
                next_cursor, done = {"fmt": fmt, "done": True}, True

        data["tasks"] = tasks
        data["cursor"] = next_cursor
        data["done"] = bool(done)
        data["format"] = fmt
        data["count"] = len(tasks)
        data.pop("twin_error", None)
    except Exception as e:
        data["twin_error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
        data["twin_trace"] = traceback.format_exc()[-1200:]
        data["tasks"] = []; data["done"] = True
    return data


def handle(data):
    """Corezoid GIT Call entrypoint (runner calls handle(data))."""
    return usercode(data)


# Local harness: drive the full cursor loop over a file and report parity + batch sizes.
# `python3 usercode.py <file|url> [scope]`
if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "../1c-demo/8-3-8_8K.1CD"
    scope = sys.argv[2] if len(sys.argv) > 2 else "full"
    d = {"source_url": src, "scope": scope}
    import collections
    totals = collections.Counter(); steps = 0; maxb = 0; first_order = []
    while True:
        d = usercode({"source_url": src, "scope": scope, "cursor": d.get("cursor")})
        if d.get("twin_error"):
            print("ERROR:", d["twin_error"]); print(d.get("twin_trace", "")); break
        steps += 1
        b = len(json.dumps(d["tasks"], ensure_ascii=False)); maxb = max(maxb, b)
        for t in d["tasks"]:
            totals[t["op"]] += 1
            if len(first_order) < 12: first_order.append(t["op"])
        if d["done"]:
            break
        if steps > 100000:
            print("loop guard"); break
    print(json.dumps({"format": d.get("format"), "steps": steps,
                      "max_batch_bytes": maxb, "totals": dict(totals),
                      "first_ops": first_order}, ensure_ascii=False, indent=2))
