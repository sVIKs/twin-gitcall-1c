#!/usr/bin/env python3
"""
Twin Extractor — разбирает 1С .1CD по СТАДИЯМ оконным курсором.
Один и тот же модуль исполняется в Corezoid Git Call и во внешнем сервисе.

Контракт окна:
  window(file, stage, cursor, max_bytes, max_secs) -> {tasks[], count, next_cursor, done}
  cursor = SCAN-позиция {"obj": <индекс объекта в стадии>, "row": <смещение строки>}
  окно закрывается по сериализованному размеру батча ИЛИ тайм-бюджету; прогресс гарантирован.

ВАЖНО: модуль ТОЛЬКО парсит и формирует пакеты. Ничего не создаёт в Simulator,
не трогает существующие процессы. Создание — дело twin-builder (REST).
"""
import sys, re, json, time, zlib, collections, hashlib
import onec_dtools

# ─────────────────────────── низкоуровневое чтение 1CD ───────────────────────────

def _bytes(b):
    if b is None: return b""
    return b.value if hasattr(b, "value") else bytes(b)

def _best_text(raw):
    cands = [raw]
    for w in (-15, 15, 47):
        try: cands.append(zlib.decompress(raw, w))
        except Exception: pass
    best = ""
    for c in cands:
        for enc in ("utf-8", "cp1251", "utf-16-le", "utf-16-be", "latin-1"):
            try: t = c.decode(enc)
            except Exception: continue
            score = t.count("{") + t.count("Reference") + t.count("Документ")
            if score > (best.count("{") + best.count("Reference") + best.count("Документ")):
                best = t
    return best

_ZERO16 = b"\x00" * 16


# ── v2: канонические классы сущностей (классификатор по kind+title) ──
# Ключи — контракт с dto-build/refmap (dtoref-form-*, dtoref-anchor-*).
CANON = [
    ("employees",      ("сотруд", "работн", "персонал", "employee", "кадр", "співроб")),
    ("positions",      ("должност", "посад", "position", "штатн")),
    ("counterparties", ("контрагент", "клиент", "клієнт", "поставщик", "постачальник",
                        "покупател", "покупц", "counterpart")),
    ("nomenclature",   ("номенклатур", "товар", "продукц", "nomenclature", "product")),
    ("warehouses",     ("склад", "warehouse")),
    ("organizations",  ("организац", "організац", "organization")),
    ("currencies",     ("валют", "currenc")),
    ("units",          ("единиц изм", "од. вим", "одиниц вим", "классификатор един", "unit")),
    ("accounts",       ("план счет", "план рахунк", "счета учет", "chart of accounts")),
]

def canon_class(kind, title):
    """kind+title → канонический класс. doc → documents, info → registers,
    справочник без совпадений → catalogs (динамическая форма DTO-1С-<Назва>)."""
    if kind == "doc":
        return "documents"
    if kind == "info":
        return "registers"
    low = (title or "").lower()
    for cls, keys in CANON:
        if any(k in low for k in keys):
            return cls
    return "catalogs"


