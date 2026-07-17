# -*- coding: utf-8 -*-
"""
statements/usercode.py — git_call handler для банковских ВЫПИСОК (port-statements).

Зачем отдельно от filedoc: filedoc даёт PDF-текст через pypdf (плоский текст-слой). Для
ВЫПИСОК точнее pdfplumber — он сохраняет колоночную структуру (Rulaj/Sold, Debit/Credit),
что критично для running-balance-цепочки в validate. Здесь git_call ТОЛЬКО извлекает
(детерминированно, без OCR, без потерь); «понимание» банка делает LLM ниже по конвейеру,
а сверку — код (validate). Анти-фадж: тут чисел не трактуем.

ВХОД (file может прийти тремя способами, приоритет сверху вниз):
  A) file_handle (str) — ОПАК-хэндл Simulator-attachment (fileName из PAPI). Тогда мы сами
     скачиваем байты через PAPI download-роут с Bearer {{env_var[@sim-auth]}} и кладём в base64.
     Это путь для формы freepro: она грузит файл как attachment, НЕ как URL. См. resolveInputFile.
  B) file_b64 (str|list) + file_name — байты уже переданы вызывающим.
  C) source_url (http(s)) | path — качается urlretrieve / читается локально.

resolveInputFile PIPELINE (когда есть file_handle):
  handle → PAPI metadata → access/size/MIME check → PAPI download (Bearer) → SHA-256 → base64
  → parse. Прогресс статуса: FILE_HANDLE_RECEIVED → FILE_METADATA_VERIFIED → FILE_DOWNLOADED
  → FILE_HASHED → PARSER_STARTED. Гарды: MIME whitelist, size cap, токен только из env (не
  логируем), file_hash для идемпотентности, ошибка скачивания → STOP (out.twin_error + fetch_status).

STEP-контракт на data: {file_handle, file_name, mime_type, file_size, file_hash:"sha256:...",
  file_b64, fetch_status}. file_b64 держим в data но нигде не печатаем/эхоим.

OUT: data.statements = {found, format, n_pages, n_chars, text, pages, tables, date_line_count,
  sampled, twin_error, lib_status} + зеркалит text/format/n_pages + контракт resolveInputFile.
Размер-гард: текст режется до ~1.3МБ (reply-лимит); при переполнении sampled=True.
"""
import base64
import hashlib
import json
import os
import re
import tempfile

try:
    from urllib.request import urlopen, urlretrieve, Request
    from urllib.error import HTTPError, URLError
except Exception:  # py2 fallback (не ожидается)
    from urllib import urlretrieve
    from urllib2 import urlopen, Request, HTTPError, URLError

MAX_TEXT = 1_300_000            # держим reply < 1.4МБ
DATE_RX = re.compile(r"^\s*(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}[./-]\d{2}[./-]\d{2})\b")

# --- resolveInputFile гарды ------------------------------------------------
PAPI_BASE_DEFAULT = "https://mw.simulator.company/papi/1.0"
MAX_FILE_BYTES = 8 * 1024 * 1024       # 8 МБ cap
# MIME/расширения-whitelist: PDF / XLS / XLSX / CSV. Simulator часто отдаёт
# application/octet-stream — тогда решаем по расширению или magic-байтам.
ALLOWED_EXT = {"pdf", "xls", "xlsx", "csv"}
ALLOWED_MIME = {
    "application/pdf",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv", "application/csv",
}


def _sniff_ext(raw, name):
    """Определить расширение по имени, затем по magic-байтам (октет-стрим-фолбэк)."""
    ext = os.path.splitext(name or "")[1].lower().lstrip(".")
    if ext in ALLOWED_EXT:
        return ext
    if raw[:4] == b"%PDF":
        return "pdf"
    if raw[:2] == b"PK":                       # zip-контейнер → xlsx
        return "xlsx"
    if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # OLE2 → xls
        return "xls"
    return ext or ""


def _papi_get(url, token):
    """GET на PAPI c Bearer; вернуть (status, bytes, content_type). Токен НЕ логируем."""
    req = Request(url, headers={"Authorization": "Bearer " + token})
    try:
        resp = urlopen(req, timeout=60)
        ct = resp.headers.get("Content-Type", "") or ""
        data = resp.read(MAX_FILE_BYTES + 1)   # cap+1 чтобы поймать превышение
        code = getattr(resp, "status", None) or resp.getcode()
        return code, data, ct
    except HTTPError as e:
        body = b""
        try:
            body = e.read(4096)
        except Exception:
            pass
        return e.code, body, ""
    except URLError as e:
        raise RuntimeError("download transport error: %s" % (str(getattr(e, "reason", e))[:200]))


