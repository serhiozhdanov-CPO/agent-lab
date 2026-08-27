"""Детерминированный генератор синтетических данных трекинга практик.

Каждый профиль получает собственный random.Random(seed) — при одинаковом seed
последовательность дней воспроизводится побитово. Никаких обращений к
системному времени и к глобальному состоянию random.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from hmi.model import DAYS_IN_WEEK, DailyRecord

WEEKS = 12
DAYS = WEEKS * DAYS_IN_WEEK  # 84 дня наблюдения


@dataclass(frozen=True)
class Practice:
    """Практика с целевой частотой в неделю."""

    key: str
    title: str
    per_week: int

    def planned_weekdays(self) -> tuple[int, ...]:
        """Дни недели (0..6), на которые план равномерно раскладывает сессии."""
        return tuple(sorted({int(i * DAYS_IN_WEEK / self.per_week + 0.5) for i in range(self.per_week)}))


PRACTICES: tuple[Practice, ...] = (
    Practice("movement", "движение", 4),
    Practice("sleep_window", "режим сна", 6),
    Practice("nutrition", "питание по плану", 5),
)


@dataclass(frozen=True)
class Disruption:
    """Разрыв режима: командировка или болезнь.

    start    — день начала (0-based)
    length   — длительность в днях
    depth    — множитель вероятности выполнения во время разрыва (0..1)
    recovery — за сколько дней после разрыва человек линейно возвращается к норме
    kind     — пометка контекста, попадающая в DailyRecord.context
    """

    start: int
    length: int
    depth: float
    recovery: int
    kind: str = "travel"


@dataclass(frozen=True)
class Profile:
    """Архетип человека для генерации."""

    person_id: str
    title: str
    seed: int
    base_p: float                       # базовая вероятность выполнить сессию
    week_jitter: float = 0.05           # разброс базовой вероятности по неделям
    trend_per_week: float = 0.0         # линейный рост/спад base_p по неделям
    collapse_from_week: int | None = None  # с этой недели base_p падает до collapse_p
    collapse_p: float = 0.30
    disruptions: tuple[Disruption, ...] = field(default_factory=tuple)


def _day_multiplier(day: int, disruptions: tuple[Disruption, ...]) -> tuple[float, tuple[str, ...]]:
    """Множитель вероятности и пометки контекста для конкретного дня."""
    multiplier = 1.0
    flags: list[str] = []
    for d in disruptions:
        end = d.start + d.length
        if d.start <= day < end:
            multiplier = min(multiplier, d.depth)
            flags.append(d.kind)
        elif end <= day < end + d.recovery:
            # Линейный возврат от depth к 1.0 за d.recovery дней.
            progress = (day - end + 1) / (d.recovery + 1)
            multiplier = min(multiplier, d.depth + (1.0 - d.depth) * progress)
    return multiplier, tuple(flags)


def generate_timeline(profile: Profile, days: int = DAYS) -> list[DailyRecord]:
    """Строит детерминированный таймлайн одного человека."""
    rnd = random.Random(profile.seed)
    weeks = days // DAYS_IN_WEEK

    # Вероятность «нормального» выполнения на каждую неделю — фиксируем заранее,
    # чтобы порядок вызовов rnd не зависел от структуры плана.
    week_p: list[float] = []
    for w in range(weeks):
        p = profile.base_p + profile.trend_per_week * w
        if profile.collapse_from_week is not None and w >= profile.collapse_from_week:
            p = profile.collapse_p
        p += rnd.uniform(-profile.week_jitter, profile.week_jitter)
        week_p.append(min(1.0, max(0.0, p)))

    schedule = {p.key: p.planned_weekdays() for p in PRACTICES}

    records: list[DailyRecord] = []
    for day in range(days):
        week, weekday = divmod(day, DAYS_IN_WEEK)
        multiplier, flags = _day_multiplier(day, profile.disruptions)
        p_day = week_p[week] * multiplier

        planned = 0
        done = 0
        for practice in PRACTICES:
            if weekday not in schedule[practice.key]:
                continue
            planned += 1
            if rnd.random() < p_day:
                done += 1
        records.append(DailyRecord(day=day, planned=planned, done=done, context=flags))

    return records


# --- Набор архетипов ----------------------------------------------------------
# Демо-персона для итогового ответа — P-002: регулярный ритм с двумя
# командировками, то есть оба домена реально нагружены.

PROFILES: tuple[Profile, ...] = (
    Profile(
        person_id="P-001",
        title="Метроном (без разрывов режима)",
        seed=1001,
        base_p=0.93,
        week_jitter=0.04,
    ),
    Profile(
        person_id="P-002",
        title="Командировочный ритм (быстрый возврат)",
        seed=1002,
        base_p=0.88,
        week_jitter=0.05,
        disruptions=(
            Disruption(start=21, length=6, depth=0.20, recovery=3, kind="travel"),
            Disruption(start=56, length=5, depth=0.25, recovery=4, kind="travel"),
        ),
    ),
    Profile(
        person_id="P-003",
        title="Медленный возврат (болезнь + командировка)",
        seed=1003,
        base_p=0.85,
        week_jitter=0.05,
        disruptions=(
            Disruption(start=14, length=8, depth=0.15, recovery=18, kind="illness"),
            Disruption(start=52, length=6, depth=0.20, recovery=16, kind="travel"),
        ),
    ),
    Profile(
        person_id="P-004",
        title="Хаотик (высокий разброс недель)",
        seed=1004,
        base_p=0.62,
        week_jitter=0.32,
    ),
    Profile(
        person_id="P-005",
        title="Растущий (набирает ритм с низкой базы)",
        seed=1005,
        base_p=0.42,
        week_jitter=0.05,
        trend_per_week=0.045,
        disruptions=(Disruption(start=45, length=5, depth=0.25, recovery=6, kind="travel"),),
    ),
    Profile(
        person_id="P-006",
        title="Выгорание (обвал на 6-й неделе без возврата)",
        seed=1006,
        base_p=0.86,
        week_jitter=0.05,
        collapse_from_week=6,
        collapse_p=0.28,
    ),
)

DEMO_PERSON_ID = "P-002"


def generate_dataset(days: int = DAYS) -> dict[str, list[DailyRecord]]:
    """Полный синтетический датасет: person_id -> таймлайн."""
    return {p.person_id: generate_timeline(p, days) for p in PROFILES}