class TwinExtractor:
    """Открывает 1CD, резолвит имена (DBNames + CONFIG), отдаёт объекты/строки по стадиям.
    v2: у каждого объекта cls (канонический класс); vt-строки НЕ объекты-акторы —
    они декомпозируются в проводки (stage_doc_line_txns); only_classes — scope-фильтр."""

    def __init__(self, path):
        self.path = path
        self._fh = open(path, "rb")
        self.db = onec_dtools.DatabaseReader(self._fh)
        self.tables = self.db.tables
        self._tci = {k.upper(): k for k in self.tables}  # case-insensitive table resolver (8.2/8.3 naming)
        self._dbnames()
        self._config_index()
        self._obj_cache = {}
        self._idrref_map = None
        self._regs = None
        self._agg = None
        self.only_classes = None   # v2: scope-фильтр {cls,...} или None (всё)

    def close(self):
        try: self._fh.close()
        except Exception: pass

    # ---- DBNames ----
    def _dbnames(self):
        raw = None
        for r in self.tables["PARAMS"]:
            d = r.as_dict()
            if d.get("FILENAME") == "DBNames":
                raw = _bytes(d.get("BINARYDATA")); break
        txt = _best_text(raw) if raw else ""
        tr = re.findall(r'([0-9a-f]{8}-[0-9a-f-]{27}),"([A-Za-z]+)",(\d+)', txt)
        self.ref_idx  = {int(n): u for u, ty, n in tr if ty == "Reference"}
        self.doc_idx  = {int(n): u for u, ty, n in tr if ty == "Document"}
        self.enum_idx = {int(n): u for u, ty, n in tr if ty == "Enum"}
        self.acc_idx  = {int(n): u for u, ty, n in tr if ty == "AccumRg"}
        self.info_idx = {int(n): u for u, ty, n in tr if ty == "InfoRg"}
        self.fld_uuid = {u: int(n) for u, ty, n in tr if ty == "Fld"}
        self.dbnames_types = collections.Counter(ty for _, ty, _ in tr)  # все типы метаданных

    _UU = re.compile(r'[0-9a-f]{8}-[0-9a-f-]{27}')
    _NAME = re.compile(r'"([A-Za-zА-Яа-яЁёІіЇїЄєҐґ][\w А-Яа-яЁёІіЇїЄєҐґ]{1,40})"')

    # ---- CONFIG: декодируем ВСЕ конфиги ОДИН раз; строим тексты нужных объектов
    #      + ГЛОБАЛЬНУЮ карту _FLDnn→имя (имена полей регистров лежат в чужих конфигах). ----
    def _config_index(self):
        needed = (set(self.ref_idx.values()) | set(self.doc_idx.values())
                  | set(self.enum_idx.values()) | set(self.acc_idx.values()) | set(self.info_idx.values()))
        self._cfgtext = {}; self._gfld = {}
        for r in self.tables["CONFIG"]:
            d = r.as_dict(); fn = d.get("FILENAME") or ""
            m = re.match(self._UU, fn)
            text = _best_text(_bytes(d.get("BINARYDATA")))
            if not text: continue
            if m and m.group(0) in needed and m.group(0) not in self._cfgtext:
                self._cfgtext[m.group(0)] = text
            # глобальная карта имён полей: для каждого uuid-поля в тексте — ближайшее имя
            for mm in self._UU.finditer(text):
                u = mm.group(0)
                idx = self.fld_uuid.get(u)
                if idx is None: continue
                key = "_FLD%d" % idx
                if key in self._gfld: continue
                nm = self._NAME.findall(text[mm.end():mm.end() + 200])
                if nm: self._gfld[key] = nm[0]

    def _config_text(self, uuid): return self._cfgtext.get(uuid, "")

    def fld_name(self, col, fmap=None):
        """Имя поля: конфиг объекта → ГЛОБАЛЬНАЯ карта → чистый generic «Поле{n}» (не сырой _FLD..RREF)."""
        num = re.findall(r"\d+", col)
        if not num: return (fmap or {}).get(col, col)
        key = "_FLD" + num[0]
        return (fmap or {}).get(key) or self._gfld.get(key) or ("Поле" + num[0])

    @staticmethod
    def _first_name(cfg):
        for t in re.findall(r'[А-Яа-яЁёІіЇїЄєҐґ][А-Яа-яЁёІіЇїЄєҐґ ]{2,40}', cfg):
            t = t.strip()
            if len(set(t.replace(" ", ""))) <= 1: continue
            return t
        return None

    def _field_names(self, cfg):
        """Имена полей из КОНФИГА ОБЪЕКТА (только реальные; без fallback — иначе перекрывает глоб.карту)."""
        fm = {}
        for m in self._UU.finditer(cfg):
            u = m.group(0)
            if u in self.fld_uuid:
                names = self._NAME.findall(cfg[m.end():m.end() + 200])
                if names: fm["_FLD" + str(self.fld_uuid[u])] = names[0]
        return fm

    # стандартные поля по типу объекта (физ.имена платформы 1С — универсальны)
    STD = {"ref": {"_CODE": "Код", "_DESCRIPTION": "Наименование"},
           "doc": {"_NUMBER": "Номер", "_DATE_TIME": "Дата", "_POSTED": "Проведён"},
           "info": {"_PERIOD": "Период"},
           "vt": {"_LINENO": "Строка"}}
    SYS_EXACT = {"_IDRREF", "_VERSION", "_MARKED", "_PREDEFINEDID", "_FOLDER",
                 "_PARENTIDRREF", "_NUMBERPREFIX", "_ACTIVE",
                 "_KEYFIELD", "_RECORDKIND", "_RECORDERRREF", "_RECORDERTREF"}
    SKIP_RE = re.compile(r"_LINENO\d+$|_(RTREF|RRREF)$|_FLD\d+_(TYPE|L|N|T|S)$")  # системные/составные
    LINKNAME = {"_OWNERIDRREF": "Владелец"}   # стандартные имена ссылок

    COLOR = {"ref": "#37b24d", "doc": "#4263eb", "info": "#ae3ec9", "vt": "#1098ad"}

    def _build(self, kind, phys, scope, ref_form, default_title, keycol, idcol, fmap, cfg, parent_field=None):
        std = self.STD.get(kind, {})
        human = self._first_name(cfg) or default_title
        cols = list(self.tables[phys].fields.keys())
        form_fields, colmap, rref, n = [], {}, [], 0
        for col in cols:
            if parent_field and col == parent_field:
                rref.append({"col": col, "name": "Документ"}); continue
            if col in std:
                title = std[col]
            elif col.endswith("RREF"):
                if col == "_IDRREF" or col in self.SYS_EXACT or self.SKIP_RE.search(col): continue
                nm = self.LINKNAME.get(col) or self.fld_name(col, fmap)
                rref.append({"col": col, "name": nm}); continue
            elif col in self.SYS_EXACT or self.SKIP_RE.search(col):
                continue
            else:
                title = self.fld_name(col, fmap)
            n += 1; k = "item_%d" % n
            form_fields.append({"id": k, "class": "edit", "title": title, "visibility": "visible"})
            colmap[col] = k
        return {"kind": kind, "phys": phys, "scope": scope, "ref_form": ref_form, "title": human,
                "color": self.COLOR.get(kind, "#868e96"), "form_fields": form_fields,
                "colmap": colmap, "rref": rref, "keycol": keycol, "idcol": idcol,
                "cls": canon_class(kind, human), "parent_col": parent_field}

    def _object(self, kind, idx):
        ck = (kind, idx)
        if ck in self._obj_cache: return self._obj_cache[ck]
        spec = {"ref":  ("_REFERENCE%d", self.ref_idx,  "c%d", "tmpl_ref%d",  "Справочник%d", "_CODE",   "_IDRREF"),
                "doc":  ("_DOCUMENT%d",  self.doc_idx,  "d%d", "tmpl_doc%d",  "Документ%d",   "_NUMBER", "_IDRREF"),
                "info": ("_INFORG%d",    self.info_idx, "i%d", "tmpl_info%d", "РегСведений%d", None,     None)}[kind]
        physf, idxmap, scopef, formf, deff, keycol, idcol = spec
        phys = self._tci.get((physf % idx).upper())
        if not phys: self._obj_cache[ck] = None; return None
        cfg = self._config_text(idxmap.get(idx, ""))
        obj = self._build(kind, phys, scopef % idx, formf % idx, deff % idx, keycol, idcol,
                          self._field_names(cfg), cfg)
        self._obj_cache[ck] = obj
        return obj

    def _vt_objects(self):
        """Табличные части документов _DOCUMENT<d>_VT<v>: имена полей берём из конфига родит. документа."""
        out = []
        for n in sorted(self.tables):
            m = re.match(r"_DOCUMENT(\d+)_VT(\d+)$", n, re.I)
            if not m: continue
            di, vi = int(m.group(1)), int(m.group(2))
            ck = ("vt", (di, vi))
            if ck in self._obj_cache:
                if self._obj_cache[ck]: out.append(self._obj_cache[ck])
                continue
            cfg = self._config_text(self.doc_idx.get(di, ""))  # имена полей VT — в конфиге документа
            doc = self._object("doc", di); dtitle = doc["title"] if doc else ("Документ%d" % di)
            obj = self._build("vt", n, "v%d_%d" % (di, vi), "tmpl_vt%d_%d" % (di, vi),
                              dtitle + " — строки", "_KEYFIELD", "_KEYFIELD",
                              self._field_names(cfg), cfg, parent_field="_DOCUMENT%d_IDRREF" % di)
            obj["cls"] = "doclines"          # v2: строки — проводки, не акторы
            self._obj_cache[ck] = obj; out.append(obj)
        # scope: строки следуют за документами (documents исключены → строк нет)
        if self.only_classes and "documents" not in self.only_classes:
            return []
        return out

    def class_counts(self):
        """v2: planned counts per канонический класс (акторы) + doclines (строки-проводки).
        Отдельный ридер — итераторы onec_dtools одноразовые."""
        planned = {}
        with open(self.path, "rb") as f2:
            db2 = onec_dtools.DatabaseReader(f2)
            for o in self.objects():
                try: n = len(list(db2.tables[o["phys"]]))
                except Exception: n = 0
                planned[o["cls"]] = planned.get(o["cls"], 0) + n
            lines = 0
            for vt in self._vt_objects():
                try: lines += len(list(db2.tables[vt["phys"]]))
                except Exception: pass
            if lines: planned["doclines"] = lines
        return planned

    def objects(self):
        """Объекты, дающие формы+акторы: справочники → документы → регистры сведений.
        v2: vt-строки ИСКЛЮЧЕНЫ (строка ≠ актор — она проводка, см. stage_doc_line_txns);
        only_classes фильтрует по каноническому классу (scope-выбор пользователя)."""
        out = []
        for i in sorted(self.ref_idx):
            o = self._object("ref", i);  out.append(o) if o else None
        for i in sorted(self.doc_idx):
            o = self._object("doc", i);  out.append(o) if o else None
        for i in sorted(self.info_idx):
            o = self._object("info", i); out.append(o) if o else None
        out = [o for o in out if o]
        if self.only_classes:
            out = [o for o in out if o.get("cls") in self.only_classes]
        return out

    @staticmethod
    def _val(v):
        if v is None: return None
        if isinstance(v, (str, int, float, bool)): return v
        if v.__class__.__name__ == "Blob": return None
        return str(v)

    def _keyval(self, v):
        if v is None: return None
        if isinstance(v, (bytes, bytearray)): return v.hex()
        return self._val(v)

    def row_valid(self, obj, d):
        idc = obj["idcol"]
        if idc: return d.get(idc) is not None
        return any(v is not None for v in d.values())     # info: запись непустая

    def row_ref(self, obj, d, rowindex=None):
        for cand in (obj["keycol"], "_IDRREF"):
            if cand:
                k = self._keyval(d.get(cand))
                if k: return "%s-%s" % (obj["scope"], k)
        return "%s-r%s" % (obj["scope"], rowindex if rowindex is not None else "x")

    def row_title(self, obj, d, rowindex=None):
        if obj["kind"] == "doc":
            t = ("%s %s" % (self._val(d.get("_NUMBER")) or "", self._val(d.get("_DATE_TIME")) or "")).strip()
            return t or self.row_ref(obj, d, rowindex)
        if obj["kind"] == "ref":
            return self._val(d.get("_DESCRIPTION")) or self.row_ref(obj, d, rowindex)
        return self.row_ref(obj, d, rowindex)

    # ---- регистры накопления: измерения/ресурсы ----
    def registers(self):
        if self._regs is not None: return self._regs
        out = []
        for idx in sorted(self.acc_idx):
            phys = self._tci.get(("_ACCUMRG%d" % idx).upper())
            if not phys: continue
            cfg = self._config_text(self.acc_idx[idx]); fm = self._field_names(cfg)
            name = self._first_name(cfg) or ("Регистр%d" % idx)
            cols = list(self.tables[phys].fields.keys())
            def hn(c):
                num = re.findall(r"\d+", c); return fm.get("_FLD" + num[0], c) if num else c
            dims = [c for c in cols if c.startswith("_FLD") and c.endswith("RREF")]
            dim_names = {c: hn(c) for c in dims}
            resources = []
            for c in cols:
                if c.startswith("_FLD") and not c.endswith("RREF"):
                    nm = hn(c)
                    kind_cur = "money" if re.search(r"сумм|цен|стоим|money|amount|вартіст", nm, re.I) else "units"
                    resources.append((c, nm, kind_cur))
            # измерение «Валюта» (если есть) → реальная валюта движения
            currency_dim = next((c for c in dims if re.search(r"валют|currenc|валюта", dim_names[c], re.I)), None)
            out.append({"idx": idx, "phys": phys, "name": name, "dims": dims,
                        "dim_names": dim_names, "resources": resources, "currency_dim": currency_dim,
                        "kind": "_RECORDKIND" in cols})
        self._regs = out
        return out

    def currency_names(self):
        """ref валюты → её название (из справочника, чей заголовок ~ «Валюты»). Динамически."""
        if getattr(self, "_curmap", None) is not None: return self._curmap
        m = {}
        for i in sorted(self.ref_idx):
            o = self._object("ref", i)
            if not o or not re.search(r"валют|currenc", o["title"], re.I): continue
            with open(self.path, "rb") as f2:
                for r in onec_dtools.DatabaseReader(f2).tables[o["phys"]]:
                    d = r.as_dict()
                    if d.get("_IDRREF") is not None:
                        m[self.row_ref(o, d)] = self._val(d.get("_DESCRIPTION")) or self.row_ref(o, d)
        self._curmap = m
        return m

    def txn_currency(self, reg, d, kind_cur, idx):
        """Валюта движения: из измерения «Валюта» если есть (для денег); иначе базовая."""
        if kind_cur == "units":
            return "Единицы"
        if reg.get("currency_dim"):
            cv = d.get(reg["currency_dim"])
            if isinstance(cv, (bytes, bytearray)) and cv != _ZERO16:
                nm = self.currency_names().get(idx.get(cv.hex()))
                if nm: return nm
        return "Деньги"

    def aggregate_accounts(self):
        """Σ ресурсов регистра по актору первого измерения (знак по _RECORDKIND).
        {(actor_ref, account_name): {'amount':..,'currency':..}}. Скан на ОТДЕЛЬНОМ ридере."""
        if self._agg is not None: return self._agg
        idx = self.idrref_map(); agg = {}
        with open(self.path, "rb") as f2:
            db2 = onec_dtools.DatabaseReader(f2)
            for reg in self.registers():
                if not reg["dims"]: continue
                primary = reg["dims"][0]
                for r in db2.tables[reg["phys"]]:
                    d = r.as_dict(); pv = d.get(primary)
                    if not isinstance(pv, (bytes, bytearray)) or pv == _ZERO16: continue
                    ref = idx.get(pv.hex())
                    if not ref: continue
                    sign = 1
                    if reg["kind"]:
                        kd = d.get("_RECORDKIND")
                        sign = -1 if kd in (1, True) else 1
                    for col, nm, cur in reg["resources"]:
                        v = d.get(col)
                        if isinstance(v, (int, float)):
                            k = (ref, nm); cur0 = agg.get(k, {"amount": 0, "currency": cur})
                            cur0["amount"] += sign * v; agg[k] = cur0
        self._agg = agg
        return agg

    # ---- АУДИТ ПОЛНОТЫ: какие бизнес-объекты файла покрыты, что ещё нет ----
    # Бизнес-сущности перечислены в DBNames (это карта метаданные→хранилище).
    # «Лишние» физ.таблицы (_ACCUMRGOPT/_REFERENCECHNGR/итоги/оптимизация) — системные, не теряем.
    HANDLED = {"Reference", "Document", "AccumRg"}          # формы+акторы / счета+транзакции
    PENDING = {"Enum", "InfoRg", "Chrc", "Const"}           # ещё не извлекаем (бизнес-данные)
    def coverage(self):
        rep = {"handled": {}, "pending": {}, "vt_tables": 0, "vt_rows": 0}
        for ty, c in self.dbnames_types.items():
            bucket = "handled" if ty in self.HANDLED else ("pending" if ty in self.PENDING else None)
            if bucket: rep[bucket][ty] = c
        # табличные части документов _DOCUMENTn_VTm — строки-позиции, пока не извлекаем
        for n in self.tables:
            if re.match(r"_DOCUMENT\d+_VT\d+$", n):
                rep["vt_tables"] += 1
                try: rep["vt_rows"] += len(self.tables[n])
                except Exception: pass
        return rep

    @staticmethod
    def _form_ref_of_actor_ref(aref):
        m = re.match(r"([cd])(\d+)-", aref or "")
        if not m: return None
        return ("tmpl_doc%s" if m.group(1) == "d" else "tmpl_ref%s") % m.group(2)

    def reg_primary_form(self, reg, idx):
        """Форма каталога первого измерения регистра (резолв по образцу движения)."""
        if not reg["dims"]: return None
        primary = reg["dims"][0]
        with open(self.path, "rb") as f2:
            db2 = onec_dtools.DatabaseReader(f2)
            for r in db2.tables[reg["phys"]]:
                d = r.as_dict(); pv = d.get(primary)
                if isinstance(pv, (bytes, bytearray)) and pv != _ZERO16:
                    ref = idx.get(pv.hex())
                    if ref: return self._form_ref_of_actor_ref(ref)
        return None

    def idrref_map(self):
        """idrref(hex) → актор-ref по всем объектам. Для резолва связей. Строится один раз.
        ВАЖНО: итераторы таблиц onec_dtools ОДНОРАЗОВЫЕ — поэтому скан карты делаем на
        ОТДЕЛЬНОМ ридере файла, чтобы основной остался свежим для emit-прохода."""
        if self._idrref_map is not None: return self._idrref_map
        m = {}
        meta = {}
        with open(self.path, "rb") as f2:
            db2 = onec_dtools.DatabaseReader(f2)
            for obj in self.objects():
                for r in db2.tables[obj["phys"]]:
                    d = r.as_dict(); idr = d.get("_IDRREF")
                    if isinstance(idr, (bytes, bytearray)) and idr != _ZERO16:
                        ref = self.row_ref(obj, d)
                        m[idr.hex()] = ref
                        meta[ref] = (obj["cls"], obj["ref_form"])
        self._idrref_map = m
        self._ref_meta = meta
        return m

    def ref_meta(self):
        """v2: ref → (cls, ref_form) для reconcile (резолв формы актора по baseline-ref)."""
        if getattr(self, "_ref_meta", None) is None:
            self.idrref_map()
        return self._ref_meta


