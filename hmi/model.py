"""Типы данных, общие для генератора и для расчёта доменов."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

DAYS_IN_WEEK = 7


@dataclass(frozen=True)
class DailyRecord:
    """Один день трекинга практик одного человека.

    day      — индекс дня от начала окна наблюдения (0-based, без пропусков).
    planned  — сколько сессий практик запланировано на этот день по плану.
    done     — сколько из них реально выполнено (0 <= done <= planned).
    context  — пометки контекста дня: ("travel",), ("illness",) и т.п.
               Домен У использует их только для выбора «спокойных» недель
               при расчёте базовой линии; сами эпизады срыва детектируются
               по данным, а не по пометкам.
    """

    day: int
    planned: int
    done: int
    context: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.planned < 0 or self.done < 0:
            raise ValueError(f"day {self.day}: planned/done не могут быть < 0")
        if self.done > self.planned:
            raise ValueError(
                f"day {self.day}: done={self.done} > planned={self.planned}"
            )


@dataclass(frozen=True)
class DomainResult:
    """Результат расчёта одного домена.

    score       — балл 1..5, либо None, если домен не поддаётся оценке.
    raw         — сырое значение в [0, 1] до перевода в баллы (None вместе со score).
    components  — вклад отдельных компонентов формулы (для объяснимости).
    diagnostics — промежуточные величины: базовая линия, эпизоды, недели и т.п.
    reason      — причина, по которой score is None ("insufficient_data" и т.п.).
    """

    domain: str
    score: Optional[int]
    raw: Optional[float]
    components: dict[str, float] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    reason: Optional[str] = None

    @property
    def is_scored(self) -> bool:
        return self.score is not None


def window_adherence(records: Sequence[DailyRecord]) -> Optional[float]:
    """Доля выполнения плана на произвольном отрезке дней.

        adh = min(1, sum(done) / sum(planned))

    Возвращает None, если на отрезке вообще ничего не было запланировано
    (делить не на что — это не «ноль дисциплины», это отсутствие плана).
    Обрезка сверху нужна, чтобы перевыполнение в один день не компенсировало
    провал в другой: регулярность — это про ритм, а не про суммарный объём.
    """
    planned = sum(r.planned for r in records)
    if planned == 0:
        return None
    done = sum(r.done for r in records)
    return min(1.0, done / planned)


def split_into_weeks(
    timeline: Sequence[DailyRecord],
) -> list[list[DailyRecord]]:
    """Режет таймлайн на календарные недели по 7 дней с начала окна.

    Хвост короче 7 дней отбрасывается: неполная неделя исказила бы и среднее,
    и разброс.
    """
    ordered = sorted(timeline, key=lambda r: r.day)
    full_weeks = len(ordered) // DAYS_IN_WEEK
    return [
        ordered[i * DAYS_IN_WEEK : (i + 1) * DAYS_IN_WEEK]
        for i in range(full_weeks)
    ]
