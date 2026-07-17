# -*- coding: utf-8 -*-
"""
Локальные тесты generic_pdf_statement_parser (parse_statement + handle).

Данных с PII В РЕПО НЕТ: пути к реальным фикстурам передаются снаружи (env).
  BRD_TXT    — реальный текст выписки BRD (500 операций; PII → НЕ в репо)
  BRD_PDF    — реальный PDF (integrity-check: sha256 эталона ниже)
  SYNT_BIG   — синтетический PDF ~500 операций (fixtures-mig/SYNT_big.pdf)
  SYNT_SMALL — синтетический маленький PDF (fixtures-mig/SYNT_small.pdf)

Запуск:  BRD_TXT=/path/BRD_v2.txt BRD_PDF=/path/BRD_v2.pdf python3 test_brd_local.py

Законы (анти-фадж — никаких balancing-транзакций):
  summary self-consistency: opening + total_credit - total_debit == closing
  full sum law:             net(строк) == closing - opening (полная выгрузка)
Реальный BRD-экспорт режет листинг на 500 строк → верный verdict:
  summarySelfConsistency=PASS, extractionStatus=PASS,
  coverageStatus=TRUNCATED_SUSPECTED, rowReconciliationStatus=PARTIAL,
  validationGrade=PARTIAL_TRUNCATED (НЕ VERIFIED). Ожидаемый gap = 2893.24.
"""
import hashlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usercode  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BRD_TXT = os.environ.get("BRD_TXT", "")
BRD_PDF = os.environ.get("BRD_PDF", "")
SYNT_BIG = os.environ.get("SYNT_BIG", os.path.join(HERE, "..", "fixtures-mig", "SYNT_big.pdf"))
SYNT_SMALL = os.environ.get("SYNT_SMALL", os.path.join(HERE, "..", "fixtures-mig", "SYNT_small.pdf"))

# Эталон целостности реального PDF (384958 байт, 40 страниц); копия 208896 байт = битая.
BRD_PDF_SHA256 = "937da5bdfa50315f027b3db428eaa04cc56c09a7447f56150b65a796b0f0ec2e"

FAILED = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print("  [%s] %s %s" % (status, name, detail))
    if not cond:
        FAILED.append(name)


def sums(det):
    cr = sum(l["amount"] for l in det["lines"] if l["amount"] and l["amount"] > 0)
    db = sum(-l["amount"] for l in det["lines"] if l["amount"] and l["amount"] < 0)
    return round(cr, 2), round(db, 2), round(cr - db, 2)