# ─────────────────────────── СТАДИИ (оконный курсор) ───────────────────────────

def _forms(kind):
    def fn(ex, cursor, push, budget):
        objs = [o for o in ex.objects() if o["kind"] == kind]
        i = (cursor or {}).get("obj", 0)
        while i < len(objs):
            if budget.over(): return {"obj": i}, False
            o = objs[i]; i += 1
            push({"op": "create_form", "ref": o["ref_form"], "title": o["title"],
                  "color": o["color"], "fields": o["form_fields"], "cls": o["cls"]})
        return {"obj": i}, True
    return fn

def _iter_rows(ex, cursor, push, budget, make):
    """Обход строк ВСЕХ объектов (ref/doc/vt/info). make(obj,d,rowindex)->пакет|список|None."""
    objs = ex.objects()
    i = (cursor or {}).get("obj", 0); r0 = (cursor or {}).get("row", 0)
    while i < len(objs):
        obj = objs[i]; rows = list(ex.tables[obj["phys"]]); r = r0
        while r < len(rows):
            if budget.over(): return {"obj": i, "row": r}, False
            d = rows[r].as_dict(); ri = r; r += 1
            if not ex.row_valid(obj, d): continue
            t = make(obj, d, ri)
            if t is None: continue
            if isinstance(t, list):
                for x in t: push(x)
            else:
                push(t)
        i += 1; r0 = 0
    return {"obj": i, "row": 0}, True

