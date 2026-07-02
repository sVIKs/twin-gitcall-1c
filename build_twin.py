#!/usr/bin/env python3
"""Twin builder: deploy a digital twin of a 1C artifact into Simulator (live PAPI).

Pipeline (universal, any format, no hardcodes):
  analyze(file) -> PLAN (identified: forms, records, fields, links, accounts, currencies)
  -> op stream (1CD: extractor.window stages; XML: parsed entities; cf/dt: structure-only)
  -> execute on Simulator PAPI (ref-native idempotent): forms, currencies, account defs,
     actors+data, double-entry transfers (debit/credit), links, graph layer
  -> reconcile PLAN vs FACT on ACCOUNTS of a migration-case actor (accounts are primary;
     planned == built at the end => success).

Auth: reads ACCESS_TOKEN + WORKSPACE_ID from ./.env, header `Authorization: Simulator <jwt>`
(exactly as the Simulator MCP plugin). No secret is hardcoded.

Usage: build_twin.py <file-or-url> [--cap N] [--name NAME] [--scope structure|full] [--graph]
"""
import sys, os, re, json, ssl, time, argparse, tempfile, urllib.request, urllib.error, urllib.parse, collections

def _q(s):  # url-encode a ref for use in a URL path (Cyrillic, '/', etc.)
    return urllib.parse.quote(str(s), safe="")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import analyze as AN

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE


