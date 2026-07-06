#!/usr/bin/env python3
"""Corezoid GIT Call entrypoint (lang=python, path=filedoc) — DOC/XLSX/PDF → TEXT.

Feeds the Corezoid skill **dto-mf-file-doc**: extracts plain text from a client office
file (xlsx/xls, docx/doc, pdf, csv/txt). The Corezoid process then hands that text to the
LLM extractor (dto-mf-extract, §3) → dto-mf-writer + dto-mf-graph. This node ONLY extracts
text — deterministic, no LLM, no Simulator calls. Honest: если формат не парситься —
twin_error заполнен, text пустой, ничего не выдумываем.

Contract (git_call calls handle(data)):
  IN:  file_b64   (str)  base64 of the file content (preferred for attachments), OR
       source_url (str)  http(s) URL the runner can download
       file_name  (str)  original name WITH extension (drives format detection)
  OUT: data["filedoc"] = {
         found (bool), format (str), file_name (str), bytes (int), kb (int),
         text (str, extracted, ≤ max_chars), n_chars (int), n_rows (int|null),
         twin_error (str|null), lib_status {}
       }
  Mirrors text/file_name/kb to top-level (chunk_text picked by the process).
Окно git_call ≤30с: офисные файлы клиента малы; текст режется до max_chars (default 12000).
"""
import os, sys, json, base64, tempfile, time, traceback
from urllib.request import urlretrieve

LIB = {}
try:
    import openpyxl
    LIB["openpyxl"] = getattr(openpyxl, "__version__", "?")
except Exception as e:
    openpyxl = None; LIB["openpyxl"] = "ERR:%s" % e
try:
    import docx
    LIB["python-docx"] = "ok"
except Exception as e:
    docx = None; LIB["python-docx"] = "ERR:%s" % e
try:
    import pypdf
    LIB["pypdf"] = getattr(pypdf, "__version__", "?")
except Exception as e:
    pypdf = None; LIB["pypdf"] = "ERR:%s" % e

MAX_CHARS = 12000


def _ext(name):
    return (os.path.splitext(name or "")[1] or "").lower().lstrip(".")


def _detect(path, name):
    ext = _ext(name) or _ext(path)
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except Exception:
        head = b""
    if head[:4] == b"PK\x03\x04":
        # zip container: xlsx or docx by extension
        if ext in ("docx",):
            return "docx"
        if ext in ("xlsx", "xlsm"):
            return "xlsx"
        return ext or "zip"
    if head[:5] == b"%PDF-":
        return "pdf"
    if ext in ("xlsx", "xlsm", "xls", "docx", "doc", "pdf", "csv", "txt"):
        return ext
    return ext or "unknown"


def _materialize(data):
    """Return (local_path, source_name) from file_b64 or source_url."""
    name = data.get("file_name") or data.get("fileName") or ""
    b64 = (data.get("file_b64") or data.get("content_b64") or data.get("base64")
           or data.get("file_b64_parts"))
    if b64:
        s = b64
        # Corezoid caps a single git_call data field (~7KB): accept a list of chunks
        if isinstance(s, (list, tuple)):
            s = "".join(str(x) for x in s)
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("ascii", "ignore")
        if s.startswith("data:") and "," in s:
            s = s.split(",", 1)[1]
        # Corezoid may inject whitespace/newlines or drop padding in large fields:
        s = "".join(s.split())
        s += "=" * (-len(s) % 4)
        raw = base64.b64decode(s)
        suffix = "_" + (name or "file")
        fd, tmp = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        return tmp, (name or os.path.basename(tmp)), True
    url = (data.get("source_url") or data.get("url") or data.get("file_url") or "").strip()
    if url:
        base = name or os.path.basename(url.split("?", 1)[0]) or "file"
        fd, tmp = tempfile.mkstemp(suffix="_" + base)
        os.close(fd)
        urlretrieve(url, tmp)
        return tmp, base, True
    path = (data.get("path") or "").strip()
    if path:
        return path, (name or os.path.basename(path)), False
    raise RuntimeError("file_b64 or source_url required")