def _row_std(ex, o, d):
    """v2: семантические стандартные поля строки — для маппинга на канонические DTO-формы
    (их item_N не совпадают с парсерскими colmap item_N)."""
    std = {}
    for col, key in (("_CODE", "code"), ("_DESCRIPTION", "name"), ("_NUMBER", "number"),
                     ("_DATE_TIME", "date"), ("_POSTED", "posted")):
        v = ex._val(d.get(col)) if col in d else None
        if v not in (None, ""): std[key] = v
    return std

def stage_actor_shells(ex, cursor, push, budget):
    return _iter_rows(ex, cursor, push, budget,
        lambda o, d, ri: {"op": "create_actor", "ref": ex.row_ref(o, d, ri),
                          "form_ref": o["ref_form"], "title": ex.row_title(o, d, ri),
                          "cls": o["cls"], "std": _row_std(ex, o, d)})

def stage_actor_data(ex, cursor, push, budget):
    def mk(o, d, ri):
        data = {}
        for col, key in o["colmap"].items():
            v = ex._val(d.get(col))
            if v not in (None, ""): data[key] = v
        if not data: return None
        t = {"op": "fill_actor", "ref": ex.row_ref(o, d, ri), "form_ref": o["ref_form"],
             "data": data, "cls": o["cls"], "std": _row_std(ex, o, d)}
        if o["cls"] == "employees":     # v2: посада/оклад — для тройки персона↔посада
            pos = sal = None
            for col in d:
                nm = (ex.fld_name(col) or "").lower()
                v = ex._val(d.get(col))
                if v in (None, ""): continue
                if pos is None and re.search(r"должност|посад|position|role", nm): pos = str(v)
                if sal is None and re.search(r"оклад|зарплат|salary|оплат", nm):
                    try: sal = float(v)
                    except (TypeError, ValueError): pass
            t["position"] = pos or "Співробітник"
            if sal: t["salary"] = sal
        return t
    return _iter_rows(ex, cursor, push, budget, mk)

