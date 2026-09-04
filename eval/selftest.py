#!/usr/bin/env python3
"""Самопроверка стенда.

Заявление «ключи изолированы» ничего не стоит, пока оно не проверено. Здесь
проверяется ровно оно, на всех трёх генераторах:

1. Прогон собирается, и в рабочем каталоге агента нет эталона.
2. Подброшенная в рабочий каталог утечка ловится сканером.
3. Оценщик даёт 1.000 идеальному отчёту, собранному из эталона.
4. Оценщик наказывает за выдумку: событие в спокойном окне роняет балл.
5. Оценщик наказывает за молчание: пустой отчёт не даёт полного балла.

    python3 eval/selftest.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapters import ADAPTERS          # noqa: E402
from harness import REPO_ROOT, REPORT_SCHEMA_VERSION, scan_for_leaks   # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent


class Failure(Exception):
    pass


def check(condition, message: str) -> None:
    if not condition:
        raise Failure(message)


def prepare(generator: str, run_dir: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(EVAL_DIR / "prepare.py"),
         "--generator", generator, "--seed", "42", "--run-dir", str(run_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    check(result.returncode == 0,
          f"prepare.py упал на {generator}:\n{result.stdout}\n{result.stderr}")


def evaluate(run_dir: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(EVAL_DIR / "evaluate.py"), "--run-dir", str(run_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    check(result.returncode == 0,
          f"evaluate.py упал:\n{result.stdout}\n{result.stderr}")
    return json.loads((run_dir / "score.json").read_text(encoding="utf-8"))


def perfect_report(truth: dict) -> dict:
    """Отчёт, собранный прямо из эталона: то, что оценщик обязан принять целиком."""
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "baselines": {name: {"median": value}
                      for name, value in truth["baselines"].items()},
        "events": [
            {"id": event["id"], "kind": event["kind"],
             "from": event["from"], "to": event["to"]}
            for event in truth["events"] if not event.get("optional")
        ],
        "couplings": [
            {"cause": c["cause"], "effect": c["effect"], "lag_days": c["lag_days"],
             "effect_size": c["effect_size"], "unit": c["unit"]}
            for c in truth.get("couplings") or [] if not c.get("must_be_absent")
        ],
        "trends": [
            {"metric": t["metric"], "source": t.get("source"),
             "verdict": t["verdict"], "total_change": t.get("total_change"),
             "unit": t.get("unit")}
            for t in truth.get("trends") or []
        ],
        "missingness": {},
        "source_divergence": [
            {"source_a": d["source_a"], "source_b": d["source_b"],
             "metric": d["metric"], "kind": d["kind"], "effect": d["effect"],
             "verdict": "instrumental"}
            for d in truth.get("source_divergence") or []
        ],
    }
    missing = truth.get("missingness")
    if missing:
        report["missingness"] = {
            "mechanism": missing["mechanism"],
            "gaps": [{"from": g["from"], "to": g["to"]} for g in missing["gaps"]],
            "imputed": False,
        }
    return report


def empty_report() -> dict:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "baselines": {}, "events": [], "couplings": [], "trends": [],
        "missingness": {}, "source_divergence": [],
    }


def write_report(run_dir: Path, report: dict) -> None:
    (run_dir / "workspace" / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_for(generator: str, root: Path) -> list:
    checks = []
    run_dir = root / generator
    prepare(generator, run_dir)
    workspace = run_dir / "workspace"
    truth = json.loads((run_dir / "private" / "truth.json").read_text(encoding="utf-8"))

    # 1. В рабочем каталоге нет эталона — ни файлом, ни содержимым.
    check(not scan_for_leaks(workspace, ADAPTERS[generator].denylist),
          f"{generator}: сканер нашёл утечку в чистом прогоне")
    names = {p.name for p in workspace.rglob("*") if p.is_file()}
    check("truth.json" not in names and "answer-key.json" not in names
          and "manifest.json" not in names,
          f"{generator}: эталон оказался в каталоге агента")
    check((run_dir / "private" / "truth.json").exists(),
          f"{generator}: эталон не сохранён в private/")
    checks.append("эталон вне каталога агента")

    # 2. Подброшенная утечка обязана быть поймана.
    planted = workspace / "notes.md"
    planted.write_text("см. answer-key.json, окно P2_trip\n", encoding="utf-8")
    found = scan_for_leaks(workspace, ADAPTERS[generator].denylist)
    planted.unlink()
    check(found, f"{generator}: сканер пропустил подброшенную утечку")
    checks.append(f"подброшенная утечка поймана ({len(found)} находок)")

    # 3. Идеальный отчёт — полный балл.
    write_report(run_dir, perfect_report(truth))
    score = evaluate(run_dir)
    check(abs(score["aggregate"] - 1.0) < 1e-6,
          f"{generator}: идеальный отчёт получил {score['aggregate']}, а не 1.0\n"
          + json.dumps(score["tasks"], ensure_ascii=False, indent=1))
    checks.append("идеальный отчёт = 1.000")

    # 4. Выдумка в спокойном окне роняет балл.
    if truth.get("control_windows"):
        window = truth["control_windows"][0]
        noisy = perfect_report(truth)
        noisy["events"].append({
            "id": "fabricated", "kind": "regimen_collapse",
            "from": window["from"], "to": window["to"],
        })
        write_report(run_dir, noisy)
        noisy_score = evaluate(run_dir)
        check(noisy_score["aggregate"] < score["aggregate"],
              f"{generator}: выдуманное событие не снизило балл")
        control = next(t for t in noisy_score["tasks"] if t["task"] == "control_windows")
        check(control["detail"]["false_alarms"] >= 1,
              f"{generator}: ложное срабатывание не зафиксировано")
        checks.append(f"выдумка наказана: {score['aggregate']:.3f} → "
                      f"{noisy_score['aggregate']:.3f}")

    # 5. Пустой отчёт не даёт полного балла.
    write_report(run_dir, empty_report())
    silent = evaluate(run_dir)
    check(silent["aggregate"] < 1.0,
          f"{generator}: пустой отчёт получил полный балл")
    checks.append(f"пустой отчёт = {silent['aggregate']:.3f}")

    return checks


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory(prefix="agent-lab-selftest-") as tmp:
        root = Path(tmp)
        for generator in sorted(ADAPTERS):
            print(f"[{generator}]")
            try:
                for line in run_for(generator, root):
                    print(f"  ok   {line}")
            except Failure as exc:
                print(f"  FAIL {exc}")
                failures.append(generator)
            except Exception as exc:                      # noqa: BLE001
                print(f"  FAIL неожиданная ошибка: {exc!r}")
                failures.append(generator)

    print()
    if failures:
        print(f"Самопроверка провалена: {', '.join(failures)}")
        return 1
    print(f"Самопроверка пройдена на всех генераторах ({len(ADAPTERS)}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