def load_env():
    env = {}
    p = os.path.join(os.path.dirname(HERE), ".env")
    for line in open(p):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class PAPI:
    def __init__(self, token, ws, base="https://mw.simulator.company/papi/1.0"):
        # token + workspace are passed in (GIT Call: from task data; CLI: from .env).
        # Scheme auto-detected: app token atn_ -> Bearer, session JWT -> Simulator.
        self.tok = token
        self.scheme = "Bearer" if str(self.tok).startswith("atn_") else "Simulator"
        self.ws = ws; self.base = base
        self.calls = 0

    def __call__(self, method, path, body=None, q=""):
        self.calls += 1
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(self.base + path + q, data=data, method=method,
            headers={"Authorization": self.scheme + " " + self.tok, "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                r = urllib.request.urlopen(req, context=CTX, timeout=40)
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
            except urllib.error.HTTPError as e:
                raw = e.read().decode()
                try: j = json.loads(raw)
                except Exception: j = {"raw": raw[:300]}
                return e.code, j
            except Exception as e:
                if attempt == 2: return "ERR", {"err": str(e)[:200]}
                time.sleep(1.5)


class Builder:
    def __init__(self, papi, name, cap, do_graph, owner_id=66423):
        self.p = papi; self.name = name; self.cap = cap; self.do_graph = do_graph
        self.owner_id = owner_id
        self.pfx = "twin_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        self.form_id = {}        # form_ref -> formId
        self.actor_id = {}       # actor_ref -> {id, formId}
        self.pair = {}           # (acc_name, currency) -> (nameId, currencyId)
        self.acc_id = {}         # (actor_ref, acc_name, currency) -> {debit,credit}
        self.edge_id = {}        # (srcActorId, tgtActorId) -> edgeId
        self.graph_id = None; self.graph_layer = None; self.root_la = None
        self.node_la = {}; self.node_count = 0
        self.fact = collections.Counter()
        self.errors = []
        self.state_path = os.path.join(tempfile.gettempdir(), ".twin_%s.json" % self.pfx)
        self._load_state()

    def gref(self, ref):  # twin-scoped, ref-native
        return self.pfx + "__" + ref

    # ---------- idempotency state (reliable; resumable) ----------
    def _load_state(self):
        if os.path.exists(self.state_path):
            try:
                s = json.load(open(self.state_path))
                self.form_id = s.get("form_id", {})
                self.actor_id = s.get("actor_id", {})
                self.pair = {tuple(k.split("\x1f")): tuple(v) for k, v in s.get("pair", {}).items()}
                self.acc_id = {tuple(k.split("\x1f")): v for k, v in s.get("acc_id", {}).items()}
            except Exception:
                pass

    def save_state(self):
        s = {"form_id": self.form_id, "actor_id": self.actor_id,
             "pair": {"\x1f".join(k): list(v) for k, v in self.pair.items()},
             "acc_id": {"\x1f".join(k): v for k, v in self.acc_id.items()}}
        json.dump(s, open(self.state_path, "w"), ensure_ascii=False)

    def _resolve_form(self, gref):
        """Best-effort: find a form id by ref via search (used to recover on 'already exists')."""
        st, j = self.p("GET", "/forms/" + self.p.ws, q="?filter=id,ref&search=" + _q(gref))
        for f in (j.get("data") or []):
            if f.get("ref") == gref:
                return f["id"]
        return None

    def create_form(self, op):
        gref = self.gref(op["ref"])
        if gref in self.form_id:
            self.fact["forms"] += 1; return
        content = []
        for fld in op.get("fields", []):
            content.append({"id": fld["id"], "class": fld.get("class", "edit"),
                            "title": fld.get("title", fld["id"]), "visibility": "visible"})
        if not content:   # PAPI requires >=1 content item per section
            content = [{"id": "item_1", "class": "label", "title": op["title"][:120],
                        "value": op["title"][:120], "visibility": "visible"}]
        body = {"title": op["title"][:120], "color": op.get("color", "#868e96"),
                "ref": gref, "sections": [{"id": "main", "title": op["title"][:120], "content": content}]}
        st, j = self.p("POST", "/forms/%s/true" % self.p.ws, body)
        fid = (j.get("data") or {}).get("id") or j.get("id")
        if fid:
            self.form_id[gref] = fid; self.fact["forms"] += 1
        elif st == 400 and "already exists" in json.dumps(j, ensure_ascii=False):
            rid = self._resolve_form(gref)
            if rid: self.form_id[gref] = rid; self.fact["forms"] += 1
            else: self.errors.append(("create_form_exists_unresolved", op["ref"], st, j))
        else:
            self.errors.append(("create_form", op["ref"], st, j))

    # ---------- actors ----------
    def _actor_by_ref(self, formId, gref):
        st, j = self.p("GET", "/actors/ref/%s/%s" % (formId, _q(gref)), q="?filter=id")
        if st == 200:
            return (j.get("data") or {}).get("id")
        return None

    def create_actor(self, op):
        fid = self.form_id.get(self.gref(op["form_ref"]))
        if not fid:
            self.errors.append(("actor:no_form", op["ref"], op["form_ref"], None)); return
        gref = self.gref(op["ref"])
        existing = self._actor_by_ref(fid, gref)
        if existing:
            self.actor_id[op["ref"]] = {"id": existing, "formId": fid}; self.fact["actors"] += 1
            self._place_actor(existing); return
        body = {"ref": gref, "title": (op.get("title") or op["ref"])[:200], "data": {},
                "ownerId": self.owner_id}
        st, j = self.p("POST", "/actors/actor/%s" % fid, body)
        aid = (j.get("data") or {}).get("id") or j.get("id")
        if aid:
            self.actor_id[op["ref"]] = {"id": aid, "formId": fid}; self.fact["actors"] += 1
            self._place_actor(aid)
        else:
            self.errors.append(("create_actor", op["ref"], st, j))

    def fill_actor(self, op):
        a = self.actor_id.get(op["ref"])
        if not a: return
        st, j = self.p("PUT", "/actors/actor/ref/%s/%s" % (a["formId"], _q(self.gref(op["ref"]))),
                       {"data": op["data"]})
        if st == 200: self.fact["filled"] += 1
        else: self.errors.append(("fill_actor", op["ref"], st, j))

    # ---------- accounts / double-entry ----------
    def _ensure_pair(self, acc_name, currency):
        key = (acc_name, currency)
        if key in self.pair: return self.pair[key]
        st, j = self.p("POST", "/accounts/pair/" + self.p.ws,
                       {"accountName": acc_name, "currencyName": currency, "precision": 2})
        d = j.get("data") or {}
        nameId = (d.get("accountName") or {}).get("id")
        curId = (d.get("currency") or {}).get("id")
        if not (nameId and curId):
            self.errors.append(("ensure_pair", acc_name, currency, st, j))
        self.pair[key] = (nameId, curId)
        return self.pair[key]

    def _ensure_account(self, actor_ref, acc_name, currency):
        """Returns {'debit': id, 'credit': id} — both sides of the actor's account."""
        key = (actor_ref, acc_name, currency)
        if key in self.acc_id: return self.acc_id[key]
        a = self.actor_id.get(actor_ref)
        if not a: return None
        nameId, curId = self._ensure_pair(acc_name, currency)
        if not (nameId and curId): return None
        st, j = self.p("POST", "/accounts/%s" % a["id"],
                       {"nameId": nameId, "currencyId": curId, "accountType": "fact", "search": True})
        d = j.get("data")
        sides = {}
        if isinstance(d, list):
            for x in d:
                if x.get("incomeType") in ("debit", "credit"):
                    sides[x["incomeType"]] = x["id"]
        if not sides:
            self.errors.append(("ensure_account", actor_ref, acc_name, st, j))
        self.acc_id[key] = sides or None
        return self.acc_id[key]

    def set_balance(self, actor_ref, acc_name, currency, amount):
        s = self._ensure_account(actor_ref, acc_name, currency)
        if s and s.get("credit"):
            self.p("PUT", "/accounts/amount/%s" % s["credit"], {"amount": amount}); return True
        return False

    def transfer(self, op):
        # double-entry: from-leg debits the source's DEBIT side, to-leg credits the target's CREDIT side
        cur = op.get("currency") or "Одиниці"; nm = op.get("account_name") or "Сума"
        frm, to = [], []
        for leg in op.get("from", []):
            s = self._ensure_account(leg["actor_ref"], nm, cur)
            if s and s.get("debit"): frm.append({"accountId": s["debit"], "amount": leg["amount"]})
        for leg in op.get("to", []):
            s = self._ensure_account(leg["actor_ref"], nm, cur)
            if s and s.get("credit"): to.append({"accountId": s["credit"], "amount": leg["amount"]})
        if not frm or not to:
            self.errors.append(("transfer_empty", op["ref"], cur, nm)); return
        st, j = self.p("POST", "/transfers/" + self.p.ws,
                       {"from": frm, "to": to, "ref": self.gref(op["ref"]), "comment": nm})
        if st in (200, 201): self.fact["txns"] += 1
        elif st == 400 and "Not unique ref" in json.dumps(j, ensure_ascii=False):
            self.fact["txns"] += 1   # idempotent: this transfer already posted
        else: self.errors.append(("transfer", op["ref"], st, j))

    # ---------- links ----------
    def flush_links(self, pending):
        # resolve refs -> ids, massLink in batches of 50
        edges = []
        for e in pending:
            s = self.actor_id.get(e["source_ref"]); t = self.actor_id.get(e["target_ref"])
            if s and t and s["id"] != t["id"]:
                edges.append({"source": s["id"], "target": t["id"], "edgeTypeId": 13})
        for i in range(0, len(edges), 50):
            batch = edges[i:i + 50]
            st, j = self.p("POST", "/actors/mass_links/" + self.p.ws, batch)
            if st in (200, 201):
                self.fact["links"] += len(batch)
                for it in (j.get("data") or []):
                    ed = it.get("data") if isinstance(it, dict) else None
                    if isinstance(ed, dict) and ed.get("source") and ed.get("target") and ed.get("id"):
                        self.edge_id[(ed["source"], ed["target"])] = ed["id"]
            else: self.errors.append(("massLink", st, j))

    # ---------- visible structure graph (Graph + Layer + placed nodes/edges) ----------
    def _ga(self, kind, formId, title):
        gref = self.gref(kind)
        if gref in self.actor_id: return self.actor_id[gref]["id"]
        existing = self._actor_by_ref(formId, gref)
        if existing:
            self.actor_id[gref] = {"id": existing, "formId": formId}; return existing
        st, j = self.p("POST", "/actors/actor/%s" % formId,
                       {"ref": gref, "title": title, "ownerId": self.owner_id, "data": {}})
        aid = (j.get("data") or {}).get("id") or j.get("id")
        if aid: self.actor_id[gref] = {"id": aid, "formId": formId}
        return aid

    def setup_graph(self):
        """Create the MAIN graph (root node + layer); every actor branches from root and
        is placed LIVE as it's built. Returns the graph URL (known up-front for the widget)."""
        p = self.p
        g = self._ga("graph", 26740, "Двійник: " + self.name)
        l = self._ga("layer", 26741, "Двійник: " + self.name)
        if not (g and l):
            self.errors.append(("graph_layer", g, l)); return None
        p("POST", "/actors/link/" + p.ws, {"source": g, "target": l, "edgeTypeId": 13})
        p("DELETE", "/graph_layers/clean/" + l, {})   # wipe (DELETE needs {} body w/ json content-type)
        self.create_form({"op": "create_form", "ref": "__root__", "title": "Двійник (корінь)",
                          "color": "#e8590c",
                          "fields": [{"id": "item_1", "class": "label", "title": "Корінь", "value": "Корінь"}]})
        self.fact["forms"] -= 1   # root is a meta form — exclude from reconcile
        rfid = self.form_id.get(self.gref("__root__"))
        rref = self.gref("root")
        rid = self._actor_by_ref(rfid, rref)
        if not rid:
            st, j = p("POST", "/actors/actor/%s" % rfid,
                      {"ref": rref, "title": "Двійник: " + self.name, "ownerId": self.owner_id, "data": {}})
            rid = (j.get("data") or {}).get("id") or j.get("id")
        self.actor_id["root"] = {"id": rid, "formId": rfid}
        st, j = p("POST", "/graph_layers/actors/" + l,
                  [{"action": "create", "data": {"id": rid, "type": "node", "position": {"x": 1050, "y": 0}}}])
        for nm in ((j.get("data") or {}).get("nodesMap") or []):
            self.root_la = nm["laId"]
        self.graph_id, self.graph_layer = g, l
        return "https://mw.simulator.company/actors_graph/%s/graph/%s/layers/%s" % (p.ws, g, l)

    def _place_actor(self, aid):
        """Place one actor node on the layer and branch it from the root — LIVE fill."""
        if not self.graph_layer or aid in self.node_la: return
        n = self.node_count; self.node_count += 1
        x = 150 + (n % 14) * 150; y = 150 + (n // 14) * 95
        st, j = self.p("POST", "/graph_layers/actors/" + self.graph_layer,
                       [{"action": "create", "data": {"id": aid, "type": "node", "position": {"x": x, "y": y}}}])
        la = None
        for nm in ((j.get("data") or {}).get("nodesMap") or []):
            la = nm["laId"]
        if not la: return
        self.node_la[aid] = la
        if self.root_la:
            st2, j2 = self.p("POST", "/actors/link/" + self.p.ws,
                             {"source": self.actor_id["root"]["id"], "target": aid, "edgeTypeId": 13})
            eid = (j2.get("data") or {}).get("id") or j2.get("id")
            if eid:
                self.p("POST", "/graph_layers/actors/" + self.graph_layer,
                       [{"action": "create", "data": {"id": eid, "type": "edge",
                         "laIdSource": self.root_la, "laIdTarget": la}}])

    def build_edges(self, pending):
        """Place the real inter-actor relationship edges (Товар→Поставщик …) on the layer."""
        if not self.graph_layer: return
        eitems = []
        for e in pending:
            s = self.actor_id.get(e["source_ref"]); t = self.actor_id.get(e["target_ref"])
            if not (s and t) or s["id"] not in self.node_la or t["id"] not in self.node_la: continue
            eid = self.edge_id.get((s["id"], t["id"]))
            if eid:
                eitems.append({"action": "create", "data": {"id": eid, "type": "edge",
                               "laIdSource": self.node_la[s["id"]], "laIdTarget": self.node_la[t["id"]]}})
        for i in range(0, len(eitems), 100):
            self.p("POST", "/graph_layers/actors/" + self.graph_layer, eitems[i:i + 100])
        self.fact["graph_edges"] = len(eitems)


def op_stream_1cd(path):
    """Yield ops by running extractor.window() over all stages with the cursor loop.
    NOTE: a FRESH extractor per window() call — onec_dtools table iterators are one-shot,
    so sharing one extractor across stages/windows exhausts them (silent empty reads)."""
    import extractor as EX
    for stage, fn in EX.STAGES:
        if fn is None:
            continue
        cur = None
        while True:
            out = EX.window(path, stage, cur)
            for t in out["tasks"]:
                yield stage, t
            if out["done"]:
                break
            cur = out["next_cursor"]


def op_stream_xml(path):
    """Build op stream from EnterpriseData XML (universal, by tag; GUID-linked)."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    body = next((el for el in root.iter() if AN._local(el.tag) == "Body"), root)
    groups = collections.OrderedDict()
    for ch in list(body):
        groups.setdefault(AN._local(ch.tag), []).append(ch)

    def own_guid(el):
        # the record's own reference = first <Ссылка> directly under its key block
        for leaf in el.iter():
            if AN._local(leaf.tag) == "Ссылка" and leaf.text:
                return leaf.text.strip()
        return None

    # forms: fields = union of value-bearing leaf tags (excluding the Ссылка id)
    fmaps = {}
    for tag, els in groups.items():
        seen = collections.OrderedDict()
        for el in els:
            for leaf in el.iter():
                lt = AN._local(leaf.tag)
                if leaf.text and leaf.text.strip() and lt != tag and lt != "Ссылка":
                    seen[lt] = True
        fmap = {lt: "item_%d" % i for i, lt in enumerate(seen, 1)}
        fmaps[tag] = fmap
        flds = [{"id": v, "class": "edit", "title": k} for k, v in fmap.items()]
        yield "forms", {"op": "create_form", "ref": tag, "title": tag.split(".", 1)[-1],
                        "color": "#1864ab", "fields": flds}

    # guid -> owning record ref (ASCII-safe refs: e<tagIndex>_<rowIndex>)
    guid_owner = {}
    refs = {}
    for ti, (tag, els) in enumerate(groups.items()):
        for idx, el in enumerate(els):
            refs[(tag, idx)] = "e%d_%d" % (ti, idx)
            g = own_guid(el)
            if g:
                guid_owner.setdefault(g, refs[(tag, idx)])

    # actor shells
    for tag, els in groups.items():
        for idx, el in enumerate(els):
            title = next((AN._local(l.tag) == "Наименование" and l.text and l.text.strip()
                          for l in el.iter() if AN._local(l.tag) == "Наименование" and l.text), None)
            yield "actor_shells", {"op": "create_actor", "ref": refs[(tag, idx)],
                                   "form_ref": tag, "title": title or refs[(tag, idx)]}
    # fill data + links (GUID references to other records)
    for tag, els in groups.items():
        fmap = fmaps[tag]
        for idx, el in enumerate(els):
            ref = refs[(tag, idx)]; data = {}; og = own_guid(el)
            for leaf in el.iter():
                lt = AN._local(leaf.tag)
                if lt in fmap and leaf.text and leaf.text.strip():
                    data.setdefault(fmap[lt], leaf.text.strip())
                if lt == "Ссылка" and leaf.text:
                    g = leaf.text.strip()
                    if g != og and g in guid_owner:
                        yield "links", {"op": "create_link", "source_ref": ref,
                                        "target_ref": guid_owner[g], "name": tag.split(".", 1)[-1]}
            if data:
                yield "actor_data", {"op": "fill_actor", "ref": ref, "data": data}


def reconcile(b, plan, built):
    """Migration-case actor with PLAN/FACT accounts (accounts are primary).
    planned == built per category at the end => verified. Idempotent.
    `built` is the twin-count snapshot taken before this case form/accounts are made."""
    # case form (idempotent via builder.create_form: skips/recovers if exists)
    b.create_form({"op": "create_form", "ref": "__case__", "title": "Twin Case — " + b.name,
                   "color": "#e8590c",
                   "fields": [{"id": "item_1", "class": "edit", "title": "Файл"},
                              {"id": "item_2", "class": "edit", "title": "Формат"}]})
    fid = b.form_id.get(b.gref("__case__"))
    if not fid:
        return {"case_actor": None, "reconcile": {}}
    cref = b.gref("case")
    caid = b._actor_by_ref(fid, cref)
    if not caid:
        _, j = b.p("POST", "/actors/actor/%s" % fid, {"ref": cref, "ownerId": b.owner_id,
            "title": "Twin: " + b.name, "data": {"item_1": plan["file"], "item_2": plan["format"]}})
        caid = (j.get("data") or {}).get("id") or j.get("id")
    b.actor_id["__case__"] = {"id": caid, "formId": fid}
    rec = {}
    for cat in ["forms", "actors", "filled", "links", "txns"]:
        planned = int(plan["planned"].get(cat, 0)); fact = int(built.get(cat, 0))
        b.set_balance("__case__", "PLAN " + cat, "count", planned)
        b.set_balance("__case__", "FACT " + cat, "count", fact)
        rec[cat] = {"planned": planned, "built": fact, "match": planned == fact}
    return {"case_actor": caid, "reconcile": rec}


def fetch_to_temp(source):
    """A local path is returned as-is; an http(s) URL is streamed to a temp file
    (so big cloud-hosted bases never sit in memory). Returns a local filesystem path."""
    if not re.match(r"^https?://", str(source)):
        return source
    name = os.path.basename(urllib.parse.urlparse(source).path) or "download"
    fd, tmp = tempfile.mkstemp(suffix="_" + name); os.close(fd)
    req = urllib.request.Request(source, headers={"User-Agent": "twin-gitcall/1.0"})
    with urllib.request.urlopen(req, context=CTX, timeout=180) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk: break
            f.write(chunk)
    return tmp


def plan_ops(path, fmt, rep, scope, cap):
    """Materialize the universal op stream for a file and apply scope+cap.
    Returns (ops, plan_dict). Pure — no network."""
    if fmt == "1cd":
        raw = list(op_stream_1cd(path))
    elif fmt.startswith("xml"):
        raw = list(op_stream_xml(path))
    else:  # cf / dt: structure-only forms from analyzer entities
        raw = [("forms", {"op": "create_form", "ref": e["key"], "title": e["name"],
                          "color": "#868e96", "fields": []}) for e in rep["entities"]]
    cap_seen = collections.Counter(); capped_out = set(); ops = []
    for stage, op in raw:
        kind = op.get("op")
        if scope == "structure" and kind != "create_form":
            continue
        if kind == "create_actor":
            if cap and cap_seen[op["form_ref"]] >= cap:
                capped_out.add(op["ref"]); continue
            cap_seen[op["form_ref"]] += 1
        if kind in ("fill_actor",) and op["ref"] in capped_out:
            continue
        if kind == "create_link" and (op["source_ref"] in capped_out or op["target_ref"] in capped_out):
            continue
        if kind == "transfer" and any(l["actor_ref"] in capped_out for l in op.get("from", []) + op.get("to", [])):
            continue
        ops.append(op)
    plan_c = collections.Counter()
    for op in ops:
        plan_c[{"create_form": "forms", "create_actor": "actors", "fill_actor": "filled",
                "create_link": "links", "transfer": "txns"}.get(op.get("op"), "other")] += 1
    plan = {"file": rep["file"], "format": fmt, "identified": rep["metrics"],
            "planned": {k: plan_c.get(k, 0) for k in ("forms", "actors", "filled", "links", "txns")}}
    return ops, plan


def build(source, name=None, scope="full", cap=0, do_graph=True,
          token=None, ws=None, base="https://mw.simulator.company/papi/1.0",
          owner_id=66423, on_progress=None):
    """Programmatic twin build — used by the Corezoid GIT Call `usercode` and by the CLI.
    `source` may be a local path or http(s) URL; token+ws are supplied (no .env read).
    Returns the summary dict (plan + built + reconcile + graph_url). Idempotent (ref-native)."""
    path = fetch_to_temp(source)
    rep = AN.analyze(path)
    name = name or os.path.splitext(os.path.basename(path))[0]
    fmt = rep["format"]
    ops, plan = plan_ops(path, fmt, rep, scope, cap)
    if on_progress: on_progress({"PLAN": plan})

    papi = PAPI(token, ws, base); b = Builder(papi, name, cap, do_graph, owner_id)
    t0 = time.time(); pending_links = []
    graph_url = b.setup_graph() if do_graph else None
    if graph_url and on_progress: on_progress({"GRAPH_URL": graph_url})
    order = {"create_form": 0, "create_actor": 1, "fill_actor": 2, "transfer": 3, "create_link": 4}
    for op in sorted(ops, key=lambda o: order.get(o.get("op"), 9)):
        k = op.get("op")
        if k == "create_form": b.create_form(op)
        elif k == "create_actor": b.create_actor(op)
        elif k == "fill_actor": b.fill_actor(op)
        elif k == "transfer": b.transfer(op)
        elif k == "create_link": pending_links.append(op)
    b.flush_links(pending_links)
    if do_graph: b.build_edges(pending_links)
    built_snapshot = dict(b.fact)   # twin counts BEFORE the reconcile case form/accounts
    out = reconcile(b, plan, built_snapshot)
    b.save_state()
    rec = out["reconcile"]
    summary = {"twin": name, "format": fmt, "papi_calls": papi.calls,
               "elapsed_s": round(time.time() - t0, 1),
               "plan": plan["planned"], "identified": plan["identified"],
               "built": built_snapshot, "reconcile": rec,
               "all_match": bool(rec) and all(v["match"] for v in rec.values()),
               "case_actor": out["case_actor"],
               "errors": b.errors[:15], "error_count": len(b.errors)}
    if graph_url:
        summary["graph_layer"] = b.graph_layer
        summary["graph_url"] = graph_url
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--cap", type=int, default=0, help="max actors per form (0 = all)")
    ap.add_argument("--name", default=None)
    ap.add_argument("--scope", default="full", choices=["structure", "full"])
    ap.add_argument("--graph", action="store_true")
    ap.add_argument("--plan-only", action="store_true")
    a = ap.parse_args()

    env = load_env()
    token = os.environ.get("SIM_APP_TOKEN") or env.get("SIM_APP_TOKEN") or env["ACCESS_TOKEN"]
    ws = env["WORKSPACE_ID"]

    def emit(o): print(json.dumps(o, ensure_ascii=False), flush=True)

    if a.plan_only:
        path = fetch_to_temp(a.file); rep = AN.analyze(path)
        _, plan = plan_ops(path, rep["format"], rep, a.scope, a.cap)
        print(json.dumps({"PLAN": plan}, ensure_ascii=False, indent=2)); return

    summary = build(a.file, name=a.name, scope=a.scope, cap=a.cap, do_graph=a.graph,
                    token=token, ws=ws, on_progress=emit)
    print(json.dumps({"RESULT": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