def _xlsx_text(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    lines, nrows = [], 0
    for ws in wb.worksheets:
        lines.append("# Лист: %s" % ws.title)
        for row in ws.iter_rows(values_only=True):
            cells = [("" if c is None else str(c)) for c in row]
            if any(x.strip() for x in cells):
                lines.append("\t".join(cells).rstrip())
                nrows += 1
        if sum(len(x) for x in lines) > MAX_CHARS * 2:
            break
    wb.close()
    return "\n".join(lines), nrows


def _docx_text(path):
    d = docx.Document(path)
    lines = [p.text for p in d.paragraphs if p.text and p.text.strip()]
    nrows = 0
    for tbl in d.tables:
        for r in tbl.rows:
            cells = [c.text.strip() for c in r.cells]
            if any(cells):
                lines.append("\t".join(cells))
                nrows += 1
    return "\n".join(lines), (nrows or None)


def _pdf_text(path):
    r = pypdf.PdfReader(path)
    out = []
    for pg in r.pages:
        try:
            t = pg.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            out.append(t)
        if sum(len(x) for x in out) > MAX_CHARS * 2:
            break
    return "\n".join(out), None


def handle(data, context=None):
    out = {"found": False, "format": None, "file_name": None, "bytes": 0, "kb": 0,
           "text": "", "n_chars": 0, "n_rows": None, "twin_error": None, "lib_status": LIB}
    t0 = time.time()
    tmp_created = False
    try:
        path, name, tmp_created = _materialize(data)
        out["file_name"] = name
        try:
            out["bytes"] = os.path.getsize(path)
            out["kb"] = int(round(out["bytes"] / 1024.0))
        except Exception:
            pass
        fmt = _detect(path, name)
        out["format"] = fmt
        text, nrows = "", None
        if fmt in ("xlsx", "xlsm"):
            if openpyxl is None:
                raise RuntimeError("openpyxl unavailable")
            text, nrows = _xlsx_text(path)
        elif fmt == "docx":
            if docx is None:
                raise RuntimeError("python-docx unavailable")
            text, nrows = _docx_text(path)
        elif fmt == "pdf":
            if pypdf is None:
                raise RuntimeError("pypdf unavailable")
            text, nrows = _pdf_text(path)
        elif fmt in ("csv", "txt"):
            with open(path, "rb") as f:
                text = f.read().decode("utf-8", "ignore")
        else:
            out["twin_error"] = ("формат '%s' не підтримується у цій хвилі "
                                 "(xlsx/docx/pdf/csv/txt); текст не витягнуто" % fmt)
        text = (text or "").strip()
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]
        out["text"] = text
        out["n_chars"] = len(text)
        out["n_rows"] = nrows
        out["found"] = bool(text)
        if not text and not out["twin_error"]:
            out["twin_error"] = "текст не витягнуто (порожній або нечитабельний файл)"
    except Exception as e:
        out["twin_error"] = "%s: %s" % (type(e).__name__, e)
        out["trace"] = traceback.format_exc()[-800:]
    finally:
        if tmp_created:
            try:
                os.unlink(path)  # noqa
            except Exception:
                pass
    out["ms"] = int((time.time() - t0) * 1000)
    # keep the git_call reply small: drop the echoed base64 payload
    for k in ("file_b64", "content_b64", "base64"):
        if k in data:
            try:
                del data[k]
            except Exception:
                data[k] = ""
    data["filedoc"] = out
    data["doc_text"] = out["text"]
    data["file_name_out"] = out["file_name"]
    data["found"] = out["found"]
    data["twin_error"] = out["twin_error"]
    # top-level diagnostics (visible without digging into filedoc.*)
    data["fd_format"] = out["format"]
    data["fd_kb"] = out["kb"]
    data["fd_nchars"] = out["n_chars"]
    data["fd_lib"] = out["lib_status"]
    return data


def usercode(data, context=None):
    return handle(data, context)


if __name__ == "__main__":
    import glob
    args = sys.argv[1:]
    if not args:
        args = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                      "sprint-mf-2026-07-06", "fixtures", "docs", "*")))
    for a in args:
        with open(a, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        r = handle({"file_b64": b64, "file_name": os.path.basename(a)})
        d = r["filedoc"]
        print("== %s | fmt=%s kb=%s chars=%s rows=%s err=%s" % (
            d["file_name"], d["format"], d["kb"], d["n_chars"], d["n_rows"], d["twin_error"]))
        print(d["text"][:400])
        print("----")
