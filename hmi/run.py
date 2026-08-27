"""Прогон расчёта доменов Р и У на синтетических данных.

Запуск:  python -m hmi.run
"""

from __future__ import annotations

from collections import defaultdict

from hmi.domains import (
    RegularityResult,
    ResilienceResult,
    domain_regularity,
    domain_resilience,
    rows_to_days,
)
from hmi.generate import DATA_PATH, PROFILES, read_csv, write_csv

HEADLINE_PROFILE = "typical"

SCORE_MEANING = {
    5: "ритм держится сам",
    4: "стабильно, редкие сбои",
    3: "держится под контролем",
    2: "рвано",
    1: "ритма нет",
}


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _report_regularity(result: RegularityResult) -> list[str]:
    if result.score is None:
        return [f"  Р = — ({result.note})"]
    return [
        f"  Р = {result.score}  (P_raw = {_fmt(result.raw)}) — {SCORE_MEANING[result.score]}",
        f"      A соблюдение = {_fmt(result.adherence)}"
        f" | S стабильность = {_fmt(result.stability)}"
        f" | R провалы = {_fmt(result.rhythm)}",
        f"      недель в окне: {result.weeks}, самый длинный провал: {result.longest_gap} дн.",
    ]


def _report_resilience(result: ResilienceResult) -> list[str]:
    lines: list[str] = []
    if result.score is None:
        lines.append(f"  У = — ({result.note})")
    else:
        lines.append(
            f"  У = {result.score}  (U_raw = {_fmt(result.raw)})"
            f" — медианный TTR {_fmt(result.median_ttr, 1)} дн."
        )
        lines.append(
            f"      база окна = {_fmt(result.baseline)}"
            f" | скорость возврата = {_fmt(result.speed)}"
            f" | полнота возврата = {_fmt(result.completeness)}"
        )
    for episode in result.episodes:
        if not episode.evaluated:
            tail = f"не оценён ({episode.note})"
        else:
            tail = (
                f"TTR = {episode.ttr_days} дн."
                f", полнота {_fmt(episode.completeness)}"
                + (f" [{episode.note}]" if episode.note else "")
            )
        lines.append(
            f"      · {episode.kind} {episode.start_date}..{episode.end_date}"
            f" ({episode.length_days} дн.), база {_fmt(episode.baseline)} → {tail}"
        )
    return lines


def main() -> None:
    if not DATA_PATH.exists():
        write_csv()

    by_profile: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in read_csv():
        by_profile[str(row["profile"])].append(row)

    titles = {spec.name: spec.title for spec in PROFILES}
    headline: tuple[RegularityResult, ResilienceResult] | None = None

    print(f"Индекс зрелости здоровья — домены Р и У\nисточник: {DATA_PATH}\n")

    for spec in PROFILES:
        days = rows_to_days(by_profile[spec.name])
        regularity = domain_regularity(days)
        resilience = domain_resilience(days)

        print(f"{titles[spec.name]} ({spec.name}), дней: {len(days)}")
        for line in _report_regularity(regularity):
            print(line)
        for line in _report_resilience(resilience):
            print(line)
        print()

        if spec.name == HEADLINE_PROFILE:
            headline = (regularity, resilience)

    if headline is not None:
        regularity, resilience = headline
        print(f"Итог по профилю «{titles[HEADLINE_PROFILE]}»:")
        print(f"  Р = {regularity.score}")
        print(f"  У = {resilience.score}")


if __name__ == "__main__":
    main()