def stage_links(ex, cursor, push, budget):
    """*RREF → рёбра, _PARENTIDRREF → иерархия, VT→документ, info→измерения. Резолв через idrref_map."""
    idx = ex.idrref_map()
    def mk(o, d, ri):
        src = ex.row_ref(o, d, ri); out = []
        par = d.get("_PARENTIDRREF")
        if isinstance(par, (bytes, bytearray)) and par != _ZERO16:
            tgt = idx.get(par.hex())
            if tgt and tgt != src: out.append({"op": "create_link", "source_ref": src, "target_ref": tgt, "name": "Родитель"})
        for fld in o["rref"]:
            v = d.get(fld["col"])
            if isinstance(v, (bytes, bytearray)) and v != _ZERO16:
                tgt = idx.get(v.hex())
                if tgt and tgt != src:
                    out.append({"op": "create_link", "source_ref": src, "target_ref": tgt, "name": fld["name"]})
        return out or None
    return _iter_rows(ex, cursor, push, budget, mk)

def stage_account_defs(ex, cursor, push, budget):
    """Контур-форма + контр-актор на регистр. Счета (имя+тип+валюта) заводятся ЛЕНИВО в builder
    при transfer (валют у регистра может быть несколько — form-default не годится)."""
    idx = ex.idrref_map(); regs = ex.registers()
    i = (cursor or {}).get("obj", 0)
    if i == 0:
        push({"op": "create_form", "ref": "tmpl_counter", "title": "Баланс-контур (двойная запись)",
              "color": "#f08c00", "fields": []})
    while i < len(regs):
        if budget.over(): return {"obj": i}, False
        reg = regs[i]; i += 1
        if not reg["resources"] or not ex.reg_primary_form(reg, idx): continue
        push({"op": "create_actor", "ref": "ctr%d" % reg["idx"], "form_ref": "tmpl_counter",
              "title": "Контур: " + reg["name"]})
    return {"obj": i}, True

