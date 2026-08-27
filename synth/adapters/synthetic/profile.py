"""Возрастные базовые линии субъекта (паттерн P-02).

Формулы здесь — эвристики для стенда, а не клинические нормы. Они подобраны
так, чтобы порядок величин и направление возрастных сдвигов были правдоподобны;
использовать их для оценки настоящего человека нельзя.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# Коэффициенты вынесены в константы: на них ссылается expected-patterns.md,
# и tools/check_patterns.py считает по ним ожидаемые значения.
RHR_AT_30 = 60.0
RHR_PER_YEAR = 0.07
RHR_CLAMP = (48.0, 78.0)
RHR_SUBJECT_SD = 2.5

RMSSD_LOG_INTERCEPT = 4.55
RMSSD_LOG_PER_YEAR = 0.018
RMSSD_SUBJECT_LOG_SD = 0.12

HRMAX_INTERCEPT = 208.0          # Tanaka et al.
HRMAX_PER_YEAR = 0.7

# 7.4 ч, а не расхожие «8 часов»: потребность задаёт нулевую точку долга сна
# (P-06), и завышенная константа делала бы долг ненулевым каждый день.
SLEEP_NEED_AT_30_MIN = 7.4 * 60
SLEEP_NEED_PER_YEAR_MIN = 0.008 * 60

RESP_AT_30 = 14.2
RESP_PER_YEAR = 0.01


def expected_rhr(age: float) -> float:
    """Ожидаемый пульс покоя по возрасту, без индивидуального смещения."""
    return min(max(RHR_AT_30 + RHR_PER_YEAR * (age - 30.0), RHR_CLAMP[0]), RHR_CLAMP[1])


def expected_rmssd(age: float) -> float:
    """Ожидаемая вариабельность по возрасту, без индивидуального множителя."""
    return math.exp(RMSSD_LOG_INTERCEPT - RMSSD_LOG_PER_YEAR * age)


@dataclass(frozen=True)
class Baselines:
    """Базовые линии конкретного субъекта: возрастная формула плюс личная поправка."""

    age: float
    rhr: float
    rmssd: float
    hr_max: float
    sleep_need_min: float
    respiratory_rate: float
    weight_kg: float

    @property
    def log_rmssd(self) -> float:
        return math.log(self.rmssd)


def baselines(age: float, rng: random.Random, weight_kg: float = 78.0) -> Baselines:
    """Собрать базовые линии субъекта.

    Индивидуальная поправка — нормальная для пульса и логнормальная для
    вариабельности: разброс RMSSD между людьми мультипликативный, поэтому
    смещение живёт в логарифме.
    """
    return Baselines(
        age=age,
        rhr=expected_rhr(age) + rng.gauss(0.0, RHR_SUBJECT_SD),
        rmssd=expected_rmssd(age) * math.exp(rng.gauss(0.0, RMSSD_SUBJECT_LOG_SD)),
        hr_max=HRMAX_INTERCEPT - HRMAX_PER_YEAR * age,
        sleep_need_min=SLEEP_NEED_AT_30_MIN - SLEEP_NEED_PER_YEAR_MIN * (age - 30.0),
        respiratory_rate=RESP_AT_30 + RESP_PER_YEAR * (age - 30.0),
        weight_kg=weight_kg + rng.gauss(0.0, 1.5),
    )
