"""Детерминированный генератор синтетических данных о практиках.

Данные имитируют дневник практик за 12 недель: сколько практик было
запланировано на день, сколько реально выполнено и был ли в этот день сбой
режима (командировка, болезнь, срыв).

Детерминированность обеспечивается тремя вещами:
  1. единственный источник случайности — random.Random(SEED + смещение профиля);
  2. никакого обращения к системному времени: окно задано константой START_DATE;
  3. округление — явное «половина вверх», а не банковское round() из Python.

Поэтому `python -m hmi.generate` всегда переписывает CSV байт в байт тем же
содержимым. Сам CSV закоммичен в репозиторий, так что расчёт воспроизводим
даже без перегенерации.
"""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# --- Константы генерации -----------------------------------------------------

SEED = 20260827
WINDOW_DAYS = 84  # 12 недель
START_DATE = date(2026, 6, 4)  # последний день окна — 2026-08-26

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "synthetic_practices.csv"

CSV_FIELDS = ("date", "profile", "planned", "done", "disruption")

NO_DISRUPTION = "none"

# Насколько сильно проседает выполнение практик во время сбоя разного типа.
# Доля от плана, которая всё-таки выполняется в дни эпизода.
DISRUPTION_LEVEL = {
    "travel": 0.15,
    "illness": 0.10,
    "breakdown": 0.05,
}


@dataclass(frozen=True)
class Episode:
    """Сбой режима: начинается в день `start` (0-based) и длится `length` дней."""

    start: int
    length: int
    kind: str


@dataclass(frozen=True)
class ProfileSpec:
    """Портрет человека, из которого разворачивается дневник практик."""

    name: str
    title: str
    planned_per_day: int
    base_level: float  # обычная доля выполнения плана
    wobble: float  # разброс изо дня в день
    zero_day_rate: float  # вероятность полностью пропущенного дня вне эпизодов
    recovery_days: int  # за сколько дней после эпизода выходит на базовую линию
    episodes: tuple[Episode, ...]


# Три профиля подобраны так, чтобы домены получили заметно разные баллы:
# ровный ритм и быстрый возврат; рваный ритм и долгий возврат; середина.
PROFILES: tuple[ProfileSpec, ...] = (
    ProfileSpec(
        name="steady",
        title="Ровный ритм",
        planned_per_day=4,
        base_level=0.88,
        wobble=0.07,
        zero_day_rate=0.03,
        recovery_days=3,
        episodes=(
            Episode(start=21, length=5, kind="travel"),
            Episode(start=56, length=3, kind="illness"),
        ),
    ),
    ProfileSpec(
        name="erratic",
        title="Рваный ритм",
        planned_per_day=4,
        base_level=0.55,
        wobble=0.30,
        zero_day_rate=0.22,
        recovery_days=16,
        episodes=(
            Episode(start=14, length=7, kind="breakdown"),
            Episode(start=38, length=6, kind="travel"),
            Episode(start=60, length=4, kind="breakdown"),
        ),
    ),
    ProfileSpec(
        name="typical",
        title="Обычный, с командировками",
        planned_per_day=4,
        base_level=0.78,
        wobble=0.12,
        zero_day_rate=0.06,
        recovery_days=6,
        episodes=(
            Episode(start=17, length=6, kind="travel"),
            Episode(start=45, length=5, kind="travel"),
            Episode(start=66, length=2, kind="illness"),
        ),
    ),
)


def _round_half_up(value: float) -> int:
    """Округление «половина вверх».

    Встроенный round() округляет 0.5 к чётному (round(0.5) == 0), что
    воспроизводимо, но контринтуитивно при чтении данных. Здесь нужна
    предсказуемость для человека, а не только для машины.
    """
    return math.floor(value + 0.5)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _disruption_by_day(spec: ProfileSpec) -> list[str]:
    """Разворачивает список эпизодов в поденную разметку сбоев."""
    marks = [NO_DISRUPTION] * WINDOW_DAYS
    for episode in spec.episodes:
        for offset in range(episode.length):
            day = episode.start + offset
            if 0 <= day < WINDOW_DAYS:
                marks[day] = episode.kind
    return marks


def _days_since_episode_end(spec: ProfileSpec, day: int) -> int | None:
    """Сколько дней прошло с конца ближайшего предшествующего эпизода.

    Возвращает None, если до этого дня эпизодов не было. 1 означает
    «первый день после сбоя».
    """
    ends = [e.start + e.length - 1 for e in spec.episodes if e.start + e.length - 1 < day]
    if not ends:
        return None
    return day - max(ends)


def generate_profile(spec: ProfileSpec, seed_offset: int) -> list[dict[str, object]]:
    """Строит дневник одного профиля на WINDOW_DAYS дней.

    Уровень выполнения в день складывается из трёх слагаемых:
      * эпизод сбоя — уровень падает до DISRUPTION_LEVEL[kind];
      * окно восстановления — линейный подъём от 30% базы к 100% за
        spec.recovery_days дней;
      * обычный день — база плюс равномерный шум ±wobble.
    Плюс редкие полностью пропущенные дни вне эпизодов.
    """
    rng = random.Random(SEED + seed_offset)
    marks = _disruption_by_day(spec)
    rows: list[dict[str, object]] = []

    for day in range(WINDOW_DAYS):
        disruption = marks[day]

        if disruption != NO_DISRUPTION:
            level = DISRUPTION_LEVEL[disruption]
            forced_zero = False
        else:
            since = _days_since_episode_end(spec, day)
            if since is not None and since <= spec.recovery_days:
                ramp = 0.30 + 0.70 * (since / spec.recovery_days)
                level = spec.base_level * ramp
            else:
                level = spec.base_level
            level += rng.uniform(-spec.wobble, spec.wobble)
            forced_zero = rng.random() < spec.zero_day_rate

        done = 0 if forced_zero else _round_half_up(_clip(level) * spec.planned_per_day)

        rows.append(
            {
                "date": (START_DATE + timedelta(days=day)).isoformat(),
                "profile": spec.name,
                "planned": spec.planned_per_day,
                "done": done,
                "disruption": disruption,
            }
        )

    return rows


def generate_all() -> list[dict[str, object]]:
    """Дневники всех профилей, в фиксированном порядке PROFILES."""
    rows: list[dict[str, object]] = []
    for index, spec in enumerate(PROFILES):
        rows.extend(generate_profile(spec, seed_offset=index))
    return rows


def write_csv(path: Path = DATA_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" + lineterminator="\n" — чтобы файл был одинаков на всех ОС.
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(generate_all())
    return path


def read_csv(path: Path = DATA_PATH) -> list[dict[str, object]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            {
                "date": row["date"],
                "profile": row["profile"],
                "planned": int(row["planned"]),
                "done": int(row["done"]),
                "disruption": row["disruption"],
            }
            for row in csv.DictReader(handle)
        ]


if __name__ == "__main__":
    print(f"записано: {write_csv()}")