def stage_account_txns(ex, cursor, push, budget):
    """Каждое движение регистра → СБАЛАНСИРОВАННЫЙ transfer (двойная запись).
    receipt(_RECORDKIND=0): debit контур → credit актор; expense(=1): наоборот. Σfrom==Σto."""
    idx = ex.idrref_map(); regs = ex.registers()
    i = (cursor or {}).get("obj", 0); r0 = (cursor or {}).get("row", 0)
    while i < len(regs):
        reg = regs[i]
        if not reg["dims"] or not reg["resources"]:
            i += 1; r0 = 0; continue
        primary = reg["dims"][0]; cref = "ctr%d" % reg["idx"]
        with open(ex.path, "rb") as f2:
            rows = list(onec_dtools.DatabaseReader(f2).tables[reg["phys"]])
        r = r0
        while r < len(rows):
            if budget.over(): return {"obj": i, "row": r}, False
            d = rows[r].as_dict(); ri = r; r += 1
            pv = d.get(primary)
            if not isinstance(pv, (bytes, bytearray)) or pv == _ZERO16: continue
            aref = idx.get(pv.hex())
            if not aref: continue
            receipt = (d.get("_RECORDKIND") in (0, False, None)) if reg["kind"] else True
            for col, name, kind_cur in reg["resources"]:
                v = d.get(col)
                if not isinstance(v, (int, float)) or v == 0: continue
                amt = abs(v)
                currency = ex.txn_currency(reg, d, kind_cur, idx)   # реальная валюта (из изм. «Валюта»)
                deb, cred = (cref, aref) if receipt else (aref, cref)
                dna = "p-" + hashlib.sha256(("reg%d|%s|%s|%s|%s" % (
                    reg["idx"], aref, name, ri, amt)).encode("utf-8")).hexdigest()[:26]
                push({"op": "transfer", "ref": dna,
                      "currency": currency, "account_name": name, "account_type": "fact",
                      "from": [{"actor_ref": deb, "account_name": name, "amount": amt}],
                      "to":   [{"actor_ref": cred, "account_name": name, "amount": amt}]})
        i += 1; r0 = 0
    return {"obj": i, "row": 0}, True