def resolve_input_file(data, out):
    """
    handle → metadata → checks → download → sha256 → base64. Идемпотентна по file_hash.
    Пишет контракт в data (file_name/mime_type/file_size/file_hash/file_b64/fetch_status) и
    out.resolve. При фейле — out.twin_error + data.fetch_status=<...>_ERROR и raise (STOP до materialize).
    Возвращает True если file_b64 готов к парсингу.
    """
    handle = str(data.get("file_handle") or "").strip()
    if not handle:
        return False

    token = str(data.get("sim_token") or data.get("_sim_auth") or "").strip()
    papi = str(data.get("papi_base") or PAPI_BASE_DEFAULT).rstrip("/")
    ws = str(data.get("wsId") or data.get("accId") or "").strip()

    data["fetch_status"] = "FILE_HANDLE_RECEIVED"
    data["file_handle"] = handle
    out["resolve"] = {"status": "FILE_HANDLE_RECEIVED"}

    if not token:
        data["fetch_status"] = "FILE_METADATA_ERROR"
        out["twin_error"] = "resolveInputFile: no @sim-auth token provided (sim_token empty)"
        raise RuntimeError(out["twin_error"])

    # --- metadata: список attachment'ов воркспейса, ищем по fileName == handle ---
    # (PAPI не даёт GET одного attachment по fileName; листаем воркспейс и матчим.
    #  Это же и есть access-check: 965177 увидит запись только если имеет доступ.)
    meta = None
    http_status = None
    if ws:
        try:
            m_status, m_body, _ = _papi_get(papi + "/attachments/" + ws + "?limit=100", token)
            http_status = m_status
            if m_status == 200:
                try:
                    rows = json.loads(m_body.decode("utf-8", "ignore")).get("data") or []
                except Exception:
                    rows = []
                for r in rows:
                    if str(r.get("fileName") or "") == handle:
                        meta = r
                        break
        except Exception:
            pass  # метадату не удалось — download сам покажет доступ (403/404)

    name = data.get("file_name") or (meta or {}).get("title") or os.path.basename(handle)
    if meta is not None:
        out["resolve"]["meta_status"] = http_status
        out["resolve"]["owner"] = (meta or {}).get("userId")
        declared_size = (meta or {}).get("size")
        if isinstance(declared_size, int) and declared_size > MAX_FILE_BYTES:
            data["fetch_status"] = "FILE_METADATA_ERROR"
            out["twin_error"] = "resolveInputFile: file too large (%s > %s)" % (declared_size, MAX_FILE_BYTES)
            raise RuntimeError(out["twin_error"])
        data["fetch_status"] = "FILE_METADATA_VERIFIED"
        out["resolve"]["status"] = "FILE_METADATA_VERIFIED"

    # --- download bytes через PAPI download-роут (raw binary) ---
    dl_url = papi + "/download/" + handle
    status, raw, ct = _papi_get(dl_url, token)
    out["resolve"]["download_status"] = status
    if status == 403:
        data["fetch_status"] = "FILE_DOWNLOAD_FORBIDDEN"
        out["twin_error"] = "resolveInputFile: 403 FORBIDDEN — @sim-auth cannot read this attachment"
        raise RuntimeError(out["twin_error"])
    if status == 404:
        data["fetch_status"] = "FILE_DOWNLOAD_NOT_FOUND"
        out["twin_error"] = "resolveInputFile: 404 NOT_FOUND for handle"
        raise RuntimeError(out["twin_error"])
    if status != 200:
        data["fetch_status"] = "FILE_DOWNLOAD_ERROR"
        out["twin_error"] = "resolveInputFile: download HTTP %s" % status
        raise RuntimeError(out["twin_error"])
    if len(raw) > MAX_FILE_BYTES:
        data["fetch_status"] = "FILE_SIZE_EXCEEDED"
        out["twin_error"] = "resolveInputFile: downloaded bytes exceed cap (%s)" % MAX_FILE_BYTES
        raise RuntimeError(out["twin_error"])

    # MIME/формат whitelist
    ext = _sniff_ext(raw, name)
    base_mime = (ct.split(";")[0].strip().lower() if ct else "")
    ok_mime = base_mime in ALLOWED_MIME
    ok_ext = ext in ALLOWED_EXT
    if not (ok_mime or ok_ext):
        data["fetch_status"] = "FILE_MIME_REJECTED"
        out["twin_error"] = "resolveInputFile: MIME/ext not allowed (mime=%s ext=%s)" % (base_mime, ext)
        raise RuntimeError(out["twin_error"])
    mime_type = {"pdf": "application/pdf",
                 "xls": "application/vnd.ms-excel",
                 "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 "csv": "text/csv"}.get(ext, base_mime or "application/octet-stream")

    data["fetch_status"] = "FILE_DOWNLOADED"
    out["resolve"]["status"] = "FILE_DOWNLOADED"

    # --- sha256 (идемпотентность) ---
    digest = hashlib.sha256(raw).hexdigest()
    file_hash = "sha256:" + digest
    data["fetch_status"] = "FILE_HASHED"
    out["resolve"]["status"] = "FILE_HASHED"

    # обеспечить .<ext>-имя чтобы фолбэки по расширению работали
    if ext and not str(name).lower().endswith("." + ext):
        name = (os.path.splitext(name)[0] or "file") + "." + ext

    # контракт (file_b64 держим, но не эхоим в лог)
    data["file_name"] = name
    data["mime_type"] = mime_type
    data["file_size"] = len(raw)
    data["file_hash"] = file_hash
    data["file_b64"] = base64.b64encode(raw).decode("ascii")
    out["resolve"].update({
        "file_name": name, "mime_type": mime_type, "file_size": len(raw),
        "file_hash": file_hash, "status": "PARSER_STARTED",
    })
    data["fetch_status"] = "PARSER_STARTED"
    # токен из data убираем чтобы не утёк ниже по конвейеру / в лог
    for k in ("sim_token", "_sim_auth"):
        if k in data:
            data[k] = ""
    return True


def _materialize(data):
    """(local_path, source_name, tmp_created). BIG файлы приходят через source_url."""
    name = data.get("file_name") or data.get("fileName") or ""
    b64 = (data.get("file_b64") or data.get("content_b64") or data.get("base64")
           or data.get("file_b64_parts"))
    if b64:
        s = b64
        if isinstance(s, (list, tuple)):
            s = "".join(str(x) for x in s)
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("ascii", "ignore")
        if s.startswith("data:") and "," in s:
            s = s.split(",", 1)[1]
        s = "".join(s.split())
        s += "=" * (-len(s) % 4)
        raw = base64.b64decode(s)
        fd, tmp = tempfile.mkstemp(suffix="_" + (name or "file.pdf"))
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        return tmp, (name or os.path.basename(tmp)), True
    url = (data.get("source_url") or data.get("url") or data.get("file_url") or "").strip()
    if url:
        base = name or os.path.basename(url.split("?", 1)[0]) or "file.pdf"
        fd, tmp = tempfile.mkstemp(suffix="_" + base)
        os.close(fd)
        urlretrieve(url, tmp)
        return tmp, base, True
    path = (data.get("path") or "").strip()
    if path:
        return path, (name or os.path.basename(path)), False
    raise RuntimeError("file_handle, file_b64 or source_url required")


def _extract_pdf(path):
    """pdfplumber: чистый текст + таблицы постранично. Возврат dict полей statements."""
    import pdfplumber
    pages, tables, total_dates, n_pages = [], [], 0, 0
    with pdfplumber.open(path) as pdf:
        n_pages = len(pdf.pages)
        for pg in pdf.pages:
            txt = pg.extract_text() or ""
            pages.append(txt)
            for ln in txt.splitlines():
                if DATE_RX.match(ln):
                    total_dates += 1
            # best-effort таблицы: сначала линии (lattice), потом текст (stream)
            try:
                for settings in ({"vertical_strategy": "lines", "horizontal_strategy": "lines"},
                                 {"vertical_strategy": "text", "horizontal_strategy": "text"}):
                    tbs = pg.extract_tables(settings)
                    if tbs:
                        tables.extend(tbs)
                        break
            except Exception:
                pass
    text = "\n".join(pages)
    sampled = False
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]
        sampled = True
    return {
        "found": True, "format": "pdf", "n_pages": n_pages, "n_chars": len(text),
        "text": text, "pages": pages, "tables": tables[:200],
        "date_line_count": total_dates, "sampled": sampled, "twin_error": None,
    }


