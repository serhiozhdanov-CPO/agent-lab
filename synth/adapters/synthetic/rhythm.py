"""Недельный ритм: будни против выходных (паттерн P-01).

Ночь относится ко дню пробуждения (правило атрибуции суток из canonical-format.md),
поэтому «поздние» ночи с пятницы и субботы становятся днями пробуждения 5 и 6.
Надбавка к пульсу покоя задана отдельным вектором и смещена вперёд относительно
поздних ночей: пик приходится на понедельник, а не на сами выходные.
"""

from __future__ import annotations

MONDAY, SATURDAY, SUNDAY = 0, 5, 6
LATE_NIGHT_WAKE_DAYS = (SATURDAY, SUNDAY)

# Сон. onset — минуты от полуночи дня пробуждения, со знаком.
SLEEP_ONSET_WEEKDAY = -25.0          # 23:35
SLEEP_ONSET_WEEKEND_SHIFT = 55.0     # → 00:30
SLEEP_DURATION_WEEKDAY = 411.0
SLEEP_DURATION_WEEKEND_SHIFT = 25.0
SLEEP_EFFICIENCY_WEEKDAY = 90.5
SLEEP_EFFICIENCY_WEEKEND_SHIFT = -2.0

# Надбавка к пульсу покоя по дню недели, пн..вс. Пик в понедельник — это
# отложенная расплата за сдвиг сна, а не «стресс начала недели».
RHR_BY_WEEKDAY = (3.0, 0.8, 0.0, 0.0, 0.0, 0.5, 1.0)

# Шаги по дню недели, пн..вс.
STEPS_BY_WEEKDAY = (9500.0, 9500.0, 9500.0, 9500.0, 9800.0, 12500.0, 7000.0)
STEPS_SD = 2200.0

# Тренировочная нагрузка по дню недели, пн..вс: вт, чт — интервалы, сб — длинная,
# вс — восстановительная. Понедельник, среда и пятница — отдых.
WORKOUT_BY_WEEKDAY = (0.0, 75.0, 0.0, 75.0, 0.0, 85.0, 30.0)
WORKOUT_SD = 12.0


def is_late_night_wake_day(weekday: int) -> bool:
    return weekday in LATE_NIGHT_WAKE_DAYS


def sleep_targets(weekday: int) -> tuple[float, float, float]:
    """Целевые (момент засыпания, длительность, эффективность) для дня недели."""
    if is_late_night_wake_day(weekday):
        return (
            SLEEP_ONSET_WEEKDAY + SLEEP_ONSET_WEEKEND_SHIFT,
            SLEEP_DURATION_WEEKDAY + SLEEP_DURATION_WEEKEND_SHIFT,
            SLEEP_EFFICIENCY_WEEKDAY + SLEEP_EFFICIENCY_WEEKEND_SHIFT,
        )
    return SLEEP_ONSET_WEEKDAY, SLEEP_DURATION_WEEKDAY, SLEEP_EFFICIENCY_WEEKDAY


def rhr_offset(weekday: int) -> float:
    return RHR_BY_WEEKDAY[weekday]


def steps_target(weekday: int) -> float:
    return STEPS_BY_WEEKDAY[weekday]


def workout_target(weekday: int) -> float:
    return WORKOUT_BY_WEEKDAY[weekday]