def test_a_real_brd():
    print("A) parse_statement на реальном тексте BRD (профиль brd-extras, 500 операций)")
    if not BRD_TXT or not os.path.exists(BRD_TXT):
        print("  [SKIP] BRD_TXT не задан/не найден")
        return
    if BRD_PDF and os.path.exists(BRD_PDF):
        dig = hashlib.sha256(open(BRD_PDF, "rb").read()).hexdigest()
        check("integrity PDF-эталона (sha256)", dig == BRD_PDF_SHA256, dig[:16])
    with io.open(BRD_TXT, encoding="utf-8") as f:
        det = usercode.parse_statement(f.read())
    st = det.get("parse_stats", {})
    rec = st.get("reconciliation", {})
    ver = st.get("verdict", {})
    check("template==brd-extras", det.get("template") == "brd-extras", det.get("template"))
    check("lines_found==500", st.get("lines_found") == 500, st.get("lines_found"))
    check("gaps==[]", st.get("gaps") == [], st.get("gaps"))
    check("opening==4137.10", abs((det.get("opening") or 0) - 4137.10) < 0.005, det.get("opening"))
    check("closing==17504.96", abs((det.get("closing") or 0) - 17504.96) < 0.005, det.get("closing"))
    check("total_debit==1991732.26 (declared)", abs((det.get("total_debit") or 0) - 1991732.26) < 0.005,
          det.get("total_debit"))
    check("total_credit==2005100.12 (declared)", abs((det.get("total_credit") or 0) - 2005100.12) < 0.005,
          det.get("total_credit"))
    cr, db, net = sums(det)
    print("     visible: credits=%.2f debits=%.2f net=%.2f | declared период %s..%s, строки %s..%s"
          % (cr, db, net, det.get("period_from"), det.get("period_to"),
             st.get("lines_period_from"), st.get("lines_period_to")))
    # Шапка self-consistent (4137.10+2005100.12-1991732.26=17504.96), но видимые
    # 500 строк покрывают только хвост периода (кап экспорта) → PARTIAL_TRUNCATED.
    check("visible credits==1431713.52", abs(cr - 1431713.52) < 0.01, cr)
    check("visible debits==1421238.90", abs(db - 1421238.90) < 0.01, db)
    check("reconciliation_gap==2893.24 (ОЖИДАЕМЫЙ, усечение)",
          abs((rec.get("reconciliation_gap") or 0) - 2893.24) < 0.01,
          rec.get("reconciliation_gap"))
    check("summarySelfConsistency==PASS", ver.get("summarySelfConsistency") == "PASS")
    check("extractionStatus==PASS", ver.get("extractionStatus") == "PASS")
    check("coverageStatus==TRUNCATED_SUSPECTED", ver.get("coverageStatus") == "TRUNCATED_SUSPECTED",
          ver.get("coverageStatus"))
    check("rowReconciliationStatus==PARTIAL", ver.get("rowReconciliationStatus") == "PARTIAL")
    check("validationGrade==PARTIAL_TRUNCATED (не VERIFIED)",
          ver.get("validationGrade") == "PARTIAL_TRUNCATED", ver.get("validationGrade"))
    # Ключи: ATM-снятие и его комиссия делят client_ref → одинаковый
    # operation_group_key, но РАЗНЫЕ transaction_key.
    by_ref = {}
    for l in det["lines"]:
        if l["client_ref"]:
            by_ref.setdefault(l["client_ref"], []).append(l)
    shared = [v for v in by_ref.values() if len(v) > 1]
    tks = [l["transaction_key"] for l in det["lines"]]
    check("есть группы с общим референсом (ATM+fee)", len(shared) > 0, "групп=%d" % len(shared))
    if shared:
        g = shared[0]
        check("общий operation_group_key в группе",
              len(set(l["operation_group_key"] for l in g)) == 1)
    check("transaction_key уникален по всем 500", len(set(tks)) == len(tks),
          "%d/%d" % (len(set(tks)), len(tks)))


def test_b_synt_big():
    print("B) handle() на SYNT_big.pdf (полный период → VERIFIED)")
    if not os.path.exists(SYNT_BIG):
        print("  [SKIP] SYNT_BIG не найден")
        return
    d = usercode.handle({"path": SYNT_BIG, "file_name": os.path.basename(SYNT_BIG)})
    det = (d.get("statements") or {}).get("deterministic")
    check("statements.deterministic присутствует", isinstance(det, dict))
    check("зеркало data.deterministic", d.get("deterministic") is det)
    if not det:
        return
    st = det.get("parse_stats", {})
    ver = st.get("verdict", {})
    check("template==brd-extras", det.get("template") == "brd-extras", det.get("template"))
    check("lines_found==lines_expected", st.get("lines_found") == st.get("lines_expected"),
          "%s/%s" % (st.get("lines_found"), st.get("lines_expected")))
    check("gaps==[]", st.get("gaps") == [], st.get("gaps"))
    cr, db, net = sums(det)
    print("     lines=%s credits=%.2f debits=%.2f net=%.2f closing-opening=%.2f" % (
        st.get("lines_found"), cr, db, net, det["closing"] - det["opening"]))
    check("full sum law", abs(net - (det["closing"] - det["opening"])) < 0.01)
    check("sum(debits)==total_debit", abs(db - det["total_debit"]) < 0.01, db)
    check("sum(credits)==total_credit", abs(cr - det["total_credit"]) < 0.01, cr)
    check("coverageStatus==FULL", ver.get("coverageStatus") == "FULL", ver.get("coverageStatus"))
    check("validationGrade==VERIFIED", ver.get("validationGrade") == "VERIFIED",
          ver.get("validationGrade"))