# =========================================================================
# generic_pdf_statement_parser — детерминированный разбор ТЕКСТА банковской
# выписки: layout inference + мультиязычная нормализация операций +
# канонический выход. Это НЕ bank-specific парсер: движок один, банковская
# специфика живёт ТОЛЬКО в заменяемых extraction-профилях (данные: маркеры
# детекта, локализованные метки шапки, junk/eof-фильтры) на границе
# layout-извлечения. Токены нормализации — языковые (ro/it/en), не банковские.
#
# Зачем: LLM ниже по конвейеру хорош в «понимании», но не доказуем на 500
# строках. Этот слой — код с проверяемыми законами:
#   summary self-consistency: opening + total_credit - total_debit == closing
#   full sum law:             net(строк) == closing - opening  (полная выгрузка)
# Анти-фадж: числа НЕ выравниваем (никаких balancing/gap-транзакций, никакой
# подстановки declared closing); усечённый листинг (кап экспорта, как у BRD
# ровно 500 строк) фиксируется verdict'ом: coverageStatus=TRUNCATED_SUSPECTED,
# validationGrade=PARTIAL_TRUNCATED — НЕ VERIFIED.
#
# Обратная совместимость: контракт statements={found,format,...} НЕ меняется;
# при детекте профиля добавляется statements.deterministic (и зеркально
# data.deterministic). При недетекте поля просто нет.

