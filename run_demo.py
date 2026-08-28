#!/usr/bin/env python3
"""Прогон расчёта доменов Р и У на сгенерированных синтетических данных.

Запуск:  python3 run_demo.py [--json]

Расчёт полностью детерминирован: повторный запуск даёт тот же результат.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from hmi.domains import compute_domain_r, compute_domain_u
from hmi.report import (
    components_line,
    render_table,
    score_line,
    thresholds_line,
)
from hmi.synth import DEMO_PERSON_ID, PROFILES, WEEKS, generate_dataset


def build_results() -> dict:
    """Считает оба домена для всех профилей. Чистая функция без I/O."""
    dataset = generate_dataset()
    results = {}
    for profile in PROFILES:
        timeline = dataset[profile.person_id]
        results[profile.person_id] = {
            "title": profile.title,
            "r": compute_domain_r(timeline),
            "u": compute_domain_u(timeline),
        }
    return results


def to_json(results: dict) -> str:
    """Сериализация результатов — используется тестом на детерминизм."""
    payload = {
        person_id: {
            "title": data["title"],
            "r": dataclasses.asdict(data["r"]),
            "u": dataclasses.asdict(data["u"]),
        }
        for person_id, data in results.items()
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="выдать результат в JSON")
    args = parser.parse_args(argv)

    results = build_results()

    if args.json:
        print(to_json(results))
        return 0

    print(f"Индекс зрелости здоровья — домены Р и У")
    print(f"Синтетические данные: {len(PROFILES)} человек × {WEEKS} недель × 3 практики")
    print(thresholds_line())
    print()

    rows = [
        (person_id, data["title"], data["r"], data["u"])
        for person_id, data in results.items()
    ]
    print(render_table(rows))
    print()

    demo = results[DEMO_PERSON_ID]
    r_result, u_result = demo["r"], demo["u"]

    print(f"Демо-персона {DEMO_PERSON_ID} — {demo['title']}")
    print(f"  Домен Р (регулярность): {score_line(r_result)}")
    print(components_line(r_result))
    print(f"    недельная доля выполнения плана: {r_result.diagnostics['weekly_adherence']}")
    print(f"  Домен У (устойчивость): {score_line(u_result)}")
    print(components_line(u_result))
    print(f"    базовая линия: {u_result.diagnostics['baseline']:.3f} "
          f"({u_result.diagnostics['baseline_source']}), "
          f"медиана TTR: {u_result.diagnostics['median_ttr_days']} дн.")
    for episode in u_result.diagnostics["episodes"]:
        status = "возврат завершён" if episode["recovered"] else "возврат не завершён (цензура)"
        print(
            f"    эпизод: старт день {episode['onset_day']}, дно {episode['trough_adherence']}, "
            f"глубина {episode['depth']}, TTR {episode['ttr_days']} дн. — {status}"
        )
    print()
    print(f"ИТОГ по {DEMO_PERSON_ID}:  Р = {r_result.score},  У = {u_result.score}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