def test_c_contract_small():
    print("C) обратная совместимость: handle() на SYNT_small.pdf — прежние ключи")
    if not os.path.exists(SYNT_SMALL):
        print("  [SKIP] SYNT_SMALL не найден")
        return
    d = usercode.handle({"path": SYNT_SMALL, "file_name": os.path.basename(SYNT_SMALL)})
    s = d.get("statements") or {}
    legacy = ["found", "format", "n_pages", "n_chars", "text", "pages", "tables",
              "date_line_count", "sampled", "twin_error", "lib_status"]
    check("все legacy-ключи statements", all(k in s for k in legacy),
          [k for k in legacy if k not in s])
    check("found=True/format=pdf", s.get("found") is True and s.get("format") == "pdf",
          "%s/%s" % (s.get("found"), s.get("format")))
    check("text непустой и зеркален", bool(s.get("text")) and d.get("text") == s.get("text"))
    check("n_pages зеркален", d.get("n_pages") == s.get("n_pages"), s.get("n_pages"))
    check("twin_error is None", s.get("twin_error") is None, s.get("twin_error"))


# Синтетический Intesa-подобный мини-пример (it-профиль): полный период,
# суммы сходятся → там допустим VERIFIED. Доказывает генеричность движка
# (второй extraction-профиль + итальянская нормализация), НЕ реальные данные.
INTESA_MINI = """Estratto conto
Dal 01.04.2026
Al 30.04.2026
Intestatario conto Numero conto Valuta
DEMO COMMERCE SRL IT60X0542811101000000123456 EUR
Saldo iniziale 1.000,00 Totale uscite 500,00
Saldo finale 1.700,00 Totale entrate 1.200,00
Nr. Data operazione Dettagli Beneficiario Riferimento Importo
1. 05.04.2026 Bonifico ricevuto ACME SPA OP123456 1.200,00
2. 10.04.2026 Pagamento fornitore BETA SRL OP123457 -450,00
3. 15.04.2026 Commissione bonifico OP123457 -50,00
Fine lista
"""


def test_d_intesa_mini():
    print("D) parse_statement на Intesa-подобной мини-выписке (полный период → VERIFIED)")
    det = usercode.parse_statement(INTESA_MINI)
    st = det.get("parse_stats", {})
    ver = st.get("verdict", {})
    check("template==intesa-mini", det.get("template") == "intesa-mini", det.get("template"))
    check("3 строки, gaps==[]", st.get("lines_found") == 3 and st.get("gaps") == [],
          "%s/%s" % (st.get("lines_found"), st.get("gaps")))
    check("шапка: 1000/1700, uscite 500, entrate 1200",
          det.get("opening") == 1000.0 and det.get("closing") == 1700.0
          and det.get("total_debit") == 500.0 and det.get("total_credit") == 1200.0)
    cr, db, net = sums(det)
    check("full sum law (1200-500==700)", abs(net - 700.0) < 0.01, net)
    check("validationGrade==VERIFIED", ver.get("validationGrade") == "VERIFIED",
          ver.get("validationGrade"))
    cats = [l["category"] for l in det["lines"]]
    check("категории it-нормализации", cats == ["incoming_payment", "outgoing_payment", "fee"], cats)
    # общий референс OP123457 у платежа и его комиссии → одна группа, разные ключи
    l2, l3 = det["lines"][1], det["lines"][2]
    check("группа платёж+комиссия: общий group, разные tx",
          l2["operation_group_key"] == l3["operation_group_key"]
          and l2["transaction_key"] != l3["transaction_key"])


def test_e_negative():
    print("E) negative: не-выписка → deterministic отсутствует, контракт не тронут")
    det = usercode.parse_statement("Просто произвольный текст.\nНичего банковского.")
    check("template is None", det.get("template") is None, det.get("template"))


if __name__ == "__main__":
    test_a_real_brd()
    test_b_synt_big()
    test_c_contract_small()
    test_d_intesa_mini()
    test_e_negative()
    print("\n%s: %s" % ("FAIL" if FAILED else "OK", FAILED or "все проверки зелёные"))
    sys.exit(1 if FAILED else 0)