_SOFT_HYPHEN = "­"   # U+00AD: в выписках печатается и как минус, и как дефис

# --- layout inference: общая геометрия построчных нумерованных выписок -------
# Начало операции: "N. dd.mm.yyyy <остаток строки>"
_OP_START = re.compile(r"^\s*(\d+)[.)]\s+(\d{2}\.\d{2}\.\d{4})\s+(.*)$")
# EU-число в конце строки: 1.991.732,26 / -458,27 (минус уже нормализован из U+00AD)
_AMOUNT_TAIL = re.compile(r"(-?\d{1,3}(?:\.\d{3})*,\d{1,2})\s*$")
# IBAN (страна-агностик): CC## + BBAN; свой счёт исключается отдельно
_IBAN_RX = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
# Дата валютирования в начале продолжающей строки
_VALUE_DATE = re.compile(r"^\s*(\d{2}\.\d{2}\.\d{4})\b\s*(.*)$")
# Референс клиента: OP…/банковские реф-коды (буквенный префикс + цифры)
_CLIENT_REF = re.compile(r"^(?:OP[A-Z0-9/]*\d[A-Z0-9/]*|[A-Z]{2,8}\d{2}[A-Z0-9/]{4,})$")
# Налоговый идентификатор контрагента (ro: CUI/CNP; it: CF/P.IVA)
_TAX_ID = re.compile(r"(?:CUI/CNP|CUI|CNP|C\.?F\.?|P\.?\s?IVA)\s*:?\s*(\d{2,13})")
# FX по «компактному» тексту блока (переносы рвут числа: "Curs: 5.26"+"92 RON/EUR")
_FX_RX = re.compile(r"Orig:\s*(\d+(?:\.\d+)?)\s*([A-Z]{3})\s*"
                    r"Curs:\s*(\d+(?:\.\d+)?)\s*([A-Z]{3})/([A-Z]{3})")
# Маркеры казначейства в IBAN (страновой уровень: RO Trezoreria)
_TREASURY_IBAN_MARKERS = ("TREZ",)

# --- multilingual operation normalization (ДАННЫЕ, не код-ветки) -------------
# (regex-префикс поля Detalii/Dettagli/Details, category, role, role_conf,
#  направление|None). Порядок ВАЖЕН: частное раньше общего. role_conf: 0.95 =
# детерминированный, 0.6 = weak (POS-merchant по тексту мерчанта).
OPERATION_NORMALIZATION = [
    # --- ro ---
    (re.compile(r"^Utiliz POS(?:\s+\S+\s+pl)?"), "purchase", "merchant", 0.6, "out"),
    (re.compile(r"^IncasareInst(?:\s+Pay)?"), "incoming_payment", "client", 0.95, "in"),
    (re.compile(r"^Incasare\S*"), "incoming_payment", "client", 0.95, "in"),
    (re.compile(r"^PlataInst(?:\s+Pay)?"), "outgoing_payment", "supplier", 0.95, "out"),
    (re.compile(r"^Pl Inst(?:\s+Paymnt)?"), "outgoing_payment", "supplier", 0.95, "out"),
    (re.compile(r"^PLATA\s+[A-Z]{2,4}\b"), "outgoing_payment", "supplier", 0.95, "out"),
    (re.compile(r"^Plata schimb val"), "fx_exchange", "bank", 0.95, "out"),
    (re.compile(r"^Tr\.Credit-Plata"), "outgoing_payment", "supplier", 0.95, "out"),
    (re.compile(r"^Plata\S*"), "outgoing_payment", "supplier", 0.95, "out"),
    (re.compile(r"^Retr Num[- ]?ATM(?:\s+[A-Z]\b)?"), "cash_withdrawal", "other", 0.95, "out"),
    (re.compile(r"^Retr Num\S*"), "cash_withdrawal", "other", 0.95, "out"),
    (re.compile(r"^Com Util ATM ret"), "fee", "bank", 0.95, "out"),
    (re.compile(r"^Com\.administrare"), "fee", "bank", 0.95, "out"),
    (re.compile(r"^ComisAdmPachet"), "fee", "bank", 0.95, "out"),
    (re.compile(r"^Comision"), "fee", "bank", 0.95, "out"),
    (re.compile(r"^Com\s"), "fee", "bank", 0.95, "out"),
    (re.compile(r"^CaShBck(?:\s+\S+)?"), "cashback", "bank", 0.95, "in"),
    (re.compile(r"^TransCr-Inc\S*"), "incoming_payment", "client", 0.95, "in"),
    (re.compile(r"^Depuneri numerar"), "cash_deposit", "other", 0.95, "in"),
    # --- it ---
    (re.compile(r"^Bonifico (?:ricevuto|in entrata)"), "incoming_payment", "client", 0.95, "in"),
    (re.compile(r"^Bonifico\b"), "outgoing_payment", "supplier", 0.95, "out"),
    (re.compile(r"^Pagamento POS"), "purchase", "merchant", 0.6, "out"),
    (re.compile(r"^Pagamento\b"), "outgoing_payment", "supplier", 0.95, "out"),
    (re.compile(r"^Prelievo\b"), "cash_withdrawal", "other", 0.95, "out"),
    (re.compile(r"^(?:Commissione|Canone)\b"), "fee", "bank", 0.95, "out"),
    (re.compile(r"^Versamento\b"), "cash_deposit", "other", 0.95, "in"),
    # --- en ---
    (re.compile(r"^(?:POS|Card) purchase"), "purchase", "merchant", 0.6, "out"),
    (re.compile(r"^Incoming transfer"), "incoming_payment", "client", 0.95, "in"),
    (re.compile(r"^(?:Outgoing transfer|Payment)\b"), "outgoing_payment", "supplier", 0.95, "out"),
    (re.compile(r"^ATM withdrawal"), "cash_withdrawal", "other", 0.95, "out"),
    (re.compile(r"^(?:Fee|Commission)\b"), "fee", "bank", 0.95, "out"),
    (re.compile(r"^Cash deposit"), "cash_deposit", "other", 0.95, "in"),
]