def stage_doc_line_txns(ex, cursor, push, budget):
    """v2, заповедь: каждая строка табличной части документа → ОТДЕЛЬНЫЕ проводки (dna-ref):
    деньги — контур «Документообіг» → счёт «Сума» документа (итог сходится сам, история
    построчная); количество — контур → «Залишок» целевого справочника (номенклатуры);
    + ребро документ→номенклатура (dedup на исполнителе). Строка НЕ актор."""
    idx = ex.idrref_map()
    vts = ex._vt_objects()
    i = (cursor or {}).get("obj", 0); r0 = (cursor or {}).get("row", 0)
    if i == 0 and r0 == 0 and vts:
        push({"op": "create_actor", "ref": "ctrdoc", "form_ref": "tmpl_counter",
              "title": "Контур: Документообіг", "cls": "registers"})
    while i < len(vts):
        vt = vts[i]
        # колонки суммы/количества по человеческим именам
        sumcol = qtycol = None
        for col in ex.tables[vt["phys"]].fields.keys():
            nm = (ex.fld_name(col) or "").lower()
            if sumcol is None and re.search(r"сумм|сума|вартіст|стоим|sum", nm): sumcol = col
            if qtycol is None and re.search(r"колич|кільк|qty", nm): qtycol = col
        with open(ex.path, "rb") as f2:
            rows = list(onec_dtools.DatabaseReader(f2).tables[vt["phys"]])
        r = r0
        while r < len(rows):
            if budget.over(): return {"obj": i, "row": r}, False
            d = rows[r].as_dict(); ri = r; r += 1
            par = d.get(vt["parent_col"]) if vt.get("parent_col") else None
            docref = idx.get(par.hex()) if isinstance(par, (bytes, bytearray)) and par != _ZERO16 else None
            if not docref: continue
            lineno = ex._val(d.get("_LINENO")) or (ri + 1)
            # цель-справочник (номенклатура и т.п.): первый RREF в ref-объект
            tgt = None
            for fld in vt["rref"]:
                v = d.get(fld["col"])
                if isinstance(v, (bytes, bytearray)) and v != _ZERO16:
                    t = idx.get(v.hex())
                    if t and t.startswith("c"): tgt = (t, fld["name"]); break
            amt = d.get(sumcol) if sumcol else None
            if not isinstance(amt, (int, float)):
                nums = [v for c, v in d.items() if isinstance(v, (int, float))
                        and "_LINENO" not in str(c).upper()]
                amt = max(nums) if nums else None
            if isinstance(amt, (int, float)) and amt:
                a = abs(float(amt))
                dna = "p-" + hashlib.sha256(("%s|Сума|%s|%s" % (docref, lineno, a)
                                             ).encode("utf-8")).hexdigest()[:26]
                push({"op": "transfer", "ref": dna, "currency": "Деньги",
                      "account_name": "Сума", "account_type": "fact",
                      "from": [{"actor_ref": "ctrdoc", "account_name": "Сума", "amount": a}],
                      "to":   [{"actor_ref": docref, "account_name": "Сума", "amount": a}],
                      "data": {"doc": docref, "lineno": lineno}})
            q = d.get(qtycol) if qtycol else None
            if isinstance(q, (int, float)) and q and tgt:
                qa = abs(float(q))
                dna = "p-" + hashlib.sha256(("%s|Залишок|%s|%s|%s" % (docref, tgt[0], lineno, qa)
                                             ).encode("utf-8")).hexdigest()[:26]
                push({"op": "transfer", "ref": dna, "currency": "Единицы",
                      "account_name": "Залишок", "account_type": "fact",
                      "from": [{"actor_ref": "ctrdoc", "account_name": "Залишок", "amount": qa}],
                      "to":   [{"actor_ref": tgt[0], "account_name": "Залишок", "amount": qa}],
                      "data": {"doc": docref, "lineno": lineno}})
            if tgt:
                push({"op": "create_link", "source_ref": docref, "target_ref": tgt[0],
                      "name": tgt[1], "dedup": True})
        i += 1; r0 = 0
    return {"obj": i, "row": 0}, True

