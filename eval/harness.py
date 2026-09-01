#!/usr/bin/env python3
"""Общая механика стенда: схема эталона, редактирование, сканер утечек.

Здесь нет ничего про конкретный генератор — только то, что одинаково для всех.
Знание про конкретные генераторы живёт в adapters.py.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from pathlib import Path

# Версия нормализованного эталона. Оценщик отказывается читать чужую версию:
# молча посчитать баллы по эталону другой формы хуже, чем не посчитать вовсе.
TRUTH_SCHEMA_VERSION = "1.0"

# Версия контракта отчёта агента. Описан в report-schema.md.
REPORT_SCHEMA_VERSION = "1.0"

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Даты
# --------------------------------------------------------------------------

def parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def day_span(start: date, days: int) -> tuple:
    """Окно из `days` суток, начиная со `start`, как пара дат включительно."""
    return start, start + timedelta(days=days - 1)


def overlap_days(a_from: date, a_to: date, b_from: date, b_to: date) -> int:
    """Пересечение двух отрезков дат в сутках. Ноль, если не пересекаются."""
    lo = max(a_from, b_from)
    hi = min(a_to, b_to)
    return max(0, (hi - lo).days + 1)


def iou(a_from: date, a_to: date, b_from: date, b_to: date) -> float:
    """Intersection over union для двух окон дат."""
    inter = overlap_days(a_from, a_to, b_from, b_to)
    if inter == 0:
        return 0.0
    union = ((a_to - a_from).days + 1) + ((b_to - b_from).days + 1) - inter
    return inter / union


# --------------------------------------------------------------------------
# Редактирование документов, которые уезжают в рабочий каталог агента
# --------------------------------------------------------------------------

REDACTION_MARK = "[фрагмент убран при подготовке прогона]"

# Конец предложения: точка/восклицательный/вопросительный знак с пробелом или
# концом строки. Сокращений вида «т. е.» в наших документах нет, так что
# наивного правила достаточно.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _redact_text(text: str, rules: list) -> tuple:
    """Убрать из связного текста предложения, попавшие под правила."""
    kept, dropped = [], 0
    for sentence in _SENTENCE_SPLIT.split(text):
        if any(rule.search(sentence) for rule in rules):
            dropped += 1
            continue
        kept.append(sentence)
    if dropped == 0:
        return text, 0
    cleaned = " ".join(part.strip() for part in kept if part.strip())
    return (cleaned + " " + REDACTION_MARK).strip(), dropped


def _is_block_line(line: str) -> bool:
    """Строка, которая не склеивается с соседями в один абзац."""
    stripped = line.strip()
    return (not stripped
            or stripped.startswith("|")
            or stripped.startswith("#")
            or stripped.startswith("```")
            or stripped in ("---", "***", "___"))


def redact_markdown(text: str, rules: list, drop_headings: tuple = ()) -> tuple:
    """Вырезать из markdown всё, что рассказывает о содержимом этого датасета.

    Разделы из `drop_headings` выбрасываются целиком вместе с вложенными
    подразделами. В остальном тексте убираются предложения, попавшие под
    `rules`: в таблицах — по ячейкам, в прозе — по предложениям внутри абзаца.

    Единицей для прозы взят именно абзац, а не строка: в этих документах
    предложение регулярно переносится на следующую строку, и порезка по
    строкам оставляла бы обрубки в середине уцелевшего текста.

    Правило разделения: убираем утверждения о том, **что лежит в этих данных**,
    и оставляем методические требования к аналитике. «Не смешивать источники
    внутри одного тренда» — это то, что аналитик обязан знать до начала работы,
    а «в командировке смена пояса неотличима от срыва» — это подсказка.
    """
    rules = [re.compile(p, re.IGNORECASE) if isinstance(p, str) else p for p in rules]
    out, redactions = [], 0
    skip_level = None
    in_fence = False
    paragraph = []

    def flush_paragraph():
        nonlocal redactions
        if not paragraph:
            return
        joined = "\n".join(paragraph)
        if any(rule.search(joined) for rule in rules):
            cleaned, dropped = _redact_text(" ".join(
                line.strip() for line in paragraph), rules)
            redactions += dropped
            out.append(cleaned)
        else:
            out.extend(paragraph)
        paragraph.clear()

    for line in text.splitlines():
        if line.strip().startswith("```"):
            flush_paragraph()
            in_fence = not in_fence
            if skip_level is None:
                out.append(line)
            continue

        if in_fence:
            if skip_level is not None:
                continue
            if any(rule.search(line) for rule in rules):
                cleaned, dropped = _redact_text(line, rules)
                redactions += dropped
                out.append(cleaned)
            else:
                out.append(line)
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush_paragraph()
            level, title = len(heading.group(1)), heading.group(2).strip()
            if skip_level is not None and level <= skip_level:
                skip_level = None          # вышли из выброшенного раздела
            if skip_level is None and title in drop_headings:
                skip_level = level
                redactions += 1
                out.extend(["#" * level + " " + title, "", REDACTION_MARK, ""])
                continue
        if skip_level is not None:
            continue

        if _is_block_line(line):
            flush_paragraph()
            if line.lstrip().startswith("|"):
                cells = line.split("|")
                for i, cell in enumerate(cells):
                    cleaned, dropped = _redact_text(cell, rules)
                    if dropped:
                        cleaned = f" {cleaned.strip()} "   # вернуть отбивку ячейки
                    cells[i] = cleaned
                    redactions += dropped
                out.append("|".join(cells))
            else:
                out.append(line)
            continue

        paragraph.append(line)

    flush_paragraph()
    return "\n".join(out) + "\n", redactions


# --------------------------------------------------------------------------
# Сканер утечек
# --------------------------------------------------------------------------

# Признаки, которых не должно быть в рабочем каталоге агента ни при каком
# генераторе. Это последняя проверка перед выдачей прогона: если сюда что-то
# просочилось, прогон не выдаётся вообще.
#
# Список намеренно структурный — имена файлов, идентификаторы паттернов, ключи
# из эталонных файлов. Отдельные слова из предметной области сюда не входят:
# перечень допустимых значений в контракте отчёта (`MCAR` | `MAR` | `MNAR`)
# ответа не выдаёт, потому что называет все варианты симметрично. Утечка — это
# утверждение о том, что в данных, а не словарь того, что бывает.
CORE_DENYLIST = (
    r"answer[-_]key",
    r"expected-patterns",
    r"эталонные ответы",
    r"must_not_conclude",
    r"negative_controls",
    r"baselines_from_age",
    r"forced_not_worn",
    r"\bP\d{1,2}_[a-z]",          # P2_trip, P1_stable_rhythm, P4_late_bedtime
    r"\bNC\d_[a-z]",              # NC1_quiet_week, NC2_source_bias
)

# Имена файлов, которые не имеют права оказаться в рабочем каталоге.
FORBIDDEN_NAMES = (
    "answer-key.json",
    "manifest.json",
    "expected-patterns.md",
    "truth.json",
    "raw-ground-truth.json",
    "score.json",
)

# Расширения, которые сканируются как текст. Всё остальное (архивы, бинарь)
# в рабочий каталог не попадает по построению — prepare.py копирует только
# известный список файлов.
TEXT_SUFFIXES = (".md", ".csv", ".jsonl", ".json", ".txt", ".py")


def scan_for_leaks(root: Path, extra_denylist: tuple = ()) -> list:
    """Найти в каталоге всё, что выдаёт агенту ответы.

    Возвращает список находок `(путь, номер строки, термин)`. Пустой список —
    единственный результат, при котором прогон считается пригодным.
    """
    patterns = [re.compile(p, re.IGNORECASE)
                for p in tuple(CORE_DENYLIST) + tuple(extra_denylist)]
    findings = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)

        if path.name in FORBIDDEN_NAMES:
            findings.append((str(rel), 0, f"запрещённое имя файла: {path.name}"))
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            findings.append((str(rel), 0, f"файл не прочитан: {exc}"))
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in patterns:
                match = pattern.search(line)
                if match:
                    findings.append((str(rel), lineno, match.group(0)))

    return findings


# --------------------------------------------------------------------------
# Провенанс
# --------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