# --- extraction profiles: заменяемые layout-адаптеры (ДАННЫЕ) ----------------
# Профиль = детект шаблона + локализованные метки шапки + junk/eof строки
# страницы + известный кап листинга. Новый банк = новый профиль, движок общий.
STATEMENT_PROFILES = [
    {
        "name": "brd-extras",
        "lang": "ro",
        "detect": ("Extras de cont", "Detinator cont", "Sold initial"),
        "labels": {
            "period_from": re.compile(r"De la data de\s+(\d{2}\.\d{2}\.\d{4})"),
            "period_to": re.compile(r"Pana la data de\s+(\d{2}\.\d{2}\.\d{4})"),
            "holder": re.compile(r"Detinator cont[^\n]*\n(.+?)\s+"
                                 r"([A-Z]{2}\d{2}[A-Z0-9]{11,30})\s+([A-Z]{3})\s*$", re.M),
            "opening_debits": re.compile(r"Sold initial\s+(-?[\d.,]+)\s+Suma debite\s+(-?[\d.,]+)"),
            "closing_credits": re.compile(r"Sold final\s+(-?[\d.,]+)\s+Suma credite\s+(-?[\d.,]+)"),
        },
        "junk": re.compile(
            r"^\s*(Acest extras de cont|Tiparit in data de|Pagina \d+|Extras de cont"
            r"|De la data de|Pana la data de|Detinator cont|Sold initial|Sold final"
            r"|Nr\.\s+Data inregistrarii|Data valutei|IBAN\s*$)"),
        "eof": re.compile(r"^\s*(Informatii utile despre|Sfarsit lista)"),
        "listing_cap": 500,   # известный лимит экспорта строк BRD
    },
    {
        "name": "intesa-mini",
        "lang": "it",
        "detect": ("Estratto conto", "Intestatario conto", "Saldo iniziale"),
        "labels": {
            "period_from": re.compile(r"Dal\s+(\d{2}\.\d{2}\.\d{4})"),
            "period_to": re.compile(r"Al\s+(\d{2}\.\d{2}\.\d{4})"),
            "holder": re.compile(r"Intestatario conto[^\n]*\n(.+?)\s+"
                                 r"([A-Z]{2}\d{2}[A-Z0-9]{11,30})\s+([A-Z]{3})\s*$", re.M),
            "opening_debits": re.compile(r"Saldo iniziale\s+(-?[\d.,]+)\s+Totale uscite\s+(-?[\d.,]+)"),
            "closing_credits": re.compile(r"Saldo finale\s+(-?[\d.,]+)\s+Totale entrate\s+(-?[\d.,]+)"),
        },
        "junk": re.compile(r"^\s*(Estratto conto|Dal\s|Al\s|Intestatario conto"
                           r"|Saldo iniziale|Saldo finale|Nr\.\s+Data)"),
        "eof": re.compile(r"^\s*Fine lista"),
        "listing_cap": None,
    },
]