STAGES = [
    ("forms_catalogs", _forms("ref")),
    ("forms_documents", _forms("doc")),
    ("forms_registers", _forms("info")), # регистры сведений
    ("enums", None),              # TODO: _ENUMn (значения из CONFIG; поля сейчас text, не select)
    ("actor_shells", stage_actor_shells),
    ("actor_data", stage_actor_data),
    ("account_defs", stage_account_defs),       # счета на акторах + контр-акторы (двойная запись)
    ("account_txns", stage_account_txns),       # движения регистров → balanced transfers (dna-ref)
    ("doc_line_txns", stage_doc_line_txns),     # v2: строки документов → проводки (декомпозиция)
    ("links", stage_links),
]
STAGE_FN = dict(STAGES)


class _Budget:
    def __init__(self, max_bytes, max_secs):
        self.max_bytes, self.max_secs = max_bytes, max_secs
        self.t0, self.size = time.time(), 0
    def add(self, n): self.size += n
    def over(self): return self.size >= self.max_bytes or (time.time() - self.t0) >= self.max_secs


def window(file_url, stage, cursor=None, max_bytes=1_400_000, max_secs=25, _ex=None, classes=None):
    if stage not in STAGE_FN: raise ValueError("unknown stage: %s" % stage)
    fn = STAGE_FN[stage]
    tasks, budget = [], _Budget(max_bytes, max_secs)
    meta = (cursor or {}).get("_meta", {})
    def push(t):
        if meta: t.update(meta)
        tasks.append(t); budget.add(len(json.dumps(t, ensure_ascii=False)))
    if fn is None:
        return {"tasks": [], "count": 0, "next_cursor": cursor or {}, "done": True, "stage": stage, "todo": True}
    ex = _ex or TwinExtractor(file_url)
    if classes: ex.only_classes = set(classes)
    try:
        nxt, done = fn(ex, cursor, push, budget)
    finally:
        if _ex is None: ex.close()
    return {"tasks": tasks, "count": len(tasks), "next_cursor": nxt, "done": done, "stage": stage}


if __name__ == "__main__":
    f = sys.argv[1] if len(sys.argv) > 1 else "1c-demo/8-3-8_8K.1CD"
    stage = sys.argv[2] if len(sys.argv) > 2 else "forms_catalogs"
    cur = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
    out = window(f, stage, cur)
    print(json.dumps({"stage": out["stage"], "count": out["count"], "done": out["done"],
                      "next_cursor": out["next_cursor"], "sample": out["tasks"][:5]},
                     ensure_ascii=False, indent=2))
