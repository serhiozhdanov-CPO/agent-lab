#!/usr/bin/env python3
"""Генератор синтетических данных о здоровье и режиме.

Собирает 16 недель суточных записей плюс лабораторные точки в формате,
описанном в data-format.md, и закладывает в них паттерны из expected-patterns.md.

Только стандартная библиотека. Детерминирован по --seed.

    python3 health-synth/generate.py --age 38 --seed 42 --self-check
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sys
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta

SCHEMA_VERSION = "1.0"

# --------------------------------------------------------------------------
# Календарь паттернов. Дни нумеруются с 1; день 1 — понедельник 2026-01-05.
# Все окна должны совпадать с таблицей в expected-patterns.md.
# --------------------------------------------------------------------------

P1_BASELINE = (1, 21)        # стабильный базовый период
P2_TRIP = (29, 34)           # командировка, tz +08:00
P2_RETURN_DAY = 35           # день обратного перелёта, tz снова +03:00
P2_TAIL = (36, 39)           # хвост адаптации после возврата
P3_STEADY = (50, 70)         # период устойчивого ритма
P4_BINGE = (81, 84)          # срыв режима
P4_TAIL = (85, 90)           # хвост восстановления
P7_BATTERY = (44, 48)        # разряженная батарея кольца
P7_FORCED_NOT_WORN = (82, 84)  # две ночи срыва без данных

WHOOP_WINDOW = (36, 70)      # период, когда параллельно надет WHOOP
TRAINING_START = 36          # начало тренировочного блока (P6a)
LAB_DAYS = (3, 26, 27, 61, 88, 110)

HOME_TZ = "+03:00"
TRIP_TZ = "+08:00"

# Коэффициенты связи P5: поздний отход ко сну → метрики следующего дня.
P5_HRV_PER_HOUR = -0.085     # доля от базы на каждый час позднего отхода
P5_RHR_PER_HOUR = 2.2        # bpm на каждый час

P6A_TOTAL_RHR_DRIFT = -3.0   # bpm за весь период на фоне тренировочного блока

AR1_PHI = 0.45               # автокорреляция шума

# Систематическое смещение WHOOP относительно кольца.
WHOOP_HRV_BIAS = 1.06
WHOOP_RHR_BIAS = -1.0
WHOOP_SLEEP_BIAS = 12.0

APPLE_DOUBLE_COUNT_MAX = 0.30  # пик двойного учёта шагов к концу окна WHOOP


# --------------------------------------------------------------------------
# Базовые линии от возраста
# --------------------------------------------------------------------------

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class Baselines:
    """Персональные базовые линии, посчитанные от возраста.

    Формулы правдоподобны по форме и по направлению возрастной динамики,
    но клинически не валидированы — это генератор синтетики, а не норматив.
    """

    age: int
    hr_resting: float
    hrv_rmssd: float
    sleep_need_min: float
    max_hr: float
    respiratory_rate: float

    @classmethod
    def for_age(cls, age: int) -> "Baselines":
        return cls(
            age=age,
            # Пульс покоя медленно растёт с возрастом.
            hr_resting=clamp(48.0 + 0.18 * (age - 20), 45.0, 75.0),
            # Вариабельность падает экспоненциально: ~57 мс в 25, ~45 в 38, ~30 в 55.
            hrv_rmssd=clamp(62.0 * math.exp(-0.018 * (age - 20)), 12.0, 90.0),
            # Потребность во сне слегка снижается.
            sleep_need_min=(8.0 - 0.008 * (age - 20)) * 60.0,
            # Формула Tanaka — нужна для шкалы нагрузки.
            max_hr=208.0 - 0.7 * age,
            respiratory_rate=clamp(13.6 + 0.02 * (age - 20), 12.0, 17.0),
        )


# --------------------------------------------------------------------------
# Шум
# --------------------------------------------------------------------------

class AR1:
    """Автокоррелированный шум первого порядка.

    Белый шум усредняется, и любой заложенный тренд читается однозначно.
    AR(1) создаёт правдоподобные «волны» на 3-5 дней, из-за которых часть
    трендов выглядит убедительнее, чем они есть, — ровно то, что нужно,
    чтобы аналитику пришлось работать, а не считывать ответ с графика.
    """

    def __init__(self, rng: random.Random, sd: float, phi: float = AR1_PHI):
        self.rng = rng
        self.sd = sd
        self.phi = phi
        self.state = rng.gauss(0.0, sd)
        self.innovation_sd = sd * math.sqrt(1.0 - phi * phi)

    def next(self) -> float:
        self.state = self.phi * self.state + self.rng.gauss(0.0, self.innovation_sd)
        return self.state


# --------------------------------------------------------------------------
# Запись
# --------------------------------------------------------------------------

FIELDS = [
    "record_id", "date", "metric", "value", "unit", "source", "method",
    "period_start", "period_end", "tz_offset", "quality", "source_version",
    "original_metric", "ingested_at",
]


@dataclass
class Record:
    date: str
    metric: str
    value: float
    unit: str
    source: str
    method: str
    period_start: str
    period_end: str
    tz_offset: str
    quality: str
    source_version: str
    original_metric: str
    ingested_at: str
    record_id: str = field(default="", init=False)

    def __post_init__(self) -> None:
        key = f"{self.date}|{self.metric}|{self.source}".encode("utf-8")
        self.record_id = hashlib.blake2b(key, digest_size=8).hexdigest()

    def as_row(self) -> dict:
        data = asdict(self)
        return {name: data[name] for name in FIELDS}


# --------------------------------------------------------------------------
# Состояние дня
# --------------------------------------------------------------------------

@dataclass
class Day:
    index: int                 # 1-based
    date: date
    dow: int                   # 0 = понедельник
    tz_offset: str

    sleep_onset: float = 0.0   # минуты от локальной полуночи, со знаком
    sleep_duration: float = 0.0
    sleep_efficiency: float = 0.0
    sleep_deep: float = 0.0
    sleep_rem: float = 0.0
    hr_resting: float = 0.0
    hrv_rmssd: float = 0.0
    respiratory_rate: float = 0.0
    body_temp_delta: float = 0.0
    steps: float = 0.0
    active_energy: float = 0.0
    activity_load: float = 0.0

    wear_hours: float = 24.0
    ring_alive: bool = True    # False = разряжена батарея, строк нет вообще
    night_tracked: bool = True # False = не надето ночью, ночных метрик нет


def in_window(index: int, window: tuple) -> bool:
    return window[0] <= index <= window[1]


def ramp(index: int, window: tuple) -> float:
    """Позиция внутри окна от 0.0 до 1.0."""
    start, end = window
    if end == start:
        return 1.0
    return (index - start) / (end - start)


# --------------------------------------------------------------------------
# Слой 1-4: расписание сна
# --------------------------------------------------------------------------

ONSET_BASE = -45.0           # 23:15 по локальному времени


def build_schedule(days: list, rng: random.Random, base: Baselines) -> None:
    """Отход ко сну и длительность: база, будни/выходные, оверлеи паттернов."""
    onset_noise = AR1(rng, sd=22.0, phi=0.30)
    dur_noise = AR1(rng, sd=26.0)

    for day in days:
        onset = ONSET_BASE
        duration = base.sleep_need_min

        # Будни/выходные: поздние пятница и суббота, длинный сон в выходные.
        if day.dow in (4, 5):          # пятница, суббота
            onset += 55.0
        if day.dow in (5, 6):          # суббота, воскресенье
            duration += 35.0

        noise_scale = 1.0

        # P2 — командировка. В локальном времени человек ложится почти как дома,
        # весь сдвиг спрятан в tz_offset.
        if in_window(day.index, P2_TRIP):
            phase = ramp(day.index, P2_TRIP)
            onset += 25.0 - 10.0 * phase
            duration -= 90.0 - 65.0 * phase
            noise_scale = 1.6
        elif day.index == P2_RETURN_DAY:
            onset += 40.0
            duration -= 55.0
            noise_scale = 1.6
        elif in_window(day.index, P2_TAIL):
            phase = ramp(day.index, P2_TAIL)
            duration -= 35.0 * (1.0 - phase)
            noise_scale = 1.3

        # P3 — устойчивый ритм: главное здесь не среднее, а низкая дисперсия.
        if in_window(day.index, P3_STEADY):
            noise_scale = 0.55
            duration += 18.0
            if day.dow in (4, 5):
                onset -= 30.0      # даже в пятницу режим держится

        # P4 — срыв: четыре ночи подряд 02:00-03:30 и по пять часов сна.
        if in_window(day.index, P4_BINGE):
            phase = ramp(day.index, P4_BINGE)
            onset += 165.0 + 45.0 * phase
            duration -= 105.0
            noise_scale = 1.4
        elif in_window(day.index, P4_TAIL):
            # Режим сна восстанавливается сразу — в отличие от метрик.
            duration += 12.0

        day.sleep_onset = onset + onset_noise.next() * noise_scale
        # Поздняя ночь укорачивает сон: время подъёма гораздо жёстче времени отбоя.
        late = max(0.0, day.sleep_onset - ONSET_BASE)
        day.sleep_duration = duration - 0.32 * late + dur_noise.next() * noise_scale


# --------------------------------------------------------------------------
# Слой 5-6: метрики восстановления, связь lag-1 и медленный дрейф
# --------------------------------------------------------------------------

def build_recovery(days: list, rng: random.Random, base: Baselines, total_days: int) -> None:
    hrv_noise = AR1(rng, sd=0.065)      # доля от базы
    rhr_noise = AR1(rng, sd=1.65)
    eff_noise = AR1(rng, sd=2.1)
    resp_noise = AR1(rng, sd=0.38)
    temp_noise = AR1(rng, sd=0.11)
    deep_noise = AR1(rng, sd=9.0)
    rem_noise = AR1(rng, sd=11.0)

    for i, day in enumerate(days):
        # P5 — связь с лагом в сутки: метрики читают ВЧЕРАШНИЙ отход ко сну.
        prev_dev_hours = 0.0
        if i > 0:
            prev_dev_hours = (days[i - 1].sleep_onset - ONSET_BASE) / 60.0

        hrv_mult = 1.0 + P5_HRV_PER_HOUR * prev_dev_hours
        rhr_add = P5_RHR_PER_HOUR * prev_dev_hours
        resp_add = 0.0
        temp_add = 0.0
        eff = 88.5

        # P6a — настоящий медленный тренд на фоне тренировочного блока.
        if day.index >= TRAINING_START:
            progress = (day.index - TRAINING_START) / max(1, total_days - TRAINING_START)
            rhr_add += P6A_TOTAL_RHR_DRIFT * progress

        # P2 — командировка: провал на восток глубже и длиннее, возврат легче.
        if in_window(day.index, P2_TRIP):
            phase = ramp(day.index, P2_TRIP)
            hrv_mult *= 1.0 - (0.32 - 0.16 * phase)
            rhr_add += 5.5 - 3.5 * phase
            resp_add += 0.6 * (1.0 - phase)
            temp_add += 0.15 * (1.0 - phase)
            eff -= 5.0 * (1.0 - phase)
        elif day.index == P2_RETURN_DAY:
            hrv_mult *= 0.90
            rhr_add += 3.0
            eff -= 3.0
        elif in_window(day.index, P2_TAIL):
            phase = ramp(day.index, P2_TAIL)
            hrv_mult *= 1.0 - 0.12 * (1.0 - phase)
            rhr_add += 3.0 * (1.0 - phase)

        # P3 — устойчивый ритм: рост примерно 6% неделя к неделе.
        if in_window(day.index, P3_STEADY):
            week = (day.index - P3_STEADY[0]) // 7
            # Прибавка намеренно скромная: низкая дисперсия отхода ко сну сама
            # по себе снимает штраф P5, и это уже поднимает HRV примерно на 3%.
            hrv_mult *= 1.0 + 0.025 * (week + 1)
            rhr_add -= 0.6 * (week + 1)
            eff += 4.0

        # P4 — срыв и асимметричное восстановление.
        if in_window(day.index, P4_BINGE):
            # Оверлей плоский: углубление от ночи к ночи создаёт сама
            # связь P5, которая читает всё более поздний отход ко сну накануне.
            # Складывать сюда ещё и ramp значило бы посчитать эффект дважды.
            hrv_mult *= 1.0 - 0.30
            rhr_add += 6.5
            resp_add += 1.5
            temp_add += 0.40
            eff -= 12.0
        elif in_window(day.index, P4_TAIL):
            offset = day.index - P4_BINGE[1]          # 1..6
            # Пульс покоя возвращается за двое суток...
            rhr_add += max(0.0, 4.0 * (1.0 - offset / 2.5))
            # ...а вариабельность за пять-шесть.
            hrv_mult *= 1.0 - 0.26 * max(0.0, 1.0 - offset / 7.0)
            resp_add += 0.5 * max(0.0, 1.0 - offset / 3.0)

        day.hrv_rmssd = base.hrv_rmssd * hrv_mult * (1.0 + hrv_noise.next())
        day.hr_resting = base.hr_resting + rhr_add + rhr_noise.next()
        day.respiratory_rate = base.respiratory_rate + resp_add + resp_noise.next()
        day.body_temp_delta = temp_add + temp_noise.next()
        day.sleep_efficiency = clamp(eff + eff_noise.next(), 40.0, 100.0)

        deep_share = 0.19
        rem_share = 0.22
        if in_window(day.index, P4_BINGE):
            deep_share *= 0.60
        day.sleep_deep = max(0.0, day.sleep_duration * deep_share + deep_noise.next())
        day.sleep_rem = max(0.0, day.sleep_duration * rem_share + rem_noise.next())


# --------------------------------------------------------------------------
# Слой активности
# --------------------------------------------------------------------------

def build_activity(days: list, rng: random.Random, base: Baselines, total_days: int) -> None:
    steps_noise = AR1(rng, sd=1250.0)
    load_noise = AR1(rng, sd=1.6)
    energy_noise = AR1(rng, sd=60.0)

    for day in days:
        steps = 8600.0
        if day.dow == 5:
            steps *= 1.10          # суббота — прогулки
        elif day.dow == 6:
            steps *= 0.75          # воскресенье — дома

        load = 8.0
        if day.dow in (5, 6):
            load += 1.5 if day.dow == 5 else -2.0

        # P6a — тренировочный блок поднимает нагрузку, но НЕ шаги.
        # Шаги остаются без тренда намеренно: весь их видимый рост в данных
        # создаётся только двойным учётом источников (P6b).
        if day.index >= TRAINING_START:
            progress = (day.index - TRAINING_START) / max(1, total_days - TRAINING_START)
            load += 5.0 * progress

        if in_window(day.index, P2_TRIP) or day.index == P2_RETURN_DAY:
            if day.index in (P2_TRIP[0], P2_RETURN_DAY):
                steps *= 1.40      # аэропорты и пересадки
            load *= 0.75
        if in_window(day.index, P4_BINGE):
            steps *= 1.20          # активная социальная жизнь, а не постельный режим
            load *= 0.85

        day.steps = max(0.0, steps + steps_noise.next())
        day.activity_load = clamp(load + load_noise.next(), 0.0, 21.0)
        day.active_energy = max(
            0.0, 0.045 * day.steps + 22.0 * day.activity_load + energy_noise.next()
        )


# --------------------------------------------------------------------------
# P7 — пропуски
# --------------------------------------------------------------------------

def apply_missingness(days: list, rng: random.Random) -> dict:
    """Три механизма пропусков, каждый со своим следом в данных."""
    log = {"battery_dead": [], "not_worn": [], "partial": []}

    for day in days:
        # Разряженная батарея: непрерывный блок, ни одной строки от кольца.
        if in_window(day.index, P7_BATTERY):
            day.ring_alive = False
            day.night_tracked = False
            log["battery_dead"].append(day.date.isoformat())
            continue

        # Пропуски, скоррелированные со срывом: человек перестаёт себя мерить
        # именно тогда, когда всё плохо. Две ночи из четырёх фиксированы.
        if day.index in range(P7_FORCED_NOT_WORN[0], P7_FORCED_NOT_WORN[1] + 1, 2):
            day.night_tracked = False
            day.wear_hours = round(rng.uniform(0.5, 2.5), 1)
            log["not_worn"].append(day.date.isoformat())
            continue

        # Остальные ночи срыва защищены от случайных пропусков: иначе глубину
        # провала не по чему оценивать, а answer-key обещает ровно две дыры.
        if in_window(day.index, P4_BINGE):
            day.wear_hours = round(rng.uniform(20.0, 24.0), 1)
            continue

        # В базовом периоде и в устойчивом ритме пропусков почти нет —
        # иначе P1 перестаёт быть контрольной группой, а P3 эталоном.
        if in_window(day.index, P3_STEADY):
            day.wear_hours = round(rng.uniform(22.0, 24.0), 1)
            continue
        p_not_worn = 0.015 if in_window(day.index, P1_BASELINE) else 0.04
        p_partial = 0.015 if in_window(day.index, P1_BASELINE) else 0.06

        roll = rng.random()
        if roll < p_not_worn:
            day.night_tracked = False
            day.wear_hours = round(rng.uniform(0.0, 2.5), 1)
            log["not_worn"].append(day.date.isoformat())
        elif roll < p_not_worn + p_partial:
            day.night_tracked = False
            day.wear_hours = round(rng.uniform(3.0, 6.0), 1)
            log["partial"].append(day.date.isoformat())
        else:
            day.wear_hours = round(rng.uniform(21.0, 24.0), 1)

    return log


# --------------------------------------------------------------------------
# Эмиссия записей
# --------------------------------------------------------------------------

def stamp(day: Day, minutes_from_midnight: float) -> str:
    moment = datetime.combine(day.date, datetime.min.time()) + timedelta(
        minutes=minutes_from_midnight
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + day.tz_offset


def ingest_stamp(day: Day, hour: int, lag_days: int = 0) -> str:
    moment = datetime.combine(day.date, datetime.min.time()) + timedelta(
        days=lag_days, hours=hour
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


RING_VERSION = "sber_ring/2.4.1"
APPLE_VERSION = "ios/18.3"
WHOOP_VERSION = "whoop/4.9.2"
LAB_VERSION = "lab_lis/3.2"


def emit_ring(day: Day, records: list) -> None:
    if not day.ring_alive:
        return

    night_start = day.sleep_onset
    night_end = day.sleep_onset + day.sleep_duration
    day_start, day_end = 0.0, 24 * 60 - 1

    def night(metric, value, unit, method, original, quality="ok"):
        records.append(Record(
            date=day.date.isoformat(), metric=metric, value=value, unit=unit,
            source="sber_ring", method=method,
            period_start=stamp(day, night_start), period_end=stamp(day, night_end),
            tz_offset=day.tz_offset, quality=quality, source_version=RING_VERSION,
            original_metric=original, ingested_at=ingest_stamp(day, 9),
        ))

    def daily(metric, value, unit, method, original, quality="ok"):
        records.append(Record(
            date=day.date.isoformat(), metric=metric, value=value, unit=unit,
            source="sber_ring", method=method,
            period_start=stamp(day, day_start), period_end=stamp(day, day_end),
            tz_offset=day.tz_offset, quality=quality, source_version=RING_VERSION,
            original_metric=original, ingested_at=ingest_stamp(day, 23),
        ))

    # wear_hours пишется всегда, пока кольцо живо, — именно он позволяет
    # отличить «не носил» от «устройство молчало».
    daily("wear_hours", day.wear_hours, "h", "device_measured", "sber.device.wear_hours")

    if day.night_tracked:
        night("hr_resting", round(day.hr_resting), "bpm", "device_measured",
              "sber.sleep.hr_resting_bpm")
        night("hrv_rmssd", round(day.hrv_rmssd), "ms", "device_measured",
              "sber.sleep.hrv_rmssd_ms")
        night("respiratory_rate", round(day.respiratory_rate, 1), "brpm",
              "device_measured", "sber.sleep.resp_rate_brpm")
        night("body_temp_delta", round(day.body_temp_delta, 2), "C",
              "device_measured", "sber.sleep.temp_delta_c")
        night("sleep_onset", round(day.sleep_onset), "min", "device_derived",
              "sber.sleep.onset_local")
        night("sleep_duration", round(day.sleep_duration), "min", "device_derived",
              "sber.sleep.total_min")
        night("sleep_efficiency", round(day.sleep_efficiency, 1), "pct",
              "device_derived", "sber.sleep.efficiency_pct")
        night("sleep_deep", round(day.sleep_deep), "min", "device_derived",
              "sber.sleep.deep_min")
        night("sleep_rem", round(day.sleep_rem), "min", "device_derived",
              "sber.sleep.rem_min")

    if day.wear_hours >= 3.0:
        # Шаги с кольца остаются даже при частичном ношении — и служат
        # контролем, на фоне которого виден артефакт двойного учёта в Apple.
        daily("steps", round(day.steps * 0.97), "count", "device_measured",
              "sber.activity.steps",
              quality="ok" if day.wear_hours >= 18 else "low_confidence")
        daily("activity_load", round(day.activity_load * 0.88, 1), "score",
              "device_derived", "sber.activity.load_score")


def emit_apple(day: Day, records: list, rng: random.Random, ring_sleep: float) -> None:
    day_start, day_end = 0.0, 24 * 60 - 1

    # P6b — ловушка. С подключением WHOOP его iOS-приложение пишет данные
    # обратно в Apple Health, и часть шагов начинает учитываться дважды.
    # Роста активности за этим «трендом» нет, и он обрывается ступенькой
    # ровно тогда, когда второй источник исчезает.
    inflation = 1.0
    if in_window(day.index, WHOOP_WINDOW):
        inflation += APPLE_DOUBLE_COUNT_MAX * ramp(day.index, WHOOP_WINDOW)

    steps = round(day.steps * inflation + rng.gauss(0, 120))
    records.append(Record(
        date=day.date.isoformat(), metric="steps", value=max(0, steps), unit="count",
        source="apple_health", method="app_aggregated",
        period_start=stamp(day, day_start), period_end=stamp(day, day_end),
        tz_offset=day.tz_offset,
        quality="low_confidence" if inflation > 1.0 else "ok",
        source_version=APPLE_VERSION,
        original_metric="HKQuantityTypeIdentifierStepCount",
        ingested_at=ingest_stamp(day, 23),
    ))

    records.append(Record(
        date=day.date.isoformat(), metric="active_energy",
        value=round(day.active_energy + rng.gauss(0, 18)), unit="kcal",
        source="apple_health", method="app_aggregated",
        period_start=stamp(day, day_start), period_end=stamp(day, day_end),
        tz_offset=day.tz_offset, quality="ok", source_version=APPLE_VERSION,
        original_metric="HKQuantityTypeIdentifierActiveEnergyBurned",
        ingested_at=ingest_stamp(day, 23),
    ))

    # Зеркало WHOOP: та же ночь приезжает во второй раз с чужим source.
    if in_window(day.index, WHOOP_WINDOW) and ring_sleep:
        mirrored = round(ring_sleep + WHOOP_SLEEP_BIAS + rng.gauss(0, 6))
        records.append(Record(
            date=day.date.isoformat(), metric="sleep_duration", value=mirrored,
            unit="min", source="apple_health", method="app_aggregated",
            period_start=stamp(day, day.sleep_onset),
            period_end=stamp(day, day.sleep_onset + mirrored),
            tz_offset=day.tz_offset, quality="low_confidence",
            source_version=APPLE_VERSION,
            original_metric="HKCategoryValueSleepAnalysisAsleepUnspecified",
            ingested_at=ingest_stamp(day, 10),
        ))


def emit_whoop(day: Day, records: list, rng: random.Random) -> None:
    if not in_window(day.index, WHOOP_WINDOW):
        return

    night_start = day.sleep_onset
    night_end = day.sleep_onset + day.sleep_duration

    def night(metric, value, unit, method, original):
        records.append(Record(
            date=day.date.isoformat(), metric=metric, value=value, unit=unit,
            source="whoop", method=method,
            period_start=stamp(day, night_start), period_end=stamp(day, night_end),
            tz_offset=day.tz_offset, quality="ok", source_version=WHOOP_VERSION,
            original_metric=original, ingested_at=ingest_stamp(day, 8),
        ))

    # WHOOP надет и в те дни, когда кольцо разряжено или снято, — но читает
    # систематически иначе. Склейка двух серий без поправки на это смещение
    # даёт ступеньку, которая выглядит как физиология.
    night("hrv_rmssd", round(day.hrv_rmssd * WHOOP_HRV_BIAS + rng.gauss(0, 1.5)),
          "ms", "device_measured", "whoop.recovery.hrv_rmssd_milli")
    night("hr_resting", round(day.hr_resting + WHOOP_RHR_BIAS + rng.gauss(0, 0.8)),
          "bpm", "device_measured", "whoop.recovery.resting_heart_rate")
    night("sleep_duration",
          round(day.sleep_duration + WHOOP_SLEEP_BIAS + rng.gauss(0, 7)),
          "min", "device_derived", "whoop.sleep.stage_summary")
    night("sleep_efficiency",
          round(clamp(day.sleep_efficiency + rng.gauss(0, 1.2), 40, 100), 1),
          "pct", "device_derived", "whoop.sleep.sleep_efficiency_percentage")

    records.append(Record(
        date=day.date.isoformat(), metric="activity_load",
        value=round(clamp(day.activity_load + rng.gauss(0, 0.5), 0, 21), 1),
        unit="score", source="whoop", method="device_derived",
        period_start=stamp(day, 0), period_end=stamp(day, 24 * 60 - 1),
        tz_offset=day.tz_offset, quality="ok", source_version=WHOOP_VERSION,
        original_metric="whoop.cycle.score.strain",
        ingested_at=ingest_stamp(day, 23),
    ))


# --------------------------------------------------------------------------
# Лабораторные точки
# --------------------------------------------------------------------------

LAB_UNITS = {
    "hs_crp": "mg/L", "ferritin": "ng/mL", "vitamin_d_25oh": "ng/mL",
    "hba1c": "pct", "tsh": "mIU/L", "cortisol_morning": "nmol/L",
}

# Панели намеренно разные: точки 26 и 27 — не дубль, а досдача анализов.
LAB_PANELS = {
    3:   ["hs_crp", "ferritin", "vitamin_d_25oh", "hba1c", "tsh"],
    26:  ["hs_crp", "vitamin_d_25oh", "hba1c", "tsh"],
    27:  ["ferritin", "cortisol_morning"],
    61:  ["hs_crp", "ferritin", "vitamin_d_25oh", "hba1c", "tsh", "cortisol_morning"],
    88:  ["hs_crp", "ferritin", "vitamin_d_25oh", "hba1c", "tsh", "cortisol_morning"],
    110: ["hs_crp", "ferritin", "vitamin_d_25oh", "hba1c", "tsh", "cortisol_morning"],
}


def lab_value(metric: str, day_index: int, rng: random.Random) -> float:
    """Лабораторные значения с сезонным трендом и откликом на срыв.

    Забор на дне 88 — через неделю после срыва: воспалительный маркёр вверх,
    ферритин вниз, утренний кортизол вверх. HbA1c держится плоским — это
    негативный контроль, метрика, которая обязана НЕ отреагировать.
    """
    post_binge = day_index == 88

    if metric == "hs_crp":
        base = 2.8 if post_binge else 0.6
        return round(max(0.1, base + rng.gauss(0, 0.12)), 2)
    if metric == "ferritin":
        base = 92.0 if post_binge else 128.0
        return round(max(5.0, base + rng.gauss(0, 7.0)), 1)
    if metric == "vitamin_d_25oh":
        # Медленное зимнее снижение, не связанное ни с одним паттерном.
        return round(max(5.0, 31.0 - 0.045 * day_index + rng.gauss(0, 1.1)), 1)
    if metric == "hba1c":
        return round(5.3 + rng.gauss(0, 0.05), 2)
    if metric == "tsh":
        return round(max(0.3, 2.1 + rng.gauss(0, 0.22)), 2)
    if metric == "cortisol_morning":
        base = 505.0 if post_binge else 380.0
        return round(max(100.0, base + rng.gauss(0, 28.0)), 1)
    raise ValueError(f"неизвестная лабораторная метрика: {metric}")


def emit_lab(days: list, records: list, rng: random.Random) -> None:
    by_index = {day.index: day for day in days}
    for day_index in LAB_DAYS:
        day = by_index.get(day_index)
        if day is None:
            continue
        for metric in LAB_PANELS[day_index]:
            records.append(Record(
                date=day.date.isoformat(), metric=metric,
                value=lab_value(metric, day_index, rng), unit=LAB_UNITS[metric],
                source="lab", method="lab_assay",
                period_start=stamp(day, 8 * 60 + 15),
                period_end=stamp(day, 8 * 60 + 15),
                tz_offset=day.tz_offset, quality="ok", source_version=LAB_VERSION,
                original_metric=f"lis.panel.{metric}",
                # Результат приходит через двое суток после забора.
                ingested_at=ingest_stamp(day, 14, lag_days=2),
            ))


# --------------------------------------------------------------------------
# Сборка
# --------------------------------------------------------------------------

def build_days(start: date, total_days: int) -> list:
    days = []
    for i in range(total_days):
        current = start + timedelta(days=i)
        index = i + 1
        tz = TRIP_TZ if in_window(index, P2_TRIP) else HOME_TZ
        days.append(Day(index=index, date=current, dow=current.weekday(), tz_offset=tz))
    return days


def generate(age: int, seed: int, start: date, weeks: int) -> tuple:
    total_days = weeks * 7
    base = Baselines.for_age(age)
    days = build_days(start, total_days)

    # Отдельный генератор на каждый слой: правка одного слоя не сдвигает
    # последовательность случайных чисел в остальных.
    rng_schedule = random.Random(seed * 7919 + 1)
    rng_recovery = random.Random(seed * 7919 + 2)
    rng_activity = random.Random(seed * 7919 + 3)
    rng_missing = random.Random(seed * 7919 + 4)
    rng_apple = random.Random(seed * 7919 + 5)
    rng_whoop = random.Random(seed * 7919 + 6)
    rng_lab = random.Random(seed * 7919 + 7)

    build_schedule(days, rng_schedule, base)
    build_recovery(days, rng_recovery, base, total_days)
    build_activity(days, rng_activity, base, total_days)
    missing_log = apply_missingness(days, rng_missing)

    records: list = []
    for day in days:
        emit_ring(day, records)
        ring_sleep = day.sleep_duration if (day.ring_alive and day.night_tracked) else 0.0
        emit_apple(day, records, rng_apple, ring_sleep)
        emit_whoop(day, records, rng_whoop)
    emit_lab(days, records, rng_lab)

    records.sort(key=lambda r: (r.date, r.metric, r.source))
    return days, records, base, missing_log


# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------

def write_csv(path, records: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(record.as_row())


def write_jsonl(path, records: list) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.as_row(), ensure_ascii=False) + "\n")


def window_dates(days: list, window: tuple) -> dict:
    by_index = {day.index: day for day in days}
    return {
        "days": list(window),
        "from": by_index[window[0]].date.isoformat(),
        "to": by_index[window[1]].date.isoformat(),
    }


def build_answer_key(days, records, base, missing_log, args, checks) -> dict:
    metric_counts: dict = {}
    source_counts: dict = {}
    for record in records:
        metric_counts[record.metric] = metric_counts.get(record.metric, 0) + 1
        source_counts[record.source] = source_counts.get(record.source, 0) + 1

    by_index = {day.index: day for day in days}
    return {
        "_warning": (
            "Это эталонные ответы. Не показывайте этот файл агенту, который "
            "анализирует records.csv — иначе эксперимент ничего не измеряет."
        ),
        "schema_version": SCHEMA_VERSION,
        "params": {
            "age": args.age, "seed": args.seed,
            "start_date": args.start_date, "weeks": args.weeks,
            "total_days": len(days),
            "end_date": days[-1].date.isoformat(),
            "home_tz": HOME_TZ,
        },
        "baselines_from_age": {
            "hr_resting_bpm": round(base.hr_resting, 2),
            "hrv_rmssd_ms": round(base.hrv_rmssd, 2),
            "sleep_need_min": round(base.sleep_need_min, 1),
            "max_hr_bpm": round(base.max_hr, 1),
            "respiratory_rate_brpm": round(base.respiratory_rate, 2),
            "sleep_onset_base_min": ONSET_BASE,
        },
        "patterns": {
            "P1_baseline": window_dates(days, P1_BASELINE),
            "P2_trip": {
                **window_dates(days, P2_TRIP),
                "tz_from": HOME_TZ, "tz_to": TRIP_TZ,
                "return_day": P2_RETURN_DAY,
                "return_date": by_index[P2_RETURN_DAY].date.isoformat(),
                "tail": window_dates(days, P2_TAIL),
            },
            "P3_steady": window_dates(days, P3_STEADY),
            "P4_binge": {
                **window_dates(days, P4_BINGE),
                "tail": window_dates(days, P4_TAIL),
                "recovery_days_rhr": 2, "recovery_days_hrv": 6,
            },
            "P5_lag1_coupling": {
                "lag_days": 1,
                "hrv_pct_per_hour_late": P5_HRV_PER_HOUR * 100,
                "rhr_bpm_per_hour_late": P5_RHR_PER_HOUR,
                "exclude_windows": [list(P2_TRIP), list(P2_TAIL),
                                    list(P4_BINGE), list(P4_TAIL)],
            },
            "P6a_real_trend": {
                "metric": "hr_resting",
                "total_change_bpm": P6A_TOTAL_RHR_DRIFT,
                "driver": "activity_load",
                "starts_day": TRAINING_START,
            },
            "P6b_decoy_trend": {
                "metric": "steps", "source": "apple_health",
                "cause": "двойной учёт при зеркалировании WHOOP в Apple Health",
                **window_dates(days, WHOOP_WINDOW),
                "max_inflation": APPLE_DOUBLE_COUNT_MAX,
                "control_source": "sber_ring",
            },
            "P7_missingness": {
                "battery_dead": window_dates(days, P7_BATTERY),
                "forced_not_worn_during_binge":
                    [by_index[i].date.isoformat()
                     for i in range(P7_FORCED_NOT_WORN[0], P7_FORCED_NOT_WORN[1] + 1, 2)],
                "mechanism": "MNAR — вероятность пропуска растёт в дни срыва",
                "log": missing_log,
            },
        },
        "sources": {
            "whoop_window": window_dates(days, WHOOP_WINDOW),
            "whoop_bias_vs_ring": {
                "hrv_rmssd_mult": WHOOP_HRV_BIAS,
                "hr_resting_add_bpm": WHOOP_RHR_BIAS,
                "sleep_duration_add_min": WHOOP_SLEEP_BIAS,
            },
        },
        "lab_points": [
            {"day": d, "date": by_index[d].date.isoformat(), "panel": LAB_PANELS[d]}
            for d in LAB_DAYS if d in by_index
        ],
        "counts": {
            "records_total": len(records),
            "by_metric": dict(sorted(metric_counts.items())),
            "by_source": dict(sorted(source_counts.items())),
        },
        "self_check": checks,
    }


# --------------------------------------------------------------------------
# Самопроверка
# --------------------------------------------------------------------------

def mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else float("nan")


def stdev(values) -> float:
    values = list(values)
    if len(values) < 2:
        return float("nan")
    mu = mean(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / (len(values) - 1))


def pearson(xs, ys) -> float:
    xs, ys = list(xs), list(ys)
    if len(xs) < 3:
        return float("nan")
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den else float("nan")


def ols_slope(xs, ys) -> float:
    xs, ys = list(xs), list(ys)
    mx, my = mean(xs), mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if not den:
        return float("nan")
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def series(records, metric: str, source: str) -> dict:
    """Ряд «дата → значение» так, как его увидит аналитика: только по записям."""
    return {r.date: r.value for r in records if r.metric == metric and r.source == source}


def self_check(days, records, base) -> tuple:
    """Проверяет, что заложенные паттерны действительно видны в выданных данных.

    Нужна потому, что паттерн легко утопить в шуме: без этой проверки можно
    молча отдать датасет, в котором искать нечего.
    """
    by_date = {day.date.isoformat(): day for day in days}
    index_of = {day.date.isoformat(): day.index for day in days}
    dates_by_index = {day.index: day.date.isoformat() for day in days}

    hrv = series(records, "hrv_rmssd", "sber_ring")
    rhr = series(records, "hr_resting", "sber_ring")
    onset = series(records, "sleep_onset", "sber_ring")
    dur = series(records, "sleep_duration", "sber_ring")
    apple_steps = series(records, "steps", "apple_health")
    ring_steps = series(records, "steps", "sber_ring")

    def window_values(mapping, window):
        return [mapping[dates_by_index[i]] for i in range(window[0], window[1] + 1)
                if dates_by_index.get(i) in mapping]

    def window_mean(mapping, window, minimum=2):
        """Среднее по окну, расширяя его, если ночей не хватило из-за пропусков."""
        start, end = window
        for widen in range(0, 4):
            values = window_values(mapping, (start - widen, end + widen))
            if len(values) >= minimum:
                return mean(values)
        return float("nan")

    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    # P1 — контрольная группа обязана быть спокойной.
    p1_hrv = window_values(hrv, P1_BASELINE)
    p1_rhr = window_values(rhr, P1_BASELINE)
    hrv_cv = stdev(p1_hrv) / mean(p1_hrv)
    check("P1: низкая вариативность HRV в базовом периоде", hrv_cv <= 0.13,
          f"CV = {hrv_cv:.3f}, порог 0.13")
    check("P1: пульс покоя стабилен", stdev(p1_rhr) <= 3.4,
          f"SD = {stdev(p1_rhr):.2f} bpm, порог 3.4")

    hrv_base = mean(p1_hrv)
    rhr_base = mean(p1_rhr)

    # P2 — смена пояса видна в tz_offset и провал глубже, чем на возврате.
    tz_by_index = {day.index: day.tz_offset for day in days}
    tz_ok = (tz_by_index[P2_TRIP[0]] == TRIP_TZ
             and tz_by_index[P2_TRIP[0] - 1] == HOME_TZ
             and tz_by_index[P2_RETURN_DAY] == HOME_TZ)
    check("P2: tz_offset меняется на днях 29 и 35", tz_ok,
          f"день 28 {tz_by_index[P2_TRIP[0]-1]}, день 29 {tz_by_index[P2_TRIP[0]]}, "
          f"день 35 {tz_by_index[P2_RETURN_DAY]}")

    out_drop = mean(window_values(hrv, (29, 32))) / hrv_base - 1.0
    back_drop = mean(window_values(hrv, (36, 38))) / hrv_base - 1.0
    check("P2: провал HRV на выезде не менее 13%", out_drop <= -0.13,
          f"{out_drop * 100:.1f}%")
    check("P2: возврат на запад легче, чем выезд на восток", back_drop > out_drop,
          f"выезд {out_drop * 100:.1f}%, возврат {back_drop * 100:.1f}%")

    # P3 — устойчивый ритм: минимальная дисперсия отхода ко сну за все 16 недель.
    best_start, best_sd = None, float("inf")
    for start_index in range(1, len(days) - 20):
        values = window_values(onset, (start_index, start_index + 20))
        if len(values) < 21:
            continue  # окно с пропусками не может считаться самым стабильным
        sd = stdev(values)
        if sd < best_sd:
            best_sd, best_start = sd, start_index
    overlap_days = 0
    if best_start is not None:
        overlap_days = len(set(range(best_start, best_start + 21))
                           & set(range(P3_STEADY[0], P3_STEADY[1] + 1)))
    check("P3: самое стабильное трёхнедельное окно совпадает с днями 50-70",
          overlap_days >= 16,
          f"найдено окно с дня {best_start}, пересечение {overlap_days} из 21 дня, "
          f"SD отхода ко сну {best_sd:.1f} мин")
    p3_dur = window_values(dur, P3_STEADY)
    long_nights = sum(1 for v in p3_dur if v >= 420)
    check("P3: не менее 19 ночей из 21 длиннее 7 часов", long_nights >= 19,
          f"{long_nights} из {len(p3_dur)}")

    # P4 — глубина срыва и асимметрия восстановления.
    # База берётся локальная, по десяти дням перед срывом: к 81-му дню
    # собственный уровень человека уже сдвинут трендом P6a, и сравнение
    # с январской базой занизило бы глубину провала.
    hrv_local = mean(window_values(hrv, (71, 80)))
    rhr_local = mean(window_values(rhr, (71, 80)))
    binge_hrv = mean(window_values(hrv, P4_BINGE)) / hrv_local - 1.0
    binge_rhr = mean(window_values(rhr, P4_BINGE)) - rhr_local
    check("P4: HRV падает не менее чем на 30%", binge_hrv <= -0.30,
          f"{binge_hrv * 100:.1f}%")
    check("P4: пульс покоя растёт не менее чем на 5 bpm", binge_rhr >= 5.0,
          f"+{binge_rhr:.1f} bpm")

    # Сравниваем трёхдневными средними, а не отдельными днями: одиночная суббота
    # и так сидит ниже базы из-за пятничного позднего отхода ко сну (P5).
    rhr_early = window_mean(rhr, (86, 87)) - rhr_local
    hrv_early = window_mean(hrv, (85, 87)) / hrv_local
    hrv_late = window_mean(hrv, (88, 90)) / hrv_local
    # Продолжение хвоста берём шестью днями: по трём дням недельный ритм
    # сна ещё способен перевесить само восстановление.
    hrv_tail = window_mean(hrv, (88, 93)) / hrv_local
    rhr_recovered = 1.0 - rhr_early / binge_rhr
    hrv_recovered = 1.0 - (1.0 - hrv_early) / (-binge_hrv)
    check("P4: пульс покоя восстанавливается заметно быстрее HRV",
          rhr_recovered - hrv_recovered >= 0.20,
          f"к дню 87 отыграно: пульс покоя {rhr_recovered * 100:.0f}%, "
          f"HRV {hrv_recovered * 100:.0f}% от глубины срыва")
    check("P4: HRV на вторые-третьи сутки ещё не восстановлен", hrv_early <= 0.92,
          f"дни 85-87: {hrv_early * 100:.0f}% от базы")
    check("P4: восстановление HRV продолжается на второй неделе",
          hrv_tail - hrv_early >= 0.05,
          f"дни 85-87 {hrv_early * 100:.0f}% -> дни 88-93 {hrv_tail * 100:.0f}%")

    # P5 — связь lag-1, посчитанная так же, как её должна считать аналитика.
    excluded = set()
    for window in (P2_TRIP, P2_TAIL, P4_BINGE, P4_TAIL, P7_BATTERY):
        excluded.update(range(window[0], window[1] + 1))
    excluded.add(P2_RETURN_DAY)

    lag1_x, lag1_y, lag0_x, lag0_y = [], [], [], []
    for index in range(2, len(days) + 1):
        if index in excluded or (index - 1) in excluded:
            continue
        prev_date = dates_by_index[index - 1]
        today = dates_by_index[index]
        if prev_date in onset and today in hrv:
            lag1_x.append((onset[prev_date] - ONSET_BASE) / 60.0)
            lag1_y.append(hrv[today] / hrv_base - 1.0)
        if today in onset and today in hrv:
            lag0_x.append((onset[today] - ONSET_BASE) / 60.0)
            lag0_y.append(hrv[today] / hrv_base - 1.0)

    r_lag1 = pearson(lag1_x, lag1_y)
    r_lag0 = pearson(lag0_x, lag0_y)
    slope = ols_slope(lag1_x, lag1_y) * 100.0
    check("P5: корреляция lag-1 в диапазоне -0.85..-0.30", -0.85 <= r_lag1 <= -0.30,
          f"r = {r_lag1:.3f} по {len(lag1_x)} дням")
    check("P5: lag-1 сильнее lag-0", abs(r_lag1) > abs(r_lag0),
          f"lag-1 r = {r_lag1:.3f}, lag-0 r = {r_lag0:.3f}")
    check("P5: наклон близок к заложенному", -15.0 <= slope <= -4.0,
          f"{slope:.2f}% на час, заложено {P5_HRV_PER_HOUR * 100:.1f}%")

    # P6a — реальный тренд, P6b — ловушка.
    rhr_points = [(index_of[d], v) for d, v in rhr.items()]
    rhr_points.sort()
    total_drift = ols_slope([p[0] for p in rhr_points],
                            [p[1] for p in rhr_points]) * (len(days) - 1)
    check("P6a: пульс покоя снижается за период", -6.5 <= total_drift <= -1.2,
          f"{total_drift:.2f} bpm за {len(days)} дней, "
          f"заложено {P6A_TOTAL_RHR_DRIFT:.1f}")

    overlap = [dates_by_index[i] for i in range(WHOOP_WINDOW[0], WHOOP_WINDOW[1] + 1)]
    apple_in = [apple_steps[d] for d in overlap if d in apple_steps]
    ring_in = [ring_steps[d] for d in overlap if d in ring_steps]
    ratio = mean(apple_in) / mean(ring_in)
    check("P6b: шаги Apple завышены относительно кольца в окне WHOOP", ratio >= 1.10,
          f"отношение {ratio:.3f}")

    after = [dates_by_index[i] for i in range(WHOOP_WINDOW[1] + 1, len(days) + 1)]
    ratio_after = (mean([apple_steps[d] for d in after if d in apple_steps])
                   / mean([ring_steps[d] for d in after if d in ring_steps]))
    check("P6b: ложный тренд обрывается ступенькой после дня 70",
          ratio_after < ratio - 0.08,
          f"Apple/кольцо: в окне {ratio:.3f}, после {ratio_after:.3f}")

    # P7 — пропуски и их неслучайность.
    battery_dates = {dates_by_index[i]
                     for i in range(P7_BATTERY[0], P7_BATTERY[1] + 1)}
    ring_rows_in_gap = sum(1 for r in records
                           if r.source == "sber_ring" and r.date in battery_dates)
    check("P7: пятидневный блок без единой строки от кольца", ring_rows_in_gap == 0,
          f"{ring_rows_in_gap} строк")

    binge_dates = [dates_by_index[i] for i in range(P4_BINGE[0], P4_BINGE[1] + 1)]
    missing_nights = [d for d in binge_dates if d not in hrv]
    have_wear = all(
        any(r.metric == "wear_hours" and r.date == d and r.source == "sber_ring"
            for r in records)
        for d in missing_nights
    )
    check("P7: две ночи срыва без ночных метрик", len(missing_nights) == 2,
          f"{len(missing_nights)} ночей: {', '.join(missing_nights) or '—'}")
    check("P7: у пропущенных ночей срыва есть wear_hours", have_wear,
          "пропуск объясним через wear_hours")

    summary = {
        "passed": sum(1 for c in checks if c["passed"]),
        "total": len(checks),
        "measured": {
            "p1_hrv_cv": round(hrv_cv, 4),
            "hrv_baseline_ms": round(hrv_base, 2),
            "rhr_baseline_bpm": round(rhr_base, 2),
            "p2_hrv_drop_outbound_pct": round(out_drop * 100, 1),
            "p2_hrv_drop_inbound_pct": round(back_drop * 100, 1),
            "p3_best_window_start_day": best_start,
            "p3_onset_sd_min": round(best_sd, 2),
            "p4_hrv_drop_pct": round(binge_hrv * 100, 1),
            "p4_rhr_rise_bpm": round(binge_rhr, 2),
            "p4_local_hrv_base_ms": round(hrv_local, 2),
            "p4_local_rhr_base_bpm": round(rhr_local, 2),
            "p4_hrv_pct_of_base_d85_87": round(hrv_early * 100, 1),
            "p4_hrv_pct_of_base_d88_90": round(hrv_late * 100, 1),
            "p4_hrv_pct_of_base_d88_93": round(hrv_tail * 100, 1),
            "p4_rhr_delta_d86_87": round(rhr_early, 2),
            "p4_recovered_by_d87_rhr_pct": round(rhr_recovered * 100, 1),
            "p4_recovered_by_d87_hrv_pct": round(hrv_recovered * 100, 1),
            "p5_r_lag1": round(r_lag1, 4),
            "p5_r_lag0": round(r_lag0, 4),
            "p5_slope_pct_per_hour": round(slope, 3),
            "p6a_rhr_total_drift_bpm": round(total_drift, 3),
            "p6b_apple_over_ring_ratio_in_window": round(ratio, 4),
            "p6b_apple_over_ring_ratio_after": round(ratio_after, 4),
        },
    }
    return checks, summary


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Генератор синтетических данных о здоровье и режиме.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--age", type=int, default=38,
                        help="возраст: от него считаются базовые линии пульса покоя и HRV")
    parser.add_argument("--seed", type=int, default=42, help="сид воспроизводимости")
    parser.add_argument("--start-date", default="2026-01-05",
                        help="дата первого дня, ISO; ожидается понедельник")
    parser.add_argument("--weeks", type=int, default=16, help="сколько недель генерировать")
    parser.add_argument("--out-dir", default="health-synth/out", help="каталог вывода")
    parser.add_argument("--format", choices=("csv", "jsonl", "both"), default="both",
                        help="формат вывода")
    parser.add_argument("--self-check", action="store_true",
                        help="проверить, что паттерны действительно видны в данных")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if not 16 <= args.age <= 90:
        print("Возраст вне поддерживаемого диапазона 16-90.", file=sys.stderr)
        return 2
    if args.weeks < 1:
        print("Число недель должно быть положительным.", file=sys.stderr)
        return 2

    start = date.fromisoformat(args.start_date)
    if start.weekday() != 0:
        print(f"Внимание: {args.start_date} — не понедельник. Нумерация недель "
              f"в expected-patterns.md рассчитана на старт с понедельника.",
              file=sys.stderr)
    if args.weeks != 16:
        print(f"Внимание: паттерны из expected-patterns.md рассчитаны на 16 недель, "
              f"запрошено {args.weeks}. Часть окон может выйти за границы.",
              file=sys.stderr)

    days, records, base, missing_log = generate(args.age, args.seed, start, args.weeks)

    checks, summary = ([], None)
    if args.self_check:
        try:
            checks, summary = self_check(days, records, base)
        except (KeyError, ZeroDivisionError, IndexError) as exc:
            print(f"Самопроверка невозможна на этих параметрах: {exc}", file=sys.stderr)
            checks, summary = [], None

    import os
    os.makedirs(args.out_dir, exist_ok=True)
    written = []
    if args.format in ("csv", "both"):
        path = os.path.join(args.out_dir, "records.csv")
        write_csv(path, records)
        written.append(path)
    if args.format in ("jsonl", "both"):
        path = os.path.join(args.out_dir, "records.jsonl")
        write_jsonl(path, records)
        written.append(path)

    key_path = os.path.join(args.out_dir, "answer-key.json")
    answer_key = build_answer_key(days, records, base, missing_log, args,
                                  {"checks": checks, "summary": summary})
    with open(key_path, "w", encoding="utf-8") as handle:
        json.dump(answer_key, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    written.append(key_path)

    print(f"Возраст {args.age}, сид {args.seed}, {len(days)} дней "
          f"({days[0].date} — {days[-1].date}).")
    print(f"Базовые линии: пульс покоя {base.hr_resting:.1f} bpm, "
          f"HRV {base.hrv_rmssd:.1f} мс, потребность во сне "
          f"{base.sleep_need_min / 60:.2f} ч.")
    print(f"Записей: {len(records)}.")
    for path in written:
        print(f"  {path}")

    if args.self_check:
        if not checks:
            return 1
        print("\nСамопроверка:")
        for item in checks:
            mark = "PASS" if item["passed"] else "FAIL"
            print(f"  [{mark}] {item['name']} — {item['detail']}")
        failed = summary["total"] - summary["passed"]
        print(f"\n{summary['passed']} из {summary['total']} проверок пройдено.")
        if failed:
            print(f"Провалено проверок: {failed}. На этом сиде часть заложенных "
                  f"паттернов утонула в шуме — данные записаны, но аналитике "
                  f"их будет не найти. Возьмите другой --seed: подавляющее "
                  f"большинство проходит все проверки.", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