def _norm_text(text):
    """Нормализация извлечённого текста: soft hyphen U+00AD → '-'."""
    return (text or "").replace(_SOFT_HYPHEN, "-")


def _eu_num(s):
    """EU-число '1.991.732,26' → 1991732.26; '-458,27' → -458.27."""
    s = (s or "").strip().replace(_SOFT_HYPHEN, "-")
    neg = s.startswith("-")
    s = s.lstrip("-").replace(".", "").replace(",", ".")
    v = float(s)
    return -v if neg else v


def _iso_date(d):
    """'28.05.2026' → '2026-05-28'."""
    p = d.split(".")
    return "%s-%s-%s" % (p[2], p[1], p[0]) if len(p) == 3 else d


def _parse_statement_header(profile, text):
    """Шапка по меткам профиля: держатель, IBAN, валюта, период, сальдо, обороты."""
    h = {"company_name": None, "account_iban": None, "currency": None,
         "period_from": None, "period_to": None, "opening": None,
         "closing": None, "total_debit": None, "total_credit": None}
    lb = profile["labels"]
    m = lb["period_from"].search(text)
    if m:
        h["period_from"] = _iso_date(m.group(1))
    m = lb["period_to"].search(text)
    if m:
        h["period_to"] = _iso_date(m.group(1))
    m = lb["holder"].search(text)
    if m:
        h["company_name"] = m.group(1).strip()
        h["account_iban"] = m.group(2)
        h["currency"] = m.group(3)
    m = lb["opening_debits"].search(text)
    if m:
        h["opening"] = _eu_num(m.group(1))
        h["total_debit"] = abs(_eu_num(m.group(2)))   # печатается с минусом — берём модуль
    m = lb["closing_credits"].search(text)
    if m:
        h["closing"] = _eu_num(m.group(1))
        h["total_credit"] = abs(_eu_num(m.group(2)))
    return h


def _split_op_first_line(rest):
    """Первая строка операции после 'N. дата' → (detalii, counterparty, client_ref,
    amount|None, category, role, role_conf, направление|None).
    Layout: <тип операции> <контрагент> <референс клиента> <сумма>."""
    amount = None
    m = _AMOUNT_TAIL.search(rest)
    if m:
        amount = _eu_num(m.group(1))
        rest = rest[:m.start()].rstrip()
    client_ref = None
    toks = rest.split()
    if toks and _CLIENT_REF.match(toks[-1]):
        client_ref = toks[-1]
        rest = rest[:rest.rfind(toks[-1])].rstrip()
    detalii, cat, role, conf, direction = rest, "other", "other", 0.3, None
    counterparty = ""
    for rx, c, r, cf, dr in OPERATION_NORMALIZATION:
        m = rx.match(rest)
        if m:
            detalii = m.group(0).strip()
            counterparty = rest[m.end():].strip()
            cat, role, conf, direction = c, r, cf, dr
            break
    return detalii, counterparty, client_ref, amount, cat, role, conf, direction


