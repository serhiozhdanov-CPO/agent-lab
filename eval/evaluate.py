#!/usr/bin/env python3
"""Штатный оценщик: сверить отчёт агента с эталоном и посчитать баллы.

Единственное место, где эталон и отчёт встречаются. Агент к эталону не ходит,
оценщик не пишет ничего в рабочий каталог агента — балл кладётся в корень
прогона, чтобы агента нельзя было прогнать по кругу, подгоняя ответ под
собственный результат.

Баллы считаются только этим скриптом. Пересчёт агрегата вручную по выводу
модели не является результатом прогона: у оценщика есть допуски, сопоставление
по IoU и приведение единиц, и повторить их на глаз нельзя.

    python3 eval/evaluate.py --run-dir eval/runs/health-synth-s42
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (REPORT_SCHEMA_VERSION, TRUTH_SCHEMA_VERSION,   # noqa: E402
                     iou, overlap_days, parse_date)

# Минимальное перекрытие с эталонным окном, при котором событие засчитывается.
MIN_EVENT_IOU = 1.0 / 3.0

# Сколько суток пересечения с контрольным окном считается ложным срабатыванием.
# Один день на границе — это неточность разметки границ, а не выдумка.
CONTROL_ALARM_MIN_DAYS = 2


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------

def _window(obj: dict):
    return parse_date(obj["from"]), parse_date(obj["to"])


def _as_hrv_pct(value: float, unit: str, hrv_baseline: float):
    """Привести величину связи к процентам от базовой HRV на час позднего отбоя.

    Единица выбирается аналитикой свободно — важно, что она указана честно.
    Между процентами и миллисекундами оценщик переводит сам, через базовую
    линию из эталона.
    """
    if unit == "pct_per_hour_late":
        return value
    if unit == "ms_per_hour_late" and hrv_baseline:
        return 100.0 * value / hrv_baseline
    return None


def _pairs_match(a1, a2, b1, b2) -> bool:
    """Пара источников совпала, порядок не важен."""
    return {a1, a2} == {b1, b2}


# --------------------------------------------------------------------------
# Задачи
# --------------------------------------------------------------------------

def score_events(truth: dict, report: dict) -> dict:
    required = [e for e in truth["events"] if not e.get("optional")]
    optional = [e for e in truth["events"] if e.get("optional")]
    reported = list(report.get("events") or [])
    if not required:
        return None

    matched, used = [], set()
    for expected in required:
        e_from, e_to = _window(expected)
        best, best_iou = None, 0.0
        for index, actual in enumerate(reported):
            if index in used or actual.get("kind") != expected["kind"]:
                continue
            try:
                a_from, a_to = _window(actual)
            except (KeyError, ValueError):
                continue
            value = iou(e_from, e_to, a_from, a_to)
            if value > best_iou:
                best, best_iou = index, value
        if best is not None and best_iou >= MIN_EVENT_IOU:
            used.add(best)
            matched.append({"truth_id": expected["id"], "iou": round(best_iou, 3)})

    # Заявки, попавшие в необязательный фон, не считаем ни находкой, ни ошибкой.
    scored_reports = 0
    for index, actual in enumerate(reported):
        if index in used:
            scored_reports += 1
            continue
        if actual.get("kind") == "other":
            continue
        try:
            a_from, a_to = _window(actual)
        except (KeyError, ValueError):
            continue
        if any(iou(*_window(bg), a_from, a_to) >= MIN_EVENT_IOU for bg in optional):
            continue
        scored_reports += 1

    recall = len(matched) / len(required)
    precision = (len(matched) / scored_reports) if scored_reports else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return {
        "task": "events", "score": round(f1, 3),
        "detail": {
            "expected": len(required), "matched": len(matched),
            "reported_scored": scored_reports,
            "recall": round(recall, 3), "precision": round(precision, 3),
            "matches": matched,
            "missed": [e["id"] for e in required
                       if e["id"] not in {m["truth_id"] for m in matched}],
        },
    }


def score_control(truth: dict, report: dict) -> dict:
    """Ложные срабатывания в заведомо спокойных окнах.

    Задача измеряет сдержанность при наличии выводов. Отчёт, не заявивший ни
    одного события, сдержанность не демонстрирует — он просто молчит, и
    засчитывать ему полный балл за «ничего не выдумал» значит поощрять молчание.
    Такой отчёт остаётся без этой задачи, и его итог считается по остальным.
    """
    controls = truth.get("control_windows") or []
    events = report.get("events") or []
    if not controls or not events:
        return None

    alarms = []
    for actual in events:
        try:
            a_from, a_to = _window(actual)
        except (KeyError, ValueError):
            continue
        for window in controls:
            c_from, c_to = _window(window)
            days = overlap_days(a_from, a_to, c_from, c_to)
            if days >= CONTROL_ALARM_MIN_DAYS:
                alarms.append({
                    "control": window["id"],
                    "reported": actual.get("id") or actual.get("kind"),
                    "overlap_days": days,
                })
                break

    return {
        "task": "control_windows", "score": round(1.0 / (1 + len(alarms)), 3),
        "detail": {"control_windows": len(controls),
                   "false_alarms": len(alarms), "alarms": alarms},
    }


def score_couplings(truth: dict, report: dict) -> dict:
    expected_list = truth.get("couplings") or []
    if not expected_list:
        return None
    reported = list(report.get("couplings") or [])
    hrv_baseline = (truth.get("baselines") or {}).get("hrv_rmssd")

    hits, notes = 0, []
    for expected in expected_list:
        candidates = [c for c in reported
                      if c.get("cause") == expected["cause"]
                      and c.get("effect") == expected["effect"]]

        if expected.get("must_be_absent"):
            if candidates:
                notes.append({"id": expected["id"], "verdict": "выдуманная связь"})
            else:
                hits += 1
                notes.append({"id": expected["id"], "verdict": "верно не заявлена"})
            continue

        best = None
        for candidate in candidates:
            if candidate.get("lag_days") != expected["lag_days"]:
                continue
            value = _as_hrv_pct(candidate.get("effect_size"),
                                candidate.get("unit", ""), hrv_baseline)
            target = _as_hrv_pct(expected["effect_size"], expected["unit"], hrv_baseline)
            if value is None or target is None:
                continue
            if abs(value - target) <= expected["tolerance"]:
                best = round(value, 2)
                break
        if best is not None:
            hits += 1
            notes.append({"id": expected["id"], "verdict": "найдена",
                          "reported_pct": best})
        else:
            notes.append({"id": expected["id"], "verdict": "не найдена или вне допуска"})

    return {
        "task": "couplings", "score": round(hits / len(expected_list), 3),
        "detail": {"expected": len(expected_list), "hits": hits, "notes": notes},
    }


def score_trends(truth: dict, report: dict) -> dict:
    expected_list = truth.get("trends") or []
    if not expected_list:
        return None
    reported = list(report.get("trends") or [])

    hits, notes = 0, []
    for expected in expected_list:
        candidates = [t for t in reported if t.get("metric") == expected["metric"]]
        if expected.get("source"):
            narrowed = [t for t in candidates if t.get("source") == expected["source"]]
            candidates = narrowed or candidates
        if not candidates:
            notes.append({"id": expected["id"], "verdict": "тренд не заявлен"})
            continue
        actual = candidates[0]
        if actual.get("verdict") == expected["verdict"]:
            hits += 1
            notes.append({"id": expected["id"], "verdict": "природа определена верно"})
        else:
            notes.append({
                "id": expected["id"],
                "verdict": f"природа определена неверно: "
                           f"{actual.get('verdict')} вместо {expected['verdict']}",
            })

    return {
        "task": "trends", "score": round(hits / len(expected_list), 3),
        "detail": {"expected": len(expected_list), "hits": hits, "notes": notes},
    }


def score_missingness(truth: dict, report: dict) -> dict:
    expected = truth.get("missingness")
    if not expected:
        return None
    actual = report.get("missingness") or {}

    mechanism_ok = actual.get("mechanism") == expected["mechanism"]
    declared_imputation = isinstance(actual.get("imputed"), bool)

    gaps = actual.get("gaps") or []
    covered = 0
    for window in expected["gaps"]:
        e_from, e_to = _window(window)
        for reported_gap in gaps:
            try:
                a_from, a_to = _window(reported_gap)
            except (KeyError, ValueError):
                continue
            if overlap_days(e_from, e_to, a_from, a_to) >= 1:
                covered += 1
                break
    coverage = covered / len(expected["gaps"]) if expected["gaps"] else 1.0

    score = (float(mechanism_ok) + coverage + float(declared_imputation)) / 3.0
    return {
        "task": "missingness", "score": round(score, 3),
        "detail": {
            "mechanism_expected": expected["mechanism"],
            "mechanism_reported": actual.get("mechanism"),
            "mechanism_ok": mechanism_ok,
            "gaps_expected": len(expected["gaps"]), "gaps_covered": covered,
            "imputation_declared": declared_imputation,
        },
    }


def score_source_divergence(truth: dict, report: dict) -> dict:
    expected_list = truth.get("source_divergence") or []
    if not expected_list:
        return None
    reported = list(report.get("source_divergence") or [])

    hits, notes = 0, []
    for expected in expected_list:
        found = None
        for actual in reported:
            if actual.get("metric") != expected["metric"]:
                continue
            if not _pairs_match(expected["source_a"], expected["source_b"],
                                actual.get("source_a"), actual.get("source_b")):
                continue
            if actual.get("kind") != expected["kind"]:
                continue
            value = actual.get("effect")
            if not isinstance(value, (int, float)):
                continue
            # Расхождение могло быть посчитано в обратную сторону.
            if expected["kind"] == "multiplicative":
                variants = [value, (1.0 / value) if value else None]
            else:
                variants = [value, -value]
            if any(v is not None and abs(v - expected["effect"]) <= expected["tolerance"]
                   for v in variants):
                found = value
                break
        if found is not None:
            hits += 1
            notes.append({"id": expected["id"], "verdict": "найдено",
                          "reported_effect": found})
        else:
            notes.append({"id": expected["id"],
                          "verdict": "не найдено или вне допуска"})

    return {
        "task": "source_divergence", "score": round(hits / len(expected_list), 3),
        "detail": {"expected": len(expected_list), "hits": hits, "notes": notes},
    }


SCORERS = (score_events, score_control, score_couplings,
           score_trends, score_missingness, score_source_divergence)

TASK_TITLES = {
    "events": "События: найдены и правильно названы",
    "control_windows": "Контрольные окна: ничего не выдумано",
    "couplings": "Связи между метриками",
    "trends": "Тренды: физиология или артефакт",
    "missingness": "Пропуски: механизм и границы",
    "source_divergence": "Расхождения между источниками",
}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def load_report(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"отчёт агента не найден: {path}\n"
            f"Агент должен положить report.json в корень рабочего каталога.")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"report.json не разбирается как JSON: {exc}")
    if not isinstance(report, dict):
        raise SystemExit("report.json должен быть объектом верхнего уровня")
    version = report.get("schema_version")
    if version != REPORT_SCHEMA_VERSION:
        print(f"Внимание: отчёт заявляет схему {version!r}, оценщик рассчитан на "
              f"{REPORT_SCHEMA_VERSION!r}. Считаю как есть.", file=sys.stderr)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Официальный оценщик отчёта агента.")
    parser.add_argument("--run-dir", required=True, help="каталог прогона от prepare.py")
    parser.add_argument("--report", default=None,
                        help="путь к отчёту (по умолчанию <run-dir>/workspace/report.json)")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    truth_path = run_dir / "private" / "truth.json"
    if not truth_path.exists():
        raise SystemExit(f"эталон не найден: {truth_path}")
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    if truth.get("schema_version") != TRUTH_SCHEMA_VERSION:
        raise SystemExit(
            f"эталон версии {truth.get('schema_version')!r}, оценщик рассчитан на "
            f"{TRUTH_SCHEMA_VERSION!r}. Пересоберите прогон.")

    report = load_report(Path(args.report) if args.report
                         else run_dir / "workspace" / "report.json")

    tasks = [result for result in (scorer(truth, report) for scorer in SCORERS)
             if result is not None]
    aggregate = sum(t["score"] for t in tasks) / len(tasks) if tasks else 0.0

    width = max(len(TASK_TITLES[t["task"]]) for t in tasks)
    print(f"Прогон: {run_dir}   генератор: {truth['generator']}")
    print("-" * (width + 10))
    for task in tasks:
        print(f"{TASK_TITLES[task['task']]:<{width}}  {task['score']:.3f}")
    print("-" * (width + 10))
    print(f"{'Итог':<{width}}  {aggregate:.3f}")

    control = next((t for t in tasks if t["task"] == "control_windows"), None)
    if control and control["detail"]["false_alarms"]:
        print()
        print(f"Ложных срабатываний в спокойных окнах: "
              f"{control['detail']['false_alarms']}")
        for alarm in control["detail"]["alarms"]:
            print(f"  {alarm['control']}: заявлено {alarm['reported']!r}, "
                  f"пересечение {alarm['overlap_days']} сут")

    score = {
        "generator": truth["generator"],
        "aggregate": round(aggregate, 3),
        "tasks": tasks,
        "scorer_version": TRUTH_SCHEMA_VERSION,
    }
    out_path = run_dir / "score.json"
    out_path.write_text(json.dumps(score, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print()
    print(f"Подробности: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
