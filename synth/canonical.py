"""Канонический формат слоя приёма.

Реализует то, что описано в canonical-format.md: наблюдение, реестр метрик,
запись потока JSONL и проверка записи на соответствие схеме 1.0.

Этот модуль общий для всех адаптеров. Генератор `synth` пользуется им ровно
так же, как им будут пользоваться адаптеры Apple Health, WHOOP и кольца Сбера.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = "1.0"

DAILY = "daily"
POINT = "point"
INTERVAL = "interval"
AGGREGATIONS = (DAILY, POINT, INTERVAL)

# Как получено значение. Это не метаданные «на всякий случай»: вместе с
# method_detail пара задаёт условие сопоставимости (решение 2 формата).
MEASURED = "measured"      # прибор измерил напрямую
DERIVED = "derived"        # вендор вычислил из своих сырых данных
AGGREGATED = "aggregated"  # свёртка за период (сумма шагов за сутки)
IMPUTED = "imputed"        # восстановлено, а не измерено
METHODS = (MEASURED, DERIVED, AGGREGATED, IMPUTED)


@dataclass(frozen=True)
class MetricSpec:
    """Строка реестра метрик: одна метрика — одна каноническая единица."""

    unit: str
    aggregation: str
    lo: float
    hi: float
    description: str


# Реестр метрик 1.0. Границы lo/hi — физически возможные, а не «нормальные»:
# их дело ловить сломанный адаптер, а не ставить диагноз.
METRICS: dict[str, MetricSpec] = {
    # Сердечный ритм и вегетатика
    "hr.resting": MetricSpec("bpm", DAILY, 25, 130, "пульс покоя за сутки"),
    "hr.max_daily": MetricSpec("bpm", DAILY, 60, 230, "максимальный пульс за сутки"),
    # RMSSD и SDNN — РАЗНЫЕ метрики, а не одна от разных источников: разная
    # математика, разное время суток, значения отличаются в разы. Складывать
    # их в один ряд нельзя, и словарь этого просто не позволяет.
    "hrv.rmssd": MetricSpec("ms", DAILY, 3, 300, "вариабельность RMSSD"),
    "hrv.sdnn": MetricSpec("ms", DAILY, 3, 400, "вариабельность SDNN"),
    "respiratory.rate": MetricSpec("brpm", DAILY, 5, 40, "частота дыхания во сне"),
    # Сон
    "sleep.duration": MetricSpec("min", DAILY, 0, 1080, "фактическое время сна"),
    "sleep.efficiency": MetricSpec("pct", DAILY, 0, 100, "доля сна от времени в постели"),
    "sleep.onset": MetricSpec("min_from_midnight", DAILY, -420, 720, "момент засыпания, со знаком"),
    "sleep.offset": MetricSpec("min_from_midnight", DAILY, -240, 1080, "момент пробуждения"),
    # Активность и тело
    "activity.steps": MetricSpec("count", DAILY, 0, 100000, "шаги"),
    "activity.active_energy": MetricSpec("kcal", DAILY, 0, 8000, "активные килокалории"),
    "workout.load": MetricSpec("au", DAILY, 0, 1000, "тренировочная нагрузка"),
    "body.temp_deviation": MetricSpec("degC", DAILY, -3, 5, "отклонение ночной температуры"),
    "body.weight": MetricSpec("kg", POINT, 20, 400, "вес"),
    # Самоотчёт
    "context.alcohol_units": MetricSpec("unit", DAILY, 0, 40, "алкоголь за вечер этого дня"),
    "subjective.energy": MetricSpec("score_1_5", DAILY, 1, 5, "самооценка энергии"),
    # Лаборатория
    "lab.ferritin": MetricSpec("ng/mL", POINT, 1, 3000, "ферритин"),
    "lab.crp": MetricSpec("mg/L", POINT, 0, 500, "C-реактивный белок"),
    "lab.hemoglobin": MetricSpec("g/L", POINT, 30, 250, "гемоглобин"),
    "lab.vitamin_d": MetricSpec("ng/mL", POINT, 1, 200, "витамин D, 25-OH"),
    "lab.hba1c": MetricSpec("pct", POINT, 2, 20, "гликированный гемоглобин"),
    "lab.tsh": MetricSpec("mIU/L", POINT, 0.01, 100, "тиреотропный гормон"),
}

LAB_METRICS = tuple(m for m in METRICS if m.startswith("lab."))

# Метрики носимого устройства: они исчезают вместе, когда устройство не надето.
# Самоотчёт и вес в этот блок не входят — у них своя механика пропусков.
WEARABLE_METRICS = (
    "hr.resting",
    "hr.max_daily",
    "hrv.rmssd",
    "respiratory.rate",
    "body.temp_deviation",
    "sleep.duration",
    "sleep.efficiency",
    "sleep.onset",
    "sleep.offset",
    "activity.steps",
    "activity.active_energy",
)

# Приоритет вендоров при конфликте за один и тот же день (решение 6 формата).
# Слой приёма его не применяет — он лишь предлагает потребителю опору.
VENDOR_PRECEDENCE: dict[str, tuple[str, ...]] = {
    "hr.resting": ("whoop", "sber_ring", "apple_health"),
    "hrv.rmssd": ("whoop", "sber_ring"),
    "hrv.sdnn": ("apple_health",),
    "activity.steps": ("apple_health", "sber_ring", "whoop"),
}


@dataclass(frozen=True)
class Source:
    vendor: str
    adapter_version: str
    device: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"vendor": self.vendor, "adapter_version": self.adapter_version}
        if self.device is not None:
            out["device"] = self.device
        return out


@dataclass(frozen=True)
class Observation:
    """Одно наблюдение — одна строка выходного JSONL."""

    subject_id: str
    metric: str
    value: float
    effective_date: date
    timezone: str
    source: Source
    method: str = MEASURED
    method_detail: str = ""
    effective_start: datetime | None = None
    effective_end: datetime | None = None
    confidence: float | None = None
    schema_version: str = SCHEMA_VERSION

    @property
    def imputed(self) -> bool:
        return self.method == IMPUTED

    @property
    def comparability_key(self) -> tuple[str, str]:
        """Сравнивать абсолютные значения можно только внутри одной такой пары."""
        return (self.metric, self.method_detail)

    @property
    def unit(self) -> str:
        return METRICS[self.metric].unit

    @property
    def aggregation(self) -> str:
        return METRICS[self.metric].aggregation

    def to_dict(self) -> dict[str, Any]:
        quality: dict[str, Any] = {"imputed": self.imputed}
        if self.confidence is not None:
            quality["confidence"] = round(self.confidence, 4)
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "subject_id": self.subject_id,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "method": self.method,
            "method_detail": self.method_detail,
            "effective_date": self.effective_date.isoformat(),
            "timezone": self.timezone,
            "aggregation": self.aggregation,
            "source": self.source.to_dict(),
            "quality": quality,
        }
        if self.effective_start is not None:
            out["effective_start"] = self.effective_start.isoformat()
        if self.effective_end is not None:
            out["effective_end"] = self.effective_end.isoformat()
        return out

    def to_json(self) -> str:
        # sort_keys + фиксированные разделители: два прогона с одним seed
        # обязаны дать побайтово одинаковый файл.
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ValidationError(Exception):
    pass


def validate_record(rec: dict[str, Any], line_no: int | None = None) -> list[str]:
    """Проверить одну разобранную запись. Возвращает список нарушений."""
    where = f"строка {line_no}: " if line_no is not None else ""
    problems: list[str] = []

    def bad(msg: str) -> None:
        problems.append(where + msg)

    for key in ("schema_version", "subject_id", "metric", "value", "unit",
                "method", "method_detail",
                "effective_date", "timezone", "aggregation", "source", "quality"):
        if key not in rec:
            bad(f"нет обязательного поля {key!r}")
    if problems:
        return problems

    if rec["schema_version"] != SCHEMA_VERSION:
        bad(f"версия схемы {rec['schema_version']!r}, ожидалась {SCHEMA_VERSION!r}")

    metric = rec["metric"]
    spec = METRICS.get(metric)
    if spec is None:
        bad(f"метрика {metric!r} отсутствует в реестре")
        return problems

    if rec["unit"] != spec.unit:
        bad(f"{metric}: единица {rec['unit']!r}, каноническая {spec.unit!r}")
    if rec["aggregation"] != spec.aggregation:
        bad(f"{metric}: агрегация {rec['aggregation']!r}, ожидалась {spec.aggregation!r}")

    if rec["method"] not in METHODS:
        bad(f"{metric}: method {rec['method']!r} не из {METHODS}")
    if not isinstance(rec["method_detail"], str) or not rec["method_detail"]:
        bad(f"{metric}: method_detail пуст — без него значение не с чем сравнивать")

    value = rec["value"]
    if value is None:
        bad(f"{metric}: value равно null — пропуск выражается отсутствием строки")
    elif not isinstance(value, (int, float)) or isinstance(value, bool):
        bad(f"{metric}: value не число ({value!r})")
    elif not (spec.lo <= value <= spec.hi):
        bad(f"{metric}: значение {value} вне границ реестра [{spec.lo}, {spec.hi}]")

    try:
        date.fromisoformat(rec["effective_date"])
    except (TypeError, ValueError):
        bad(f"effective_date {rec['effective_date']!r} не разбирается")

    start, end = rec.get("effective_start"), rec.get("effective_end")
    if start is not None and end is not None:
        try:
            if datetime.fromisoformat(start) > datetime.fromisoformat(end):
                bad("effective_start позже effective_end")
        except (TypeError, ValueError):
            bad("effective_start/effective_end не разбираются")

    source = rec["source"]
    if not isinstance(source, dict) or "vendor" not in source or "adapter_version" not in source:
        bad("source обязан содержать vendor и adapter_version")

    quality = rec["quality"]
    if not isinstance(quality, dict) or not isinstance(quality.get("imputed"), bool):
        bad("quality.imputed обязан быть булевым")
    elif "confidence" in quality and not (0.0 <= quality["confidence"] <= 1.0):
        bad(f"quality.confidence {quality['confidence']} вне [0, 1]")

    return problems


def write_jsonl(observations: Iterable[Observation], path: str) -> int:
    """Записать поток наблюдений. Возвращает число записанных строк."""
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for obs in observations:
            fh.write(obs.to_json())
            fh.write("\n")
            count += 1
    return count


def read_jsonl(path: str) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
