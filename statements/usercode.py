# -*- coding: utf-8 -*-
"""
statements/usercode.py — git_call handler для банковских ВЫПИСОК (port-statements).

Зачем отдельно от filedoc: filedoc даёт PDF-текст через pypdf (плоский текст-слой). Для
ВЫПИСОК точнее pdfplumber — он сохраняет колоночную структуру (Rulaj/Sold, Debit/Credit),
что критично для running-balance-цепочки в validate. Здесь git_call ТОЛЬКО извлекает
(детерминированно, без OCR, без потерь); «понимание» банка делает LLM ниже по конвейеру,
а сверку — код (validate). Анти-фадж: тут чисел не трактуем.

IN:  source_url (str, http(s) — качается) | file_b64 (str|list) | path (+ file_name с .pdf)
OUT: data.statements = {
       found, format:"pdf", n_pages, n_chars,
       text,           # чистый текст всех страниц (pdfplumber extract_text), для LLM-экстрактора
       pages:[str],    # текст по страницам (для чанкинга «40 страниц»)
       tables:[[[cell]]], # best-effort таблицы (pdfplumber), если у банка есть сетка
       date_line_count,   # ДЕТЕРМИНИРОВАННЫЙ счётчик строк-кандидатов (по датам) — анти-пропуск для stitch
       sampled, twin_error, lib_status
     }
     + зеркалит text/format/n_pages на верх.
Размер-гард: текст режется до ~1.3МБ (reply-лимит); при переполнении sampled=True.
"""
import base64
import os
import re
import tempfile

try:
    from urllib.request import urlretrieve
except Exception:  # py2 fallback (не ожидается)
    from urllib import urlretrieve

MAX_TEXT = 1_300_000            # держим reply < 1.4МБ
DATE_RX = re.compile(r"^\s*(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}[./-]\d{2}[./-]\d{2})\b")


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
    raise RuntimeError("file_b64 or source_url required")


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
    import sys, json
    d = handle({"path": sys.argv[1], "file_name": os.path.basename(sys.argv[1])}) if len(sys.argv) > 1 else {}
    s = d.get("statements", {})
    print("found=%s format=%s n_pages=%s n_chars=%s dates=%s tables=%s err=%s" % (
        s.get("found"), s.get("format"), s.get("n_pages"), s.get("n_chars"),
        s.get("date_line_count"), len(s.get("tables", [])), s.get("twin_error")))
    print((s.get("text") or "")[:500])
