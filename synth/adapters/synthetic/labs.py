"""Редкие лабораторные точки с неравными разрывами (паттерн P-07).

Ключевая деталь: забор дня 63 попадает внутрь окна острой болезни, и ферритин
в нём ведёт себя физиологически верно — как реактант острой фазы он растёт.
Значение не ошибочно; оно просто в этот день измеряет не то, что в остальные.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .events import ILLNESS_ACUTE_DAYS

# Неравные разрывы: 21, 27, 12, 25, 21 день.
LAB_DAYS = (3, 24, 51, 63, 88, 109)

# Ферритин: истинное снижение запасов железа за период.
FERRITIN_START = 82.0
FERRITIN_END = 41.0
FERRITIN_INFLAMED_MULT = 2.05     # подъём как реактанта острой фазы
FERRITIN_ASSAY_CV = 0.03

CRP_BASELINE = 1.05
CRP_ASSAY_CV = 0.20
CRP_INFLAMED = 18.2

HEMOGLOBIN_START, HEMOGLOBIN_END, HEMOGLOBIN_SD = 149.0, 138.0, 1.5
VITAMIN_D_START, VITAMIN_D_END, VITAMIN_D_SD = 28.0, 22.0, 0.8

# Намеренно плоские маркёры: на них проверяется, не выдумает ли аналитика
# тренд там, где его нет.
HBA1C_LEVEL, HBA1C_SD = 5.35, 0.035
TSH_LEVEL, TSH_SD = 2.05, 0.13
TSH_INFLAMED = 2.8


@dataclass(frozen=True)
class LabPanel:
    day: int
    values: dict[str, float]
    inflamed: bool


def _lerp(start: float, end: float, day: int, total_days: int) -> float:
    return start + (end - start) * (day / (total_days - 1))


def draw_panels(total_days: int, rng: random.Random) -> list[LabPanel]:
    """Собрать 6 лабораторных панелей на датах LAB_DAYS."""
    panels: list[LabPanel] = []
    for day in LAB_DAYS:
        if day >= total_days:
            continue
        inflamed = day in ILLNESS_ACUTE_DAYS

        ferritin_true = _lerp(FERRITIN_START, FERRITIN_END, day, total_days)
        ferritin = ferritin_true * (FERRITIN_INFLAMED_MULT if inflamed else 1.0)
        ferritin *= 1.0 + rng.gauss(0.0, FERRITIN_ASSAY_CV)

        crp = CRP_INFLAMED if inflamed else CRP_BASELINE
        crp *= 1.0 + rng.gauss(0.0, CRP_ASSAY_CV)

        values = {
            "lab.ferritin": round(ferritin, 1),
            "lab.crp": round(max(crp, 0.1), 2),
            "lab.hemoglobin": round(
                _lerp(HEMOGLOBIN_START, HEMOGLOBIN_END, day, total_days)
                + rng.gauss(0.0, HEMOGLOBIN_SD), 0),
            "lab.vitamin_d": round(
                _lerp(VITAMIN_D_START, VITAMIN_D_END, day, total_days)
                + rng.gauss(0.0, VITAMIN_D_SD), 1),
            "lab.hba1c": round(HBA1C_LEVEL + rng.gauss(0.0, HBA1C_SD), 2),
            "lab.tsh": round(
                (TSH_INFLAMED if inflamed else TSH_LEVEL) + rng.gauss(0.0, TSH_SD), 2),
        }
        panels.append(LabPanel(day=day, values=values, inflamed=inflamed))
    return panels


def ferritin_true_at(day: int, total_days: int) -> float:
    """Истинный ферритин без воспалительного искажения — для ground_truth.json."""
    return _lerp(FERRITIN_START, FERRITIN_END, day, total_days)
