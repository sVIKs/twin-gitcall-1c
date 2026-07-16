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