def parse_statement(text):
    """
    generic_pdf_statement_parser: текст выписки → канонический dict
    {template, company_name, account_iban, currency, period_from, period_to,
     opening, closing, total_debit, total_credit, lines[], parse_stats:{
     lines_found, lines_expected, gaps, reconciliation, verdict}}.
    Если ни один extraction-профиль не детектится → {'template': None}
    (handle() тогда поле deterministic не добавляет — контракт не меняется).
    """
    text = _norm_text(text)
    profile = None
    for p in STATEMENT_PROFILES:
        if all(mk in text for mk in p["detect"]):
            profile = p
            break
    if profile is None:
        return {"template": None}

    out = _parse_statement_header(profile, text)
    out["template"] = profile["name"]

    # --- нарезка на блоки операций (layout inference) ------------------------
    blocks = []            # [(nr, booking_date, first_rest, [continuation lines])]
    cur = None
    for ln in text.splitlines():
        if profile["eof"].match(ln):
            break
        if profile["junk"].match(ln):
            continue
        # строка держателя, повторяющаяся на каждой странице
        if out["account_iban"] and out["account_iban"] in ln \
                and re.search(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}\s+[A-Z]{3}\s*$", ln):
            continue
        m = _OP_START.match(ln)
        if m:
            cur = (int(m.group(1)), m.group(2), m.group(3), [])
            blocks.append(cur)
        elif cur is not None:
            cur[3].append(ln)

    own_iban = out.get("account_iban") or ""
    lines = []
    for nr, bdate, first_rest, cont in blocks:
        detalii, counterparty, client_ref, amount, cat, role, conf, type_dir = \
            _split_op_first_line(first_rest)

        # дата валютирования + референс банка (строка 'Data valutei CUI/CNP Ref.banca')
        value_date, bank_ref, tax_id, cp_iban = None, None, None, None
        for cl in cont:
            mv = _VALUE_DATE.match(cl)
            if mv and value_date is None:
                value_date = _iso_date(mv.group(1))
                tail = mv.group(2)
                mc = _TAX_ID.search(tail)
                if mc:
                    tax_id = mc.group(1)
                    tail = tail[:mc.start()] + tail[mc.end():]
                tail = tail.replace("/", " ").strip()
                if tail and re.match(r"^\d+$", tail.split()[-1]):
                    bank_ref = tail.split()[-1]
                continue
            if tax_id is None:
                mc = _TAX_ID.search(cl)
                if mc:
                    tax_id = mc.group(1)
            if cp_iban is None:
                for mi in _IBAN_RX.finditer(cl):
                    if mi.group(0) != own_iban:
                        cp_iban = mi.group(0)
                        break

        # FX и Card — по «компактному» тексту (переносы рвут токены посреди числа)
        compact = "".join([first_rest] + cont)
        fx = None
        mf = _FX_RX.search(compact)
        if mf:
            fx = {"orig_amount": float(mf.group(1)), "orig_currency": mf.group(2),
                  "rate": float(mf.group(3))}
        card = "Card nr" in compact

        # направление: приоритет — знак суммы; листинг без знака → по типу операции
        if amount is not None and amount < 0:
            direction = "out"
        elif amount is not None and amount > 0 and type_dir is None:
            direction = "in"
        else:
            direction = type_dir or "in"
        signed = amount
        if amount is not None and direction == "out" and amount > 0:
            signed = -amount

        # IBAN казначейства → налоговый платёж (страновой маркер, не bank-specific)
        if cp_iban and any(mk in cp_iban for mk in _TREASURY_IBAN_MARKERS):
            role, conf = "tax_authority", 0.95
            if direction == "out":
                cat = "tax_payment"

        # ключи: bank/client reference НЕ уникален (ATM-снятие и его комиссия
        # делят референс) → transaction_key уникален per-строка (входит Nr),
        # operation_group_key общий для связанных операций (по референсу).
        tk_src = "%s|%s|%s|%s|%s" % (nr, bdate, signed, client_ref or "", detalii)
        transaction_key = hashlib.sha1(tk_src.encode("utf-8")).hexdigest()[:16]
        operation_group_key = client_ref or transaction_key

        desc = " ".join((" ".join([first_rest] + cont)).split())[:160]
        lines.append({
            "n": nr, "date": _iso_date(bdate), "value_date": value_date,
            "amount": signed, "direction": direction,
            "counterparty": counterparty or None,
            "counterparty_tax_id": tax_id, "counterparty_iban": cp_iban,
            "client_ref": client_ref, "bank_ref": bank_ref,
            "category": cat, "role": role, "role_conf": conf,
            "fx": fx, "card": card, "description": desc,
            "transaction_key": transaction_key,
            "operation_group_key": operation_group_key,
        })

    nrs = set(l["n"] for l in lines)
    expected = max(nrs) if nrs else 0
    gaps = sorted(set(range(1, expected + 1)) - nrs)
    dates = sorted(l["date"] for l in lines)
    out["lines"] = lines
    st = {
        "lines_found": len(lines),
        "lines_expected": expected,
        "gaps": gaps,
        "lines_period_from": dates[0] if dates else None,
        "lines_period_to": dates[-1] if dates else None,
    }

    # --- сверка и verdict (анти-фадж: только проверяем законы, НЕ выравниваем) --
    sum_cr = round(sum(l["amount"] for l in lines if l["amount"] and l["amount"] > 0), 2)
    sum_db = round(sum(-l["amount"] for l in lines if l["amount"] and l["amount"] < 0), 2)
    net = round(sum_cr - sum_db, 2)
    rec = {"sum_credits": sum_cr, "sum_debits": sum_db, "net": net,
           "summary_self_consistent": None, "lines_match_header": None,
           "full_sum_law_ok": None, "reconciliation_gap": None, "implied_opening": None}
    if out["opening"] is not None and out["closing"] is not None:
        rec["implied_opening"] = round(out["closing"] - net, 2)
        rec["full_sum_law_ok"] = abs(net - (out["closing"] - out["opening"])) <= 0.01
        rec["reconciliation_gap"] = round((out["closing"] - out["opening"]) - net, 2)
        if out["total_credit"] is not None and out["total_debit"] is not None:
            rec["summary_self_consistent"] = abs(
                out["opening"] + out["total_credit"] - out["total_debit"]
                - out["closing"]) <= 0.01
            rec["lines_match_header"] = (abs(sum_cr - out["total_credit"]) <= 0.01
                                         and abs(sum_db - out["total_debit"]) <= 0.01)
    st["reconciliation"] = rec

    extraction_ok = bool(lines) and not gaps and all(l["amount"] is not None for l in lines)
    cap = profile.get("listing_cap")
    cap_hit = bool(cap) and len(lines) >= cap
    period_clipped = bool(out["period_from"] and dates and out["period_from"] < dates[0])
    if rec["lines_match_header"] and rec["full_sum_law_ok"]:
        coverage = "FULL"
    elif rec["summary_self_consistent"] and (cap_hit or period_clipped):
        coverage = "TRUNCATED_SUSPECTED"
    else:
        coverage = "UNKNOWN"
    verdict = {
        "summarySelfConsistency": ("PASS" if rec["summary_self_consistent"]
                                   else "FAIL" if rec["summary_self_consistent"] is False
                                   else "UNKNOWN"),
        "extractionStatus": "PASS" if extraction_ok else "FAIL",
        "coverageStatus": coverage,
        "rowReconciliationStatus": "FULL" if rec["full_sum_law_ok"]
                                   and rec["lines_match_header"] else "PARTIAL",
    }
    if (verdict["summarySelfConsistency"] == "PASS" and extraction_ok
            and coverage == "FULL" and verdict["rowReconciliationStatus"] == "FULL"):
        verdict["validationGrade"] = "VERIFIED"
    elif (verdict["summarySelfConsistency"] == "PASS" and extraction_ok
            and coverage == "TRUNCATED_SUSPECTED"):
        verdict["validationGrade"] = "PARTIAL_TRUNCATED"
    else:
        verdict["validationGrade"] = "FAILED"
    st["verdict"] = verdict
    out["parse_stats"] = st
    return out


