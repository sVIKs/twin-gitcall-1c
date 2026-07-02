#!/usr/bin/env python3
"""Inline GIT Call parser for Corezoid (lang=python, entrypoint `handle(data)`).

PASTE THE BODY OF THIS FILE into a GIT Call node (inline `code`/`src`). Stdlib only —
no pip deps — so it parses EnterpriseData XML (and is the basis for CSV). Binary
.1CD/.dt/.cf need onec_dtools (a repo, not inline) — handled separately.

Contract (matches the api_rpc the executor uses):
  IN  data: source_url (http/https) OR xml_text (raw), scope ("full"|"structure"), cursor
  OUT data: tasks[] (dependency-ordered), cursor, done(bool), format, count, twin_error?

Tasks are dependency-ordered: create_form → create_actor → fill_actor → create_link.
XML is small → one window, done=true. Cursor reserved for future chunking.
"""
import json, ssl, urllib.request


def handle(data):
    try:
        scope = (data.get("scope") or "full").lower()
        text = data.get("xml_text")
        if not text:
            url = data.get("source_url") or data.get("source")
            if not url:
                data["twin_error"] = "missing source_url / xml_text"; data["tasks"] = []; data["done"] = True
                return data
            ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url, headers={"User-Agent": "twin-gitcall/1.0"})
            text = urllib.request.urlopen(req, context=ctx, timeout=60).read().decode("utf-8", "replace")

        import xml.etree.ElementTree as ET
        import collections

        def local(tag):
            return tag.split("}", 1)[-1] if "}" in tag else tag

        root = ET.fromstring(text)
        body = next((el for el in root.iter() if local(el.tag) == "Body"), root)
        groups = collections.OrderedDict()
        for ch in list(body):
            groups.setdefault(local(ch.tag), []).append(ch)

        def own_guid(el):
            for leaf in el.iter():
                if local(leaf.tag) == "Ссылка" and leaf.text:
                    return leaf.text.strip()
            return None

        tasks = []
        # forms: fields = union of value-bearing leaf tags (excluding the Ссылка id)
        fmaps = {}
        for tag, els in groups.items():
            seen = collections.OrderedDict()
            for el in els:
                for leaf in el.iter():
                    lt = local(leaf.tag)
                    if leaf.text and leaf.text.strip() and lt != tag and lt != "Ссылка":
                        seen[lt] = True
            fmap = {lt: "item_%d" % i for i, lt in enumerate(seen, 1)}
            fmaps[tag] = fmap
            flds = [{"id": v, "class": "edit", "title": k} for k, v in fmap.items()]
            tasks.append({"op": "create_form", "ref": tag, "title": tag.split(".", 1)[-1],
                          "color": "#1864ab", "fields": flds})

        if scope != "structure":
            # guid -> owning record ref (ASCII-safe refs e<ti>_<idx>)
            guid_owner, refs = {}, {}
            for ti, (tag, els) in enumerate(groups.items()):
                for idx, el in enumerate(els):
                    refs[(tag, idx)] = "e%d_%d" % (ti, idx)
                    g = own_guid(el)
                    if g:
                        guid_owner.setdefault(g, refs[(tag, idx)])
            # actor shells
            for tag, els in groups.items():
                for idx, el in enumerate(els):
                    title = next((l.text.strip() for l in el.iter()
                                  if local(l.tag) == "Наименование" and l.text and l.text.strip()), None)
                    tasks.append({"op": "create_actor", "ref": refs[(tag, idx)], "form_ref": tag,
                                  "title": title or refs[(tag, idx)]})
            # fills
            for tag, els in groups.items():
                fmap = fmaps[tag]
                for idx, el in enumerate(els):
                    d = {}
                    for leaf in el.iter():
                        lt = local(leaf.tag)
                        if lt in fmap and leaf.text and leaf.text.strip():
                            d.setdefault(fmap[lt], leaf.text.strip())
                    if d:
                        tasks.append({"op": "fill_actor", "ref": refs[(tag, idx)], "form_ref": tag, "data": d})
            # links (GUID references to other records)
            for tag, els in groups.items():
                for idx, el in enumerate(els):
                    ref = refs[(tag, idx)]; og = own_guid(el)
                    for leaf in el.iter():
                        if local(leaf.tag) == "Ссылка" and leaf.text:
                            g = leaf.text.strip()
                            if g != og and g in guid_owner:
                                tasks.append({"op": "create_link", "source_ref": ref,
                                              "target_ref": guid_owner[g], "name": tag.split(".", 1)[-1]})

        data["tasks"] = tasks
        data["cursor"] = {"fmt": "xml", "done": True}
        data["done"] = True
        data["format"] = "xml"
        data["count"] = len(tasks)
        data.pop("twin_error", None)
    except Exception as e:
        data["tasks"] = []; data["done"] = True
        data["twin_error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
    return data


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "../1c-demo/employees-demo-bases/employees_1C_real.xml"
    out = handle({"xml_text": open(src, encoding="utf-8").read(), "scope": "full"})
    import collections
    tot = collections.Counter(t["op"] for t in out["tasks"])
    print(json.dumps({"format": out.get("format"), "count": out["count"], "done": out["done"],
                      "totals": dict(tot), "twin_error": out.get("twin_error"),
                      "first_ops": [t["op"] for t in out["tasks"][:6]]}, ensure_ascii=False, indent=2))
