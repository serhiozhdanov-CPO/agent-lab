#!/usr/bin/env python3
"""Проверка потока наблюдений на соответствие каноническому формату 1.0.

    python tools/validate.py data/synth-0001/observations.jsonl

Проверяет ровно то, что перечислено в конце canonical-format.md, и, кроме
схемы каждой записи, — отсутствие дублей по ключу
(subject_id, metric, effective_date, source.vendor).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from synth.canonical import validate_record  # noqa: E402


def validate_file(path: str) -> list[str]:
    problems: list[str] = []
    seen: dict[tuple, int] = {}

    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(f"строка {line_no}: не разбирается как JSON ({exc})")
                continue

            problems.extend(validate_record(rec, line_no))

            try:
                key = (rec["subject_id"], rec["metric"], rec["effective_date"],
                       rec["source"]["vendor"])
            except (KeyError, TypeError):
                continue
            if key in seen:
                problems.append(
                    f"строка {line_no}: дубль ключа {key}, первый раз в строке {seen[key]}")
            else:
                seen[key] = line_no

    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("использование: python tools/validate.py <observations.jsonl>", file=sys.stderr)
        return 2

    path = argv[1]
    problems = validate_file(path)
    total = sum(1 for _ in open(path, encoding="utf-8"))

    if problems:
        print(f"НЕВАЛИДНО: {len(problems)} нарушений в {total} записях\n")
        for p in problems[:50]:
            print("  •", p)
        if len(problems) > 50:
            print(f"  … и ещё {len(problems) - 50}")
        return 1

    print(f"ВАЛИДНО: {total} записей соответствуют схеме 1.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
