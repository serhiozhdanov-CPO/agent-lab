#!/usr/bin/env python3
"""Подготовка прогона: развести данные и ключи ответов по разным каталогам.

Генераторы кладут данные и эталон в один каталог — `records.csv` и рядом
`answer-key.json`. Для человека это удобно, для замера агента это дыра: агент с
доступом к каталогу найдёт ключ раньше, чем начнёт анализ, и любые цифры вида
«нашёл N из 7» перестают что-либо доказывать.

Этот скрипт запускает генератор во временный каталог и раскладывает результат
надвое:

    runs/<id>/workspace/   <- сюда пускают агента: только данные и задание
    runs/<id>/private/     <- эталон; агент сюда не ходит, оценщик ходит

Перед выдачей прогона `workspace/` проверяется сканером утечек. Если в нём
нашлось хоть что-то из запретного списка, прогон не выдаётся вообще: лучше
никакого замера, чем замер, про который нельзя сказать, честный он или нет.

    python3 eval/prepare.py --generator health-synth --seed 42
    python3 eval/prepare.py --generator health-data --seed 7 --run-dir /tmp/run7
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapters import ADAPTERS                                    # noqa: E402
from harness import (REPO_ROOT, REPORT_SCHEMA_VERSION,           # noqa: E402
                     redact_markdown, scan_for_leaks, sha256_file)

EVAL_DIR = Path(__file__).resolve().parent

# Документы, которые едут к агенту как есть: это контракты, а не ответы.
VERBATIM_DOCS = ("report-schema.md",)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Подготовить изолированный прогон для оценки агента.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--generator", choices=sorted(ADAPTERS), required=True,
                        help="какой генератор данных использовать")
    parser.add_argument("--seed", type=int, default=42, help="сид генератора")
    parser.add_argument("--age", type=int, default=38, help="возраст субъекта")
    parser.add_argument("--run-dir", default=None,
                        help="каталог прогона (по умолчанию eval/runs/<генератор>-s<сид>)")
    parser.add_argument("--force", action="store_true",
                        help="перезаписать каталог прогона, если он существует")
    return parser.parse_args(argv)


def run_generator(adapter, staging: Path, seed: int, age: int) -> None:
    script = REPO_ROOT / adapter.script
    if not script.exists():
        raise SystemExit(f"генератор не найден: {script}")
    command = [sys.executable, str(script)] + adapter.args(str(staging), seed, age)
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(f"генератор завершился с кодом {result.returncode}")


def build_workspace(adapter, staging: Path, workspace: Path, calendar: dict) -> list:
    """Собрать рабочий каталог агента. Возвращает список положенных файлов."""
    workspace.mkdir(parents=True)
    written = []

    for name in adapter.data_files:
        source = staging / name
        if not source.exists():
            raise SystemExit(f"генератор не создал ожидаемый файл данных: {name}")
        shutil.copy2(source, workspace / name)
        written.append(name)

    for source_rel, dest_name in adapter.docs:
        source = REPO_ROOT / source_rel
        text = source.read_text(encoding="utf-8")
        redacted, count = redact_markdown(text, adapter.redactions, adapter.drop_headings)
        header = (
            f"> Подготовлено для прогона оценки: из документа убрано {count} "
            f"фрагментов, описывающих содержимое конкретно этого датасета.\n"
            f"> Требования к методике аналитики оставлены без изменений.\n\n"
        )
        (workspace / dest_name).write_text(header + redacted, encoding="utf-8")
        written.append(dest_name)

    for name in VERBATIM_DOCS:
        shutil.copy2(EVAL_DIR / name, workspace / name)
        written.append(name)

    template = (EVAL_DIR / "TASK.template.md").read_text(encoding="utf-8")
    task = template.format(
        start=calendar["start"], end=calendar["end"], days=calendar["days"],
        report_schema_version=REPORT_SCHEMA_VERSION,
        data_files=", ".join(f"`{n}`" for n in adapter.data_files),
    )
    (workspace / "TASK.md").write_text(task, encoding="utf-8")
    written.append("TASK.md")

    return written


def main(argv=None) -> int:
    args = parse_args(argv)
    adapter = ADAPTERS[args.generator]

    run_dir = Path(args.run_dir) if args.run_dir else (
        EVAL_DIR / "runs" / f"{args.generator}-s{args.seed}")
    if run_dir.exists():
        if not args.force:
            print(f"Каталог прогона уже существует: {run_dir}\n"
                  f"Передайте --force, чтобы перезаписать.", file=sys.stderr)
            return 2
        shutil.rmtree(run_dir)

    staging = run_dir / ".staging"
    workspace = run_dir / "workspace"
    private = run_dir / "private"
    staging.mkdir(parents=True)

    try:
        run_generator(adapter, staging, args.seed, args.age)

        raw_path = staging / adapter.truth_file
        if not raw_path.exists():
            raise SystemExit(f"генератор не создал эталон: {adapter.truth_file}")
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        truth = adapter.build_truth(raw)

        private.mkdir(parents=True)
        (private / "truth.json").write_text(
            json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.copy2(raw_path, private / "raw-ground-truth.json")

        written = build_workspace(adapter, staging, workspace, truth["calendar"])
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    leaks = scan_for_leaks(workspace, adapter.denylist)
    if leaks:
        print(f"В рабочем каталоге найдены ответы — прогон не выдан ({len(leaks)}):",
              file=sys.stderr)
        for path, lineno, term in leaks[:20]:
            where = f"{path}:{lineno}" if lineno else path
            print(f"  {where}: {term}", file=sys.stderr)
        if len(leaks) > 20:
            print(f"  ... и ещё {len(leaks) - 20}", file=sys.stderr)
        shutil.rmtree(run_dir, ignore_errors=True)
        return 3

    run_meta = {
        "generator": args.generator,
        "seed": args.seed,
        "age": args.age,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "calendar": truth["calendar"],
        "workspace_files": {
            name: {
                "bytes": (workspace / name).stat().st_size,
                "sha256": sha256_file(workspace / name),
            } for name in sorted(written)
        },
        "leak_scan": "passed",
    }
    (run_dir / "run.json").write_text(
        json.dumps(run_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Прогон готов: {run_dir}")
    print(f"  каталог агента : {workspace}")
    print(f"  эталон         : {private / 'truth.json'}  (агенту не показывать)")
    print(f"  сканер утечек  : чисто, файлов в каталоге агента {len(written)}")
    print()
    print("Дальше: дать агенту ТОЛЬКО каталог агента, получить от него")
    print(f"{workspace / 'report.json'}, затем считать баллы:")
    print(f"  python3 eval/evaluate.py --run-dir {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