def handle(data, context=None):
    data = data or {}
    out = {"found": False, "format": None, "n_pages": 0, "n_chars": 0,
           "text": "", "pages": [], "tables": [], "date_line_count": 0,
           "sampled": False, "twin_error": None, "lib_status": {}}
    tmp_created = False
    path = None

    # --- resolveInputFile: если пришёл file_handle — скачиваем байты через PAPI (@sim-auth) ---
    # Делаем ДО импорта pdfplumber: даже если lib не встала, статус/контракт и access-verdict
    # должны быть видны в reply (для probeOnly identity-теста).
    if str(data.get("file_handle") or "").strip():
        try:
            resolve_input_file(data, out)
        except Exception as e:
            # STOP: не идём в materialize/parse — контракт+ошибка уже в data/out
            if not out.get("twin_error"):
                out["twin_error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
            data["statements"] = out
            data["text"] = ""
            data["format"] = None
            data["n_pages"] = 0
            return data

    try:
        import pdfplumber  # noqa: F401
        out["lib_status"]["pdfplumber"] = getattr(__import__("pdfplumber"), "__version__", "ok")
    except Exception as e:
        out["twin_error"] = "pdfplumber import failed: %s" % (str(e)[:200])
        data["statements"] = out
        return data
    try:
        path, name, tmp_created = _materialize(data)
        fmt = os.path.splitext(name)[1].lower().lstrip(".")
        if fmt and fmt != "pdf":
            out["twin_error"] = "unsupported format for statements: .%s (ожидается pdf)" % fmt
        else:
            res = _extract_pdf(path)
            out.update(res)
            # Детерминированный слой: если текст детектится одним из extraction-
            # профилей (generic_pdf_statement_parser) — разбираем кодом.
            # Best-effort: фейл парсера НЕ ломает базовый контракт извлечения.
            try:
                # парсим по полным страницам (text мог быть урезан size-гардом)
                det = parse_statement("\n".join(res.get("pages") or []) or out.get("text") or "")
                if det.get("template"):
                    out["deterministic"] = det        # → data.statements.deterministic
                    data["deterministic"] = det       # зеркало на верхний уровень
            except Exception as pe:
                out["deterministic_error"] = "%s: %s" % (type(pe).__name__, str(pe)[:200])
    except Exception as e:
        out["twin_error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
    finally:
        if tmp_created and path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    # зеркалим на верх для удобства следующих узлов
    data["statements"] = out
    data["text"] = out["text"]
    data["format"] = out["format"]
    data["n_pages"] = out["n_pages"]
    return data


if __name__ == "__main__":
    import sys
    d = handle({"path": sys.argv[1], "file_name": os.path.basename(sys.argv[1])}) if len(sys.argv) > 1 else {}
    s = d.get("statements", {})
    print("found=%s format=%s n_pages=%s n_chars=%s dates=%s tables=%s err=%s" % (
        s.get("found"), s.get("format"), s.get("n_pages"), s.get("n_chars"),
        s.get("date_line_count"), len(s.get("tables", [])), s.get("twin_error")))
    print((s.get("text") or "")[:500])
