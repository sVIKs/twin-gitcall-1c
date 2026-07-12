#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Локальний harness для filedoc mode=load (детермінований парсер по MappingContract).

Прогоняє повний cursor-цикл на згенерованих фікстурах і доказує числами:
  (a) Σsums == сума колонки у файлі (паритет, допуск 0 через Decimal)
  (b) count entities == рядків у файлі
  (c) reply chunk < 1.4 МБ
  (d) повторний прогін ідемпотентний (ті самі refs)
  (e) кросс-FK links присутні

Запуск:  python3 test_mode_load.py
"""
import os, sys, json
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "filedoc"))
import usercode as UC  # noqa

FIX = os.path.join(HERE, "..", "1c-demo", "fixtures-mig")
MAX_CHUNK_BYTES = int(1.4 * 1024 * 1024)  # 1.4 МБ

# ------------------------------------------------------------------ mapping contracts (рукописні)
CONTRACT_SOTRUDNIKI = {
    "locale": {"decimal": "."},
    "tables": [{
        "sheet": "Сотрудники", "header_row": 1,
        "rowEntity": {"class": "Employee", "ref_template": "emp-{key}", "title_col": "ФИО"},
        "columns": [
            {"col": "КодСотрудника", "role": "key"},
            {"col": "ФИО", "role": "attr"},
            {"col": "Должность", "role": "attr"},
            {"col": "Оклад", "role": "account", "account_name": "Оклад",
             "unit": "грн", "kind": "fact", "currency": "UAH"},
            {"col": "Отдел", "role": "attr"},
        ],
    }],
}

CONTRACT_NOMENKLATURA = {
    "locale": {"decimal": "."},
    "tables": [{
        "sheet": "Номенклатура", "header_row": 1,
        "rowEntity": {"class": "Product", "ref_template": "prod-{key}", "title_col": "Наименование"},
        "columns": [
            {"col": "КодТовара", "role": "key"},
            {"col": "Наименование", "role": "attr"},
            {"col": "Категория", "role": "attr"},
            {"col": "Цена", "role": "account", "account_name": "Ціна",
             "unit": "грн", "kind": "fact", "currency": "UAH"},
            {"col": "Остаток", "role": "account", "account_name": "Залишок",
             "unit": "шт", "kind": "fact", "currency": "one"},
            {"col": "Ед", "role": "attr"},
        ],
    }],
}

CONTRACT_DOGOVORY = {
    "locale": {"decimal": "."},   # у docx суми як "125000.00"
    "tables": [{
        "tableIdx": 0, "header_row": 1,
        "rowEntity": {"class": "Contract", "ref_template": "dog-{key}", "title_col": "НомерДоговора"},
        "columns": [
            {"col": "НомерДоговора", "role": "key"},
            {"col": "КодСотрудника", "role": "link",
             "to_table": "sotrudniki", "to_key": "КодСотрудника", "edge_type": "responsible"},
            {"col": "Контрагент", "role": "attr"},
            {"col": "Сумма", "role": "account", "account_name": "Сума договору",
             "unit": "грн", "kind": "fact", "currency": "UAH"},
            {"col": "Дата", "role": "attr", "date": True},
        ],
    }],
}


def run_full(file_path, file_name, contract, chunk_rows):
    """Повний cursor-цикл mode=load → зібрані entities + агреговані sums + max chunk bytes."""
    all_entities = []
    agg_sums = {}
    max_bytes = 0
    steps = 0
    cursor = None
    src_counts = {}
    unmatched_total = 0
    while True:
        data = {"mode": "load", "path": file_path, "file_name": file_name,
                "mapping_contract": contract, "chunk_rows": chunk_rows}
        if cursor is not None:
            data["cursor"] = cursor
        out = UC.handle(data)
        if out.get("twin_error"):
            print("  ERROR:", out["twin_error"])
            if out.get("twin_trace"):
                print(out["twin_trace"])
            break
        steps += 1
        chunk_bytes = len(json.dumps(out["entities"], ensure_ascii=False).encode("utf-8"))
        max_bytes = max(max_bytes, chunk_bytes)
        all_entities.extend(out["entities"])
        for k, v in out["sums"].items():
            agg_sums[k] = agg_sums.get(k, Decimal("0")) + Decimal(v)
        for k, v in out.get("source_counts", {}).items():
            src_counts[k] = src_counts.get(k, 0) + v
        unmatched_total += out.get("unmatched", 0)
        cursor = out["cursor"]
        if out["done"]:
            break
        if steps > 100000:
            print("  loop guard"); break
    return {"entities": all_entities, "sums": agg_sums, "max_bytes": max_bytes,
            "steps": steps, "src_counts": src_counts, "unmatched": unmatched_total}


def refs_of(entities):
    return [e.get("ref") for e in entities]


def main():
    print("=" * 78)
    print("HARNESS mode=load — фікстури:", FIX)
    print("=" * 78)

    cases = [
        ("sotrudniki.xlsx",   CONTRACT_SOTRUDNIKI,
         {"Оклад": Decimal("649000")}, 15),
        ("nomenklatura.xlsx", CONTRACT_NOMENKLATURA,
         {"Ціна": Decimal("63821.40"), "Залишок": Decimal("2225")}, 20),
        ("dogovory.docx",     CONTRACT_DOGOVORY,
         {"Сума договору": Decimal("973000")}, 8),
    ]

    overall_ok = True
    global_max_bytes = 0

    for fname, contract, expected_sums, expected_rows in cases:
        fpath = os.path.join(FIX, fname)
        print("\n--- %s ---" % fname)
        # маленький chunk_rows=3 → примусово багато кроків (перевірка степпера/резюме)
        r1 = run_full(fpath, fname, contract, chunk_rows=3)
        r2 = run_full(fpath, fname, contract, chunk_rows=3)  # повтор → ідемпотентність

        n = len(r1["entities"])
        global_max_bytes = max(global_max_bytes, r1["max_bytes"])

        # (b) count entities == рядків
        ok_count = (n == expected_rows)
        print("  (b) entities=%d, очікувано=%d  -> %s (steps=%d)"
              % (n, expected_rows, "OK" if ok_count else "FAIL", r1["steps"]))

        # (a) паритет сум
        ok_sums = True
        for acc, exp in expected_sums.items():
            got = r1["sums"].get(acc, Decimal("0"))
            match = (got == exp)
            ok_sums = ok_sums and match
            print("  (a) Σ[%s]=%s, очікувано=%s  -> %s"
                  % (acc, got, exp, "OK" if match else "FAIL"))

        # (c) chunk < 1.4 МБ
        ok_bytes = r1["max_bytes"] < MAX_CHUNK_BYTES
        print("  (c) max_chunk_bytes=%d (<%d)  -> %s"
              % (r1["max_bytes"], MAX_CHUNK_BYTES, "OK" if ok_bytes else "FAIL"))

        # (d) ідемпотентність refs
        refs1, refs2 = refs_of(r1["entities"]), refs_of(r2["entities"])
        ok_idem = (refs1 == refs2) and (len(set(refs1)) == len(refs1))
        print("  (d) refs ідемпотентні=%s, унікальні=%s  -> %s"
              % (refs1 == refs2, len(set(refs1)) == len(refs1), "OK" if ok_idem else "FAIL"))

        # (e) кросс-FK links (тільки dogovory)
        links = [l for e in r1["entities"] for l in e.get("links", [])]
        if "docx" in fname:
            ok_fk = len(links) == expected_rows and all(
                l["toFileTable"] == "sotrudniki" for l in links)
            print("  (e) FK links=%d → sotrudniki  -> %s (sample=%s)"
                  % (len(links), "OK" if ok_fk else "FAIL",
                     json.dumps(links[0], ensure_ascii=False) if links else "нема"))
        else:
            ok_fk = True
            print("  (e) FK: н/д для цього файлу (links=%d)" % len(links))

        # unmatched
        print("  unmatched(рядків без ключа)=%d" % r1["unmatched"])
        # sample entity
        if r1["entities"]:
            print("  sample entity:", json.dumps(r1["entities"][0], ensure_ascii=False)[:280])

        case_ok = ok_count and ok_sums and ok_bytes and ok_idem and ok_fk
        overall_ok = overall_ok and case_ok
        print("  => %s" % ("PASS" if case_ok else "FAIL"))

    print("\n" + "=" * 78)
    print("GLOBAL max_chunk_bytes = %d (<%d МБ-межа)  |  ПІДСУМОК: %s"
          % (global_max_bytes, MAX_CHUNK_BYTES, "ALL PASS" if overall_ok else "FAIL"))
    print("=" * 78)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
