"""Командная строка генератора.

    python -m synth.cli --age 38 --weeks 16 --seed 42 --out data/
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date

from .adapters.synthetic import GeneratorConfig, SyntheticAdapter
from .canonical import write_jsonl


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="synth",
        description="Генератор синтетических данных о здоровье и режиме "
                    "(адаптер слоя приёма).",
    )
    p.add_argument("--age", type=float, default=38.0,
                   help="возраст субъекта; от него считаются базовые линии (по умолчанию 38)")
    p.add_argument("--weeks", type=int, default=16, help="длительность периода в неделях")
    p.add_argument("--seed", type=int, default=42, help="seed; один seed — один и тот же файл")
    p.add_argument("--start", type=date.fromisoformat, default=date(2026, 1, 5),
                   help="дата начала, YYYY-MM-DD (по умолчанию понедельник 2026-01-05)")
    p.add_argument("--timezone", default="Europe/Moscow", help="пояс субъекта, IANA")
    p.add_argument("--subject-id", default="synth-0001")
    p.add_argument("--weight", type=float, default=78.0, help="стартовый вес, кг")
    p.add_argument("--out", default="data", help="каталог для вывода")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = GeneratorConfig(
        age=args.age,
        weeks=args.weeks,
        seed=args.seed,
        start_date=args.start,
        timezone=args.timezone,
        subject_id=args.subject_id,
        weight_kg=args.weight,
    )

    observations, manifest, truth = SyntheticAdapter().generate(cfg)

    out_dir = os.path.join(args.out, cfg.subject_id)
    os.makedirs(out_dir, exist_ok=True)
    stream_path = os.path.join(out_dir, "observations.jsonl")
    written = write_jsonl(observations, stream_path)

    for name, payload in (("manifest.json", manifest), ("ground_truth.json", truth)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")

    print(f"{written} наблюдений → {stream_path}")
    print(f"период: {manifest['period']['start']} … {manifest['period']['end']} "
          f"({manifest['period']['days']} дней), возраст {cfg.age:g}, seed {cfg.seed}")
    print(f"манифест и ключ ответов → {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
