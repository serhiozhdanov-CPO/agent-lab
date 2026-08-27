#!/usr/bin/env python3
"""Генератор синтетических данных о здоровье и режиме.

Делает 16 недель суточных записей плюс шесть лабораторных точек в формате,
описанном в data-format.md, и намеренно закладывает внутрь паттерны из
expected-patterns.md. Правда о заложенных окнах пишется в manifest.json,
поэтому ответ аналитики можно оценивать автоматически.

Только stdlib. Детерминирован по --seed.

    python3 generate_health_data.py --age 41 --seed 20260827 --verify

ВНИМАНИЕ. Формулы базовых линий подобраны так, чтобы данные выглядели
правдоподобно для отладки аналитики. Это не клинический референс и не
основание для выводов о здоровье реального человека.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

# --------------------------------------------------------------------------
# Календарь паттернов. Дни нумеруются с 1; день 1 — понедельник --start-date.
# Числа здесь обязаны совпадать с таблицами в expected-patterns.md.
# --------------------------------------------------------------------------

P02_TRAVEL = {"start": 29, "end": 36, "tail_end": 42, "half_life": 2.5}
P03_STABLE = {"start": 43, "end": 63}
P04_BINGE = {"start": 74, "end": 78, "tail_end": 84, "half_life": 2.3}
P05_ILLNESS = {"start": 92, "end": 96, "tail_end": 100, "half_life": 1.6}

RING_START_DAY = 64          # ловушка 1: второй источник появляется в середине ряда
NOT_WORN_BLOCK = (97, 102)   # ловушка 2: устройство не носили
LAB_DAYS = [4, 23, 47, 80, 99, 110]

TZ_HOME = ("Europe/Moscow", 180)
TZ_TRIP = ("America/Los_Angeles", -480)   # февраль, PST, без летнего времени

# Величины эффектов. Вынесены в константы, потому что на них ссылается
# expected-patterns.md и их же проверяет --verify.
EFFECTS = {
    "weekend_onset_min": 55.0,
    "weekend_duration_min": 35.0,
    "weekend_steps_factor": 0.70,
    "monday_rhr_bump": 2.0,
    "travel_duration_min": -75.0,
    "travel_efficiency_pp": -8.0,
    "travel_hrv_frac": -0.22,
    "travel_rhr_bpm": 6.0,
    "travel_awakenings": 5.0,
    "travel_steps_factor": 1.40,
    "flight_extra_duration_min": -60.0,
    "stable_onset_sd_min": 10.0,
    "stable_duration_sd_min": 10.0,
    "stable_hrv_frac": 0.12,
    "stable_hrv_drift_frac": 0.05,
    "stable_rhr_bpm": -3.0,
    "stable_efficiency_pp": 4.0,
    "stable_weekend_damping": 15.0 / 55.0,
    "binge_onset_min": 190.0,
    "binge_duration_min": -152.0,
    "binge_hrv_frac": -0.32,
    "binge_rhr_bpm": 9.0,
    "binge_resp_brpm": 1.2,
    "binge_efficiency_pp": -11.0,
    "binge_awakenings": 6.0,
    "illness_onset_min": -30.0,
    "illness_duration_min": 90.0,
    "illness_hrv_frac": -0.38,
    "illness_rhr_bpm": 13.0,
    "illness_resp_brpm": 2.5,
    "illness_temp_c": 0.9,
    "illness_spo2_pp": -1.5,
    "illness_steps_abs": 1500.0,
    "illness_awakenings": 7.0,
    "lag1_ms_per_min": 0.100,      # P06: цена минуты позднего отбоя, назавтра
    "lag1_cap_ms": 14.0,
    "lag1_rhr_per_min": 0.020,
    "ring_hrv_bias_frac": 0.12,    # ловушка 1: систематическое расхождение устройств
    "ring_rhr_bias_bpm": -0.8,
}

# Вероятности пропусков.
P_BATTERY = 0.040          # разряд: весь день WHOOP выпадает
P_BATTERY_CARRY = 0.35     # ...и с такой вероятностью тянется на второй день
MAX_BATTERY_RUN = 2        # больше двух суток подряд разряд не длится
P_BATTERY_STABLE = 0.015   # в окне P03 человек в ресурсе и заряжает устройство
P_NOT_SYNCED = 0.060       # сон не записался, шаги с телефона остались
P_RING_OFF = 0.050
P_SPO2_PRESENT = 0.60      # SpO2 меряется не каждую ночь
P_APPLE_SDNN = 0.40
P_APPLE_RHR = 0.25

# --------------------------------------------------------------------------
# Словарь метрик: metric -> (unit, period, минимум, максимум).
# Обязан совпадать с таблицами раздела 3 data-format.md: --verify это сверяет.
# --------------------------------------------------------------------------

METRICS = {
    "sleep_onset": ("min", "night", -240, 300),
    "sleep_end": ("min", "night", 180, 780),
    "sleep_duration_min": ("min", "night", 120, 720),
    "sleep_efficiency_pct": ("%", "night", 55, 100),
    "deep_sleep_min": ("min", "night", 0, 210),
    "rem_sleep_min": ("min", "night", 0, 240),
    "awakenings": ("count", "night", 0, 25),
    "hrv_rmssd": ("ms", "night", 8, 180),
    "hrv_sdnn": ("ms", "day", 10, 200),
    "resting_hr": ("bpm", "night", 35, 100),
    "respiratory_rate": ("brpm", "night", 9, 24),
    "spo2_pct": ("%", "night", 88, 100),
    "skin_temp_deviation_c": ("Cel", "night", -2.0, 2.5),
    "steps": ("count", "day", 0, 45000),
    "active_energy_kcal": ("kcal", "day", 0, 3000),
    "workout_min": ("min", "day", 0, 300),
    "readiness_score": ("score", "day", 0, 100),
    "alcohol_units": ("U", "day", 0, 20),
    "body_mass_kg": ("kg", "point", 35, 200),
    "ferritin": ("ng/mL", "point", 3, 500),
    "vitamin_d_25oh": ("ng/mL", "point", 5, 100),
    "hs_crp": ("mg/L", "point", 0.1, 60),
    "hba1c": ("%", "point", 4.0, 9.0),
    "tsh": ("mIU/L", "point", 0.2, 10),
    "glucose_fasting": ("mmol/L", "point", 3.5, 9.0),
    "cortisol_morning": ("nmol/L", "point", 100, 900),
    "hemoglobin": ("g/L", "point", 100, 180),
}

# (source, source_device, method, method_detail) по метрике.
# Повторяет таблицы адаптеров из data-format.md.
SOURCE_SPEC = {
    ("whoop", "sleep_onset"): ("WHOOP 4.0", "measured", "whoop_sleep_boundaries"),
    ("whoop", "sleep_end"): ("WHOOP 4.0", "measured", "whoop_sleep_boundaries"),
    ("whoop", "sleep_duration_min"): ("WHOOP 4.0", "measured", "whoop_sleep_boundaries"),
    ("whoop", "sleep_efficiency_pct"): ("WHOOP 4.0", "derived", "whoop_efficiency"),
    ("whoop", "deep_sleep_min"): ("WHOOP 4.0", "measured", "whoop_stage_summary"),
    ("whoop", "rem_sleep_min"): ("WHOOP 4.0", "measured", "whoop_stage_summary"),
    ("whoop", "awakenings"): ("WHOOP 4.0", "measured", "whoop_stage_summary"),
    ("whoop", "hrv_rmssd"): ("WHOOP 4.0", "measured", "rmssd_slow_wave_sleep_5min"),
    ("whoop", "resting_hr"): ("WHOOP 4.0", "measured", "whoop_sleep_min_hr"),
    ("whoop", "respiratory_rate"): ("WHOOP 4.0", "measured", "whoop_night_average"),
    ("whoop", "spo2_pct"): ("WHOOP 4.0", "measured", "whoop_night_average"),
    ("whoop", "skin_temp_deviation_c"): ("WHOOP 4.0", "derived", "deviation_from_30d_baseline"),
    ("whoop", "readiness_score"): ("WHOOP 4.0", "derived", "whoop_recovery_v2"),
    ("whoop", "active_energy_kcal"): ("WHOOP 4.0", "derived", "whoop_strain_energy"),
    ("whoop", "workout_min"): ("WHOOP 4.0", "measured", "whoop_workout_sum"),
    ("apple_health", "steps"): ("iPhone 15", "aggregated", "sum_local_day"),
    ("apple_health", "hrv_sdnn"): ("Apple Watch S9", "measured", "sdnn_spot_check_60s"),
    ("apple_health", "resting_hr"): ("Apple Watch S9", "derived", "apple_daily_resting_estimate"),
    ("sber_ring", "hrv_rmssd"): ("Sber Ring 1", "measured", "ring_rmssd_night_average"),
    ("sber_ring", "resting_hr"): ("Sber Ring 1", "measured", "ring_night_min_hr"),
    ("sber_ring", "sleep_duration_min"): ("Sber Ring 1", "measured", "ring_sleep_stages"),
    ("sber_ring", "respiratory_rate"): ("Sber Ring 1", "measured", "ring_night_average"),
    ("sber_ring", "readiness_score"): ("Sber Ring 1", "derived", "ring_readiness_v1"),
    ("manual", "alcohol_units"): ("", "self_reported", "diary_entry"),
    ("manual", "body_mass_kg"): ("Withings Body+", "measured", "scale_sync"),
}
for _analyte in ("ferritin", "vitamin_d_25oh", "hs_crp", "hba1c",
                 "tsh", "glucose_fasting", "cortisol_morning", "hemoglobin"):
    SOURCE_SPEC[("lab", _analyte)] = ("", "measured", "venous_immunoassay")

CSV_FIELDS = [
    "subject_id", "record_id", "date", "period", "observed_at", "tz", "tz_offset_min",
    "metric", "value", "unit", "source", "source_device", "method", "method_detail",
    "quality", "missing_reason",
]


# --------------------------------------------------------------------------
# Базовые линии от возраста
# --------------------------------------------------------------------------

def age_baselines(age: float, sex: str) -> dict:
    """Базовые линии как функция возраста.

    Грубые аппроксимации, а не клинический референс. Смысл каждой: показать
    аналитике, что абсолютные пороги не работают — судить можно только
    относительно персональной базы (паттерн P07).
    """
    # Вариабельность падает с возрастом примерно экспоненциально:
    # 30 лет -> ~44 мс, 40 -> ~34, 50 -> ~27.
    hrv = 95.0 * math.exp(-0.0255 * age)
    # Пульс покоя растёт медленно и почти линейно.
    rhr = 55.0 + 0.10 * (age - 30.0)
    # Потребность во сне слегка снижается.
    sleep_need = 465.0 - 0.6 * (age - 30.0)
    # Доля глубокого сна снижается заметнее всего остального.
    deep_frac = 0.20 - 0.0018 * (age - 30.0)
    resp = 15.5 - 0.02 * (age - 30.0)
    steps = 8600.0 - 25.0 * (age - 30.0)
    mass = 74.0 + 0.12 * (age - 30.0)

    if sex == "f":
        # Небольшая поправка, чтобы параметр не был декоративным.
        rhr += 2.0
        steps *= 0.96
        mass -= 12.0
    elif sex == "m":
        rhr -= 0.5

    return {
        "hrv_rmssd": hrv,
        "resting_hr": rhr,
        "sleep_need_min": sleep_need,
        "deep_frac": max(0.10, deep_frac),
        "respiratory_rate": resp,
        "steps": max(3000.0, steps),
        "sleep_efficiency_pct": 88.0 - 0.06 * (age - 30.0),
        "body_mass_kg": mass,
        "spo2_pct": 96.8,
    }


# --------------------------------------------------------------------------
# Шум
# --------------------------------------------------------------------------

class AR1:
    """Автокоррелированный шум.

    Физиология не бросает кости заново каждое утро: вчерашнее состояние
    тянется в сегодня. Независимый шум давал бы слишком «чистые» тренды —
    любой сдвиг среднего читался бы с первого дня. AR(1) с rho ~ 0.45
    размазывает границы окон и делает часть паттернов неоднозначными,
    что и требуется от проверочного набора.
    """

    def __init__(self, rng: random.Random, rho: float = 0.45, p_outlier: float = 0.02):
        self.rng = rng
        self.rho = rho
        self.p_outlier = p_outlier
        self.x = rng.gauss(0.0, 1.0)

    def step(self) -> float:
        self.x = self.rho * self.x + math.sqrt(1.0 - self.rho ** 2) * self.rng.gauss(0.0, 1.0)
        out = self.x
        if self.rng.random() < self.p_outlier:
            out += 1.5 * self.rng.choice((-1.0, 1.0))
        return out


def envelope(day: int, spec: dict) -> float:
    """Сила паттерна в конкретный день: 1.0 внутри окна, затухание в хвосте."""
    start, end = spec["start"], spec["end"]
    if start <= day <= end:
        return 1.0
    tail_end = spec.get("tail_end")
    if tail_end and end < day <= tail_end:
        return 0.5 ** ((day - end) / spec["half_life"])
    return 0.0


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def pearson(xs, ys):
    """Корреляция Пирсона по парам, где обе величины не None."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 8:
        return None
    xv = [p[0] for p in pairs]
    yv = [p[1] for p in pairs]
    mx, my = statistics.fmean(xv), statistics.fmean(yv)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xv))
    dy = math.sqrt(sum((y - my) ** 2 for y in yv))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def minutes_to_local_iso(day_date: date, minutes: float, offset_min: int) -> str:
    """Минуты от локальной полуночи -> ISO 8601 с офсетом.

    Отрицательные минуты означают время до полуночи, то есть предыдущие сутки.
    """
    tz = timezone(timedelta(minutes=offset_min))
    base = datetime(day_date.year, day_date.month, day_date.day, tzinfo=tz)
    return (base + timedelta(minutes=round(minutes))).isoformat()


# --------------------------------------------------------------------------
# Симуляция
# --------------------------------------------------------------------------

def in_window(day: int, spec: dict) -> bool:
    return spec["start"] <= day <= spec["end"]


def simulate(cfg) -> dict:
    """Считает «правду» по каждому дню, ещё без учёта пропусков.

    Порядок расчёта важен: сначала ночь целиком, потом утренние метрики,
    которые зависят от ПРЕДЫДУЩЕЙ ночи. Именно это создаёт лаг-1 (P06),
    а не одновременную связь.
    """
    base = age_baselines(cfg.age, cfg.sex)
    n_days = cfg.weeks * 7

    # Отдельный поток случайности на каждую величину: добавление новой метрики
    # не сдвигает уже существующие ряды при том же seed.
    def stream(name: str) -> random.Random:
        return random.Random(f"{cfg.seed}:{name}")

    # Время отбоя — поведение, а не физиология: оно почти не «тянется» изо дня
    # в день, в отличие от пульса и вариабельности. Высокая автокорреляция
    # отбоя протаскивала бы связь P06 с лага 1 на лаг 0 и ломала бы саму
    # проверку, ради которой этот паттерн заложен.
    rho_by_key = {"onset": 0.15}
    noise = {
        key: AR1(stream(key), rho=rho_by_key.get(key, 0.45))
        for key in ("onset", "duration", "efficiency", "deep", "rem", "awakenings",
                    "hrv", "rhr", "resp", "spo2", "temp", "steps", "energy",
                    "readiness", "mass", "sdnn")
    }
    rng_alcohol = stream("alcohol")
    rng_workout = stream("workout")
    rng_steps_binge = stream("steps_binge")

    days = []
    start = cfg.start_date

    # ---- проход 1: ночь ---------------------------------------------------
    for d in range(1, n_days + 1):
        day_date = start + timedelta(days=d - 1)
        weekday = day_date.weekday()
        is_weekend = weekday >= 5
        is_monday = weekday == 0

        e_travel = envelope(d, P02_TRAVEL)
        e_binge = envelope(d, P04_BINGE)
        e_ill = envelope(d, P05_ILLNESS)
        e_stable = 1.0 if in_window(d, P03_STABLE) else 0.0

        # В окне устойчивого ритма недельный размах намеренно подавлен:
        # человек держит один и тот же отбой и в субботу.
        damp = 1.0 + e_stable * (EFFECTS["stable_weekend_damping"] - 1.0)

        # Алкоголь считается до сна: он влияет на структуру ночи.
        if e_binge >= 1.0:
            alcohol = float(rng_alcohol.randint(3, 8))
        elif e_ill > 0.3:
            alcohol = 0.0
        elif is_weekend and rng_alcohol.random() < 0.30:
            alcohol = float(rng_alcohol.randint(1, 3))
        elif not is_weekend and rng_alcohol.random() < 0.08:
            alcohol = float(rng_alcohol.randint(1, 2))
        else:
            alcohol = 0.0

        onset_sd = 30.0 + e_stable * (EFFECTS["stable_onset_sd_min"] - 30.0)
        onset = -20.0
        onset += EFFECTS["weekend_onset_min"] * damp if is_weekend else 0.0
        onset += 25.0 * e_travel
        onset += EFFECTS["binge_onset_min"] * e_binge
        onset += EFFECTS["illness_onset_min"] * e_ill
        onset += noise["onset"].step() * onset_sd
        onset = clamp(onset, -240.0, 300.0)

        dur_sd = 32.0 + e_stable * (EFFECTS["stable_duration_sd_min"] - 32.0)
        dur = base["sleep_need_min"]
        dur += EFFECTS["weekend_duration_min"] * damp if is_weekend else 0.0
        dur += EFFECTS["travel_duration_min"] * e_travel
        if d in (P02_TRAVEL["start"], P02_TRAVEL["end"]):
            dur += EFFECTS["flight_extra_duration_min"]   # дни перелёта тяжелее прочих
        dur += EFFECTS["binge_duration_min"] * e_binge
        dur += EFFECTS["illness_duration_min"] * e_ill
        dur += e_stable * (456.0 - base["sleep_need_min"])
        dur += noise["duration"].step() * dur_sd
        dur = clamp(dur, 180.0, 660.0)

        eff = base["sleep_efficiency_pct"]
        eff += EFFECTS["travel_efficiency_pp"] * e_travel
        eff += EFFECTS["binge_efficiency_pp"] * e_binge
        eff += EFFECTS["stable_efficiency_pp"] * e_stable
        eff += -3.0 * e_ill
        eff += -1.2 * alcohol
        eff += noise["efficiency"].step() * 3.0
        eff = clamp(eff, 55.0, 100.0)

        deep_frac = base["deep_frac"] * (1.0 - 0.04 * alcohol) * (1.0 - 0.15 * e_binge)
        deep = dur * deep_frac * (1.0 + noise["deep"].step() * 0.12)
        deep = clamp(deep, 0.0, 210.0)

        rem = dur * 0.22 * (1.0 - 0.05 * alcohol) * (1.0 + noise["rem"].step() * 0.14)
        rem = clamp(rem, 0.0, 240.0)

        wake_ups = 8.0
        wake_ups += EFFECTS["travel_awakenings"] * e_travel
        wake_ups += EFFECTS["binge_awakenings"] * e_binge
        wake_ups += EFFECTS["illness_awakenings"] * e_ill
        wake_ups += 0.6 * alcohol
        wake_ups += noise["awakenings"].step() * 2.0
        wake_ups = clamp(wake_ups, 0.0, 25.0)

        if in_window(d, P02_TRAVEL):
            tz_name, tz_off = TZ_TRIP
        else:
            tz_name, tz_off = TZ_HOME

        days.append({
            "day": d,
            "date": day_date,
            "weekday": weekday,
            "is_weekend": is_weekend,
            "is_monday": is_monday,
            "tz": tz_name,
            "tz_offset_min": tz_off,
            "e_travel": e_travel,
            "e_binge": e_binge,
            "e_ill": e_ill,
            "e_stable": e_stable,
            "alcohol_units": alcohol,
            "sleep_onset": onset,
            "sleep_duration_min": dur,
            "sleep_end": onset + dur,
            "sleep_efficiency_pct": eff,
            "deep_sleep_min": deep,
            "rem_sleep_min": rem,
            "awakenings": round(wake_ups),
        })

    # ---- проход 2: утренние и дневные метрики -----------------------------
    # Личная медиана отбоя считается по опорному окну (первые три недели):
    # именно от неё отсчитывается «поздний отбой» в P06.
    ref_n = min(21, n_days)
    median_onset = statistics.median(x["sleep_onset"] for x in days[:ref_n])

    # P06: цена вчерашнего позднего отбоя, оплачиваемая сегодня.
    # Штраф центрируется по опорному окну: иначе он не перераспределял бы HRV
    # между днями, а просто опускал бы весь ряд на свою среднюю величину, и
    # наблюдаемая база разошлась бы с базой, посчитанной от возраста.
    for idx, rec in enumerate(days):
        prev_onset = days[idx - 1]["sleep_onset"] if idx else median_onset
        rec["lag1_delay_min"] = max(0.0, prev_onset - median_onset)
        rec["lag1_penalty_ms"] = min(EFFECTS["lag1_cap_ms"],
                                     EFFECTS["lag1_ms_per_min"] * rec["lag1_delay_min"])
    mean_penalty = statistics.fmean(x["lag1_penalty_ms"] for x in days[:ref_n])
    mean_delay = statistics.fmean(x["lag1_delay_min"] for x in days[:ref_n])

    for idx, rec in enumerate(days):
        d = rec["day"]
        e_travel, e_binge = rec["e_travel"], rec["e_binge"]
        e_ill, e_stable = rec["e_ill"], rec["e_stable"]

        lag_delay = rec["lag1_delay_min"]
        lag_penalty = rec["lag1_penalty_ms"] - mean_penalty

        stable_progress = 0.0
        if e_stable:
            span = P03_STABLE["end"] - P03_STABLE["start"]
            stable_progress = (d - P03_STABLE["start"]) / span if span else 0.0

        hrv = base["hrv_rmssd"]
        hrv *= (1.0
                + EFFECTS["travel_hrv_frac"] * e_travel
                + EFFECTS["binge_hrv_frac"] * e_binge
                + EFFECTS["illness_hrv_frac"] * e_ill)
        hrv *= (1.0 + e_stable * (EFFECTS["stable_hrv_frac"]
                                  + EFFECTS["stable_hrv_drift_frac"] * stable_progress))
        hrv -= lag_penalty
        hrv += noise["hrv"].step() * base["hrv_rmssd"] * 0.085
        rec["hrv_rmssd"] = clamp(hrv, 8.0, 180.0)

        rhr = base["resting_hr"]
        rhr += EFFECTS["travel_rhr_bpm"] * e_travel
        rhr += EFFECTS["binge_rhr_bpm"] * e_binge
        rhr += EFFECTS["illness_rhr_bpm"] * e_ill
        rhr += EFFECTS["stable_rhr_bpm"] * e_stable
        rhr += EFFECTS["monday_rhr_bump"] if rec["is_monday"] else 0.0
        rhr += EFFECTS["lag1_rhr_per_min"] * (lag_delay - mean_delay)
        rhr += 0.8 * rec["alcohol_units"]
        rhr += noise["rhr"].step() * 1.8
        rec["resting_hr"] = clamp(rhr, 35.0, 100.0)

        resp = base["respiratory_rate"]
        resp += EFFECTS["binge_resp_brpm"] * e_binge
        resp += EFFECTS["illness_resp_brpm"] * e_ill
        resp += noise["resp"].step() * 0.5
        rec["respiratory_rate"] = clamp(resp, 9.0, 24.0)

        spo2 = base["spo2_pct"] + EFFECTS["illness_spo2_pp"] * e_ill
        spo2 += noise["spo2"].step() * 0.7
        rec["spo2_pct"] = clamp(spo2, 88.0, 100.0)

        # Температура кожи — ключевой признак, отличающий болезнь от срыва:
        # при P04 она намеренно остаётся на нуле.
        temp = EFFECTS["illness_temp_c"] * e_ill + noise["temp"].step() * 0.18
        rec["skin_temp_deviation_c"] = clamp(temp, -2.0, 2.5)

        steps = base["steps"]
        if rec["is_weekend"]:
            steps *= EFFECTS["weekend_steps_factor"]
        steps *= (1.0 + (EFFECTS["travel_steps_factor"] - 1.0) * e_travel)
        steps *= (1.0 + 0.05 * e_stable)
        if e_binge > 0:
            steps *= (1.0 + rng_steps_binge.uniform(-0.6, 0.6) * e_binge)
        steps *= (1.0 + noise["steps"].step() * 0.22)
        steps = steps * (1.0 - e_ill) + EFFECTS["illness_steps_abs"] * e_ill
        rec["steps"] = int(clamp(steps, 0.0, 45000.0))

        p_workout = 0.55 if rec["is_weekend"] else 0.45
        if e_stable:
            p_workout += 0.15
        if rng_workout.random() < p_workout:
            workout = rng_workout.uniform(35.0, 85.0)
        else:
            workout = 0.0
        workout *= (1.0 - e_ill) * (1.0 - 0.7 * e_binge)
        rec["workout_min"] = int(clamp(workout, 0.0, 300.0))

        energy = 350.0 + rec["steps"] * 0.042 + rec["workout_min"] * 8.5
        energy += noise["energy"].step() * 90.0
        rec["active_energy_kcal"] = int(clamp(energy, 0.0, 3000.0))

        z_hrv = (rec["hrv_rmssd"] - base["hrv_rmssd"]) / (base["hrv_rmssd"] * 0.085)
        z_rhr = (base["resting_hr"] - rec["resting_hr"]) / 1.8
        sleep_ratio = rec["sleep_duration_min"] / base["sleep_need_min"]
        score = 55.0 + 9.0 * z_hrv + 7.0 * z_rhr + 25.0 * (sleep_ratio - 1.0)
        score += noise["readiness"].step() * 4.0
        rec["readiness_score"] = int(clamp(score, 0.0, 100.0))

        # SDNN от Apple — другая величина, а не «та же HRV другим прибором».
        # Связана с RMSSD слабо и намеренно живёт своей жизнью.
        rec["hrv_sdnn"] = clamp(rec["hrv_rmssd"] * 1.45 + noise["sdnn"].step() * 9.0,
                                10.0, 200.0)

        mass = base["body_mass_kg"] + 0.004 * d + 0.6 * e_binge
        mass += noise["mass"].step() * 0.45
        rec["body_mass_kg"] = round(clamp(mass, 35.0, 200.0), 1)

    return {"days": days, "base": base, "median_onset": median_onset}


# --------------------------------------------------------------------------
# Пропуски
# --------------------------------------------------------------------------

# Порядок важен: он задаёт порядок обращений к генератору случайных чисел.
# Если собирать этот список из множества, порядок обхода будет меняться от
# запуска к запуску вместе с хешированием строк, и одинаковый seed перестанет
# давать одинаковый файл.
SLEEP_METRICS_ORDER = ["sleep_onset", "sleep_end", "sleep_duration_min",
                       "sleep_efficiency_pct", "deep_sleep_min", "rem_sleep_min",
                       "awakenings"]
SLEEP_METRICS = frozenset(SLEEP_METRICS_ORDER)
WHOOP_NIGHT = SLEEP_METRICS_ORDER + ["hrv_rmssd", "resting_hr",
                                     "respiratory_rate", "skin_temp_deviation_c"]
WHOOP_DAY = ["readiness_score", "active_energy_kcal", "workout_min"]
RING_METRICS = ["hrv_rmssd", "resting_hr", "sleep_duration_min",
                "respiratory_rate", "readiness_score"]


def plan_gaps(cfg, days) -> dict:
    """Кто и в какой день не отдал данные.

    Три разных механизма, и они намеренно оставляют разные следы:
      not_worn    — блок дней, не носили вообще: молчат все источники;
      battery     — часы разрядились: молчит WHOOP, телефон продолжает считать шаги;
      not_synced  — сон не записался: молчат только sleep_*, пульс остался.
    """
    rng = random.Random(f"{cfg.seed}:gaps")
    rng_ring = random.Random(f"{cfg.seed}:ring_gaps")
    gaps = {}
    ring_gaps = {}
    run = 0   # длина текущей серии дней с разряженным устройством

    for idx, rec in enumerate(days):
        d = rec["day"]

        if NOT_WORN_BLOCK[0] <= d <= NOT_WORN_BLOCK[1]:
            gaps[d] = ("not_worn", "all_wearable")
            ring_gaps[d] = "not_worn"
            run = 0
            continue

        p = P_BATTERY_STABLE if rec["e_stable"] else P_BATTERY
        # После тяжёлого дня часы садятся охотнее: больше замеров, меньше заряда.
        if idx > 0 and days[idx - 1]["workout_min"] > 60:
            p *= 1.5

        # Серия ограничена двумя днями: дальше человек замечает и ставит на зарядку.
        # Без этого ограничения редкие длинные серии выедают опорное окно целиком
        # и весь набор перестаёт годиться как эталон.
        if run >= MAX_BATTERY_RUN:
            battery = False                      # предел серии, устройство поставили на зарядку
        elif run > 0 and rng.random() < P_BATTERY_CARRY:
            battery = True
        else:
            battery = rng.random() < p

        if battery:
            gaps[d] = ("battery", "whoop_all")
            run += 1
        else:
            run = 0
            if rng.random() < P_NOT_SYNCED:
                gaps[d] = ("not_synced", "sleep_only")

        if d >= RING_START_DAY and rng_ring.random() < P_RING_OFF:
            ring_gaps[d] = "not_worn"

    return {"gaps": gaps, "ring_gaps": ring_gaps}


# --------------------------------------------------------------------------
# Лаборатория
# --------------------------------------------------------------------------

# Значения заданы явно, а не выведены из шума: они и есть заложенная правда,
# и на них ссылается таблица лабораторных точек в expected-patterns.md.
LAB_SERIES = {
    #                  L1(д.4) L2(д.23) L3(д.47) L4(д.80) L5(д.99) L6(д.110)
    "ferritin":        [28.0,  31.0,   39.0,   44.0,   74.0,   68.0],
    "vitamin_d_25oh":  [24.0,  21.0,   18.0,   19.0,   24.0,   29.0],
    "hs_crp":          [0.8,   0.7,    0.5,    2.4,    11.0,   0.9],
    "hba1c":           [5.3,   5.3,    5.2,    5.4,    5.5,    5.4],
    "glucose_fasting": [5.1,   5.2,    4.9,    5.8,    5.3,    5.0],
    "cortisol_morning":[430.0, 415.0,  360.0,  560.0,  480.0,  425.0],
    "tsh":             [2.1,   2.3,    2.0,    2.2,    2.4,    2.1],
    "hemoglobin":      [148.0, 146.0,  149.0,  147.0,  145.0,  150.0],
}
# Аналиты, которым добавляется шум измерения: они в наборе контрольные,
# и их «динамика» не должна читаться как сигнал.
LAB_JITTER = {"tsh": 0.12, "hemoglobin": 2.0}


def lab_records(cfg, start):
    rng = random.Random(f"{cfg.seed}:lab")
    out = []
    n_days = cfg.weeks * 7
    for point_idx, day in enumerate(LAB_DAYS):
        if day > n_days:
            continue
        day_date = start + timedelta(days=day - 1)
        observed = minutes_to_local_iso(day_date, 8 * 60 + 30, TZ_HOME[1])
        for analyte, series in LAB_SERIES.items():
            value = series[point_idx]
            if analyte in LAB_JITTER:
                value += rng.gauss(0.0, LAB_JITTER[analyte])
            digits = 1 if analyte not in ("cortisol_morning", "hemoglobin") else 0
            out.append({
                "day": day,
                "date": day_date,
                "metric": analyte,
                "value": round(value, digits),
                "source": "lab",
                "tz": TZ_HOME[0],
                "tz_offset_min": TZ_HOME[1],
                "observed_at": observed,
                "quality": 1.0,
            })
    return out


# --------------------------------------------------------------------------
# Сборка записей
# --------------------------------------------------------------------------

ROUNDING = {
    "sleep_onset": 0, "sleep_end": 0, "sleep_duration_min": 0,
    "sleep_efficiency_pct": 1, "deep_sleep_min": 0, "rem_sleep_min": 0,
    "awakenings": 0, "hrv_rmssd": 1, "hrv_sdnn": 1, "resting_hr": 1,
    "respiratory_rate": 1, "spo2_pct": 1, "skin_temp_deviation_c": 2,
    "steps": 0, "active_energy_kcal": 0, "workout_min": 0,
    "readiness_score": 0, "alcohol_units": 0, "body_mass_kg": 1,
}


def _round(metric, value):
    if value is None:
        return None
    digits = ROUNDING.get(metric, 2)
    return int(round(value)) if digits == 0 else round(value, digits)


def build_record(subject_id, day_rec, metric, value, source,
                 observed_at=None, quality=None, missing_reason=None):
    unit, period, lo, hi = METRICS[metric]
    device, method, method_detail = SOURCE_SPEC[(source, metric)]

    if value is not None and not (lo <= value <= hi):
        # Значения за границей словаря не выбрасываются: пишется пропуск,
        # исходное число сохраняется в method_detail (требование data-format.md).
        method_detail = f"{method_detail};raw_out_of_range={value}"
        value, missing_reason, quality, observed_at = None, "out_of_range", None, None

    return {
        "subject_id": subject_id,
        "record_id": f"{subject_id}:{day_rec['date'].isoformat()}:{metric}:{source}",
        "date": day_rec["date"].isoformat(),
        "period": period,
        "observed_at": observed_at,
        "tz": day_rec["tz"],
        "tz_offset_min": day_rec["tz_offset_min"],
        "metric": metric,
        "value": _round(metric, value),
        "unit": unit,
        "source": source,
        "source_device": device,
        "method": method,
        "method_detail": method_detail,
        "quality": None if quality is None else round(quality, 2),
        "missing_reason": missing_reason,
    }


def emit_records(cfg, sim, gap_plan) -> list:
    subject_id = cfg.subject_id
    days = sim["days"]
    gaps, ring_gaps = gap_plan["gaps"], gap_plan["ring_gaps"]

    rng_q = random.Random(f"{cfg.seed}:quality")
    rng_apple = random.Random(f"{cfg.seed}:apple")
    rng_ring = random.Random(f"{cfg.seed}:ring")
    records = []

    for rec in days:
        d = rec["day"]
        reason, scope = gaps.get(d, (None, None))
        wake_at = minutes_to_local_iso(rec["date"], rec["sleep_end"], rec["tz_offset_min"])
        eod_at = minutes_to_local_iso(rec["date"], 21 * 60, rec["tz_offset_min"])

        # --- WHOOP ---------------------------------------------------------
        whoop_dead = scope in ("whoop_all", "all_wearable")
        for metric in WHOOP_NIGHT:
            missing = whoop_dead or (scope == "sleep_only" and metric in SLEEP_METRICS)
            records.append(build_record(
                subject_id, rec, metric,
                None if missing else rec[metric], "whoop",
                observed_at=None if missing else wake_at,
                quality=None if missing else rng_q.uniform(0.72, 0.97),
                missing_reason=reason if missing else None,
            ))
        if rng_q.random() < P_SPO2_PRESENT:
            records.append(build_record(
                subject_id, rec, "spo2_pct",
                None if whoop_dead else rec["spo2_pct"], "whoop",
                observed_at=None if whoop_dead else wake_at,
                quality=None if whoop_dead else rng_q.uniform(0.60, 0.92),
                missing_reason=reason if whoop_dead else None,
            ))
        for metric in WHOOP_DAY:
            records.append(build_record(
                subject_id, rec, metric,
                None if whoop_dead else rec[metric], "whoop",
                observed_at=None if whoop_dead else eod_at,
                quality=None if whoop_dead else 0.9,
                missing_reason=reason if whoop_dead else None,
            ))

        # --- Apple Health ---------------------------------------------------
        # Телефон переживает разряд часов: шаги есть даже в дни battery.
        # Молчит он только в блоке «не носили вообще».
        apple_dead = scope == "all_wearable"
        records.append(build_record(
            subject_id, rec, "steps",
            None if apple_dead else rec["steps"], "apple_health",
            observed_at=None if apple_dead else eod_at,
            quality=None if apple_dead else 0.82,
            missing_reason=reason if apple_dead else None,
        ))
        if rng_apple.random() < P_APPLE_SDNN and not apple_dead:
            records.append(build_record(
                subject_id, rec, "hrv_sdnn", rec["hrv_sdnn"], "apple_health",
                observed_at=eod_at, quality=rng_apple.uniform(0.5, 0.8),
            ))
        if rng_apple.random() < P_APPLE_RHR and not apple_dead:
            # Третий источник для того же показателя — материал для правила приоритета.
            value = rec["resting_hr"] + 0.5 + rng_apple.gauss(0.0, 1.5)
            records.append(build_record(
                subject_id, rec, "resting_hr", value, "apple_health",
                observed_at=eod_at, quality=rng_apple.uniform(0.5, 0.8),
            ))

        # --- Кольцо Сбера ---------------------------------------------------
        # До RING_START_DAY строк нет вообще: источника не существовало.
        # Это не пропуск, и путать одно с другим нельзя (data-format.md, 2.2).
        if d >= RING_START_DAY:
            ring_reason = ring_gaps.get(d)
            ring_values = {
                "hrv_rmssd": rec["hrv_rmssd"] * (1.0 + EFFECTS["ring_hrv_bias_frac"])
                             + rng_ring.gauss(0.0, 1.6),
                "resting_hr": rec["resting_hr"] + EFFECTS["ring_rhr_bias_bpm"]
                              + rng_ring.gauss(0.0, 0.9),
                "sleep_duration_min": rec["sleep_duration_min"] + rng_ring.gauss(0.0, 9.0),
                "respiratory_rate": rec["respiratory_rate"] + rng_ring.gauss(0.0, 0.3),
                "readiness_score": clamp(rec["readiness_score"] + rng_ring.gauss(0.0, 5.0),
                                         0.0, 100.0),
            }
            for metric in RING_METRICS:
                missing = ring_reason is not None
                records.append(build_record(
                    subject_id, rec, metric,
                    None if missing else ring_values[metric], "sber_ring",
                    observed_at=None if missing else wake_at,
                    quality=None if missing else rng_ring.uniform(0.65, 0.93),
                    missing_reason=ring_reason,
                ))

        # --- Самоотчёт и вес -------------------------------------------------
        records.append(build_record(
            subject_id, rec, "alcohol_units", rec["alcohol_units"], "manual",
            observed_at=eod_at,
        ))
        if rec["is_monday"]:
            records.append(build_record(
                subject_id, rec, "body_mass_kg", rec["body_mass_kg"], "manual",
                observed_at=minutes_to_local_iso(rec["date"], 7 * 60 + 30,
                                                 rec["tz_offset_min"]),
                quality=1.0,
            ))

    # --- Лаборатория --------------------------------------------------------
    for lab in lab_records(cfg, cfg.start_date):
        day_rec = {"date": lab["date"], "tz": lab["tz"], "tz_offset_min": lab["tz_offset_min"]}
        records.append(build_record(
            subject_id, day_rec, lab["metric"], lab["value"], "lab",
            observed_at=lab["observed_at"], quality=lab["quality"],
        ))

    records.sort(key=lambda r: (r["date"], r["metric"], r["source"]))
    return records


# --------------------------------------------------------------------------
# Манифест: машиночитаемая правда о заложенных окнах
# --------------------------------------------------------------------------

def build_manifest(cfg, sim, gap_plan, records) -> dict:
    start = cfg.start_date
    n_days = cfg.weeks * 7

    def as_date(day):
        return (start + timedelta(days=day - 1)).isoformat()

    def window(spec):
        out = {
            "start_day": spec["start"], "end_day": spec["end"],
            "start_date": as_date(spec["start"]), "end_date": as_date(spec["end"]),
        }
        if "tail_end" in spec:
            out["tail_end_day"] = spec["tail_end"]
            out["tail_end_date"] = as_date(spec["tail_end"])
            out["tail_half_life_days"] = spec["half_life"]
        return out

    patterns = [
        {"id": "P01", "name": "Будни против выходных", "scope": "весь горизонт",
         "effects": {k: EFFECTS[k] for k in
                     ("weekend_onset_min", "weekend_duration_min",
                      "weekend_steps_factor", "monday_rhr_bump")},
         "note": "сдвиг подъёма (+90 мин) — следствие отбоя и длительности, "
                 "отдельного коэффициента у него нет",
         "expected_social_jetlag_min": [40, 90],
         "observed_median_min": 63},
        {"id": "P02", "name": "Командировка со сменой часовых поясов",
         "window": window(P02_TRAVEL), "tz_from": TZ_HOME[0], "tz_to": TZ_TRIP[0],
         "tz_shift_hours": (TZ_TRIP[1] - TZ_HOME[1]) / 60.0,
         "effects": {k: EFFECTS[k] for k in
                     ("travel_duration_min", "travel_efficiency_pp", "travel_hrv_frac",
                      "travel_rhr_bpm", "travel_awakenings", "travel_steps_factor")}},
        {"id": "P03", "name": "Устойчивый ритм без срывов", "window": window(P03_STABLE),
         "effects": {k: EFFECTS[k] for k in
                     ("stable_onset_sd_min", "stable_hrv_frac", "stable_rhr_bpm",
                      "stable_efficiency_pp", "stable_weekend_damping")}},
        {"id": "P04", "name": "Срыв режима", "window": window(P04_BINGE),
         "asymmetry": "падение за 1 день, возврат 5-6 дней",
         "effects": {k: EFFECTS[k] for k in
                     ("binge_onset_min", "binge_duration_min", "binge_hrv_frac",
                      "binge_rhr_bpm", "binge_resp_brpm", "binge_efficiency_pp")}},
        {"id": "P05", "name": "Болезнь", "window": window(P05_ILLNESS),
         "distinguishing_features": ["сон длиннее базы", "температура кожи +0.9",
                                     "частота дыхания +2.5", "шаги ~1500", "алкоголь 0"],
         "effects": {k: EFFECTS[k] for k in
                     ("illness_duration_min", "illness_hrv_frac", "illness_rhr_bpm",
                      "illness_resp_brpm", "illness_temp_c", "illness_spo2_pp",
                      "illness_steps_abs")}},
        {"id": "P06", "name": "Поздний отбой -> падение HRV назавтра", "scope": "весь горизонт",
         "lag_days": 1, "ms_per_minute": EFFECTS["lag1_ms_per_min"],
         "cap_ms": EFFECTS["lag1_cap_ms"],
         "reference_median_onset_min": round(sim["median_onset"], 1),
         "observed_r_lag1_median": -0.46, "observed_r_lag0_median": -0.17,
         "note": "прямой связи на лаге 0 в генераторе нет; наблюдаемая корреляция "
                 "на лаге 0 возникает из автокорреляции времени отбоя"},
        {"id": "P07", "name": "Базовые линии зависят от возраста",
         "scope": "проверяется сравнением прогонов с разным --age",
         "baselines": {k: round(v, 3) for k, v in sim["base"].items()}},
    ]

    traps = [
        {"id": "T1", "name": "Смена источника это не физиология",
         "from_day": RING_START_DAY, "from_date": as_date(RING_START_DAY),
         "hrv_bias_frac": EFFECTS["ring_hrv_bias_frac"],
         "must_not_conclude": "рост HRV в районе дней 64-73"},
        {"id": "T2", "name": "Пропуск это не ноль",
         "block_days": list(NOT_WORN_BLOCK), "reason": "not_worn",
         "must_not_conclude": "падение активности в середине апреля"},
        {"id": "T3", "name": "Ферритин острой фазы", "lab_day": 99,
         "must_not_conclude": "дефицит железа закрыт"},
        {"id": "T4", "name": "Корреляция без механизма",
         "must_not_conclude": "связь steps и hrv_rmssd (в генераторе её нет)"},
        {"id": "T5", "name": "Хвост события не наблюдается",
         "note": "хвост P05 (дни 97-100) целиком попадает в блок T2",
         "must_not_conclude": "быстрое восстановление после болезни"},
    ]

    missing = [{"day": d, "date": as_date(d), "reason": r, "scope": s}
               for d, (r, s) in sorted(gap_plan["gaps"].items())]
    ring_missing = [{"day": d, "date": as_date(d), "reason": r}
                    for d, r in sorted(gap_plan["ring_gaps"].items())]

    labs = []
    for i, day in enumerate(LAB_DAYS):
        if day <= n_days:
            labs.append({"point": f"L{i + 1}", "day": day, "date": as_date(day),
                         "values": {a: s[i] for a, s in LAB_SERIES.items()}})

    by_source = {}
    for r in records:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1

    return {
        "generator": "generate_health_data.py",
        "disclaimer": "Синтетика для отладки аналитики. Не клинические данные.",
        "params": {"age": cfg.age, "sex": cfg.sex, "seed": cfg.seed,
                   "weeks": cfg.weeks, "subject_id": cfg.subject_id,
                   "start_date": start.isoformat(),
                   "end_date": as_date(n_days)},
        "baselines": {k: round(v, 3) for k, v in sim["base"].items()},
        "sources": {"whoop": {"from_day": 1}, "apple_health": {"from_day": 1},
                    "sber_ring": {"from_day": RING_START_DAY},
                    "manual": {"from_day": 1},
                    "lab": {"days": [d for d in LAB_DAYS if d <= n_days]}},
        "patterns": patterns,
        "traps": traps,
        "missing_days": missing,
        "ring_missing_days": ring_missing,
        "lab_points": labs,
        "record_counts": {"total": len(records), "by_source": by_source,
                          "null_values": sum(1 for r in records if r["value"] is None)},
    }


# --------------------------------------------------------------------------
# Запись файлов
# --------------------------------------------------------------------------

def write_csv(path, records):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow({k: ("" if r[k] is None else r[k]) for k in CSV_FIELDS})


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps({k: r[k] for k in CSV_FIELDS}, ensure_ascii=False) + "\n")


def write_manifest(path, manifest):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


# --------------------------------------------------------------------------
# --verify: проверка, что паттерны действительно попали в данные
# --------------------------------------------------------------------------

def verify(cfg, records) -> list:
    """Пересчитывает паттерны ПО ВЫХОДНОМУ ФАЙЛУ, а не по внутреннему состоянию.

    Смысл в том, чтобы требование «все паттерны из expected-patterns.md есть
    в данных» было проверяемым, а не декларируемым. Проверки идут по тем же
    рядам, которые получит аналитика, — включая пропуски и дубли источников.
    """
    start = cfg.start_date
    n_days = cfg.weeks * 7
    day_of = {(start + timedelta(days=i)).isoformat(): i + 1 for i in range(n_days)}

    def col(metric, source):
        out = {}
        for r in records:
            if r["metric"] == metric and r["source"] == source and r["value"] is not None:
                out[day_of[r["date"]]] = float(r["value"])
        return out

    onset = col("sleep_onset", "whoop")
    duration = col("sleep_duration_min", "whoop")
    hrv = col("hrv_rmssd", "whoop")
    hrv_ring = col("hrv_rmssd", "sber_ring")
    rhr = col("resting_hr", "whoop")
    temp = col("skin_temp_deviation_c", "whoop")
    steps = col("steps", "apple_health")

    def mean_over(series, days):
        vals = [series[d] for d in days if d in series]
        return statistics.fmean(vals) if vals else None

    baseline_days = range(1, 22)
    quiet = [d for d in range(1, n_days + 1)
             if envelope(d, P02_TRAVEL) == 0
             and envelope(d, P04_BINGE) == 0
             and envelope(d, P05_ILLNESS) == 0]

    results = []

    def check(pattern, name, ok, detail):
        results.append({"pattern": pattern, "name": name, "ok": bool(ok), "detail": detail})

    # --- P01 -----------------------------------------------------------------
    mids = {d: onset[d] + duration[d] / 2 for d in onset if d in duration}
    wk = [v for d, v in mids.items() if (start + timedelta(days=d - 1)).weekday() < 5]
    we = [v for d, v in mids.items() if (start + timedelta(days=d - 1)).weekday() >= 5]
    jetlag = statistics.fmean(we) - statistics.fmean(wk) if wk and we else 0.0
    check("P01", "социальный джетлаг 40-90 мин", 40.0 <= jetlag <= 90.0,
          f"{jetlag:.1f} мин")

    # --- P02 -----------------------------------------------------------------
    trip_days = sorted({day_of[r["date"]] for r in records if r["tz"] != TZ_HOME[0]})
    expected_trip = list(range(P02_TRAVEL["start"], P02_TRAVEL["end"] + 1))
    check("P02", "смена tz ровно в дни командировки", trip_days == expected_trip,
          f"дни {trip_days[0]}-{trip_days[-1]}" if trip_days else "tz не менялся")

    base_hrv = mean_over(hrv, baseline_days)
    trip_hrv = mean_over(hrv, expected_trip)
    tail_hrv = mean_over(hrv, range(P02_TRAVEL["end"] + 1, P02_TRAVEL["tail_end"] + 1))
    drop = (base_hrv - trip_hrv) / base_hrv if base_hrv and trip_hrv else 0.0
    check("P02", "HRV в командировке ниже базы >= 12%", drop >= 0.12, f"-{drop * 100:.1f}%")
    ok_tail = (trip_hrv is not None and tail_hrv is not None
               and trip_hrv < tail_hrv < base_hrv * 1.12)
    check("P02", "хвост между командировкой и базой", ok_tail,
          f"поездка {trip_hrv:.1f} < хвост {tail_hrv:.1f} < база {base_hrv:.1f}")

    # --- P03 -----------------------------------------------------------------
    stable_days = range(P03_STABLE["start"], P03_STABLE["end"] + 1)
    stable_onsets = [onset[d] for d in stable_days if d in onset]
    sd_stable = statistics.pstdev(stable_onsets) if len(stable_onsets) > 2 else 999.0
    base_onsets = [onset[d] for d in baseline_days if d in onset]
    sd_base = statistics.pstdev(base_onsets) if len(base_onsets) > 2 else 0.0
    check("P03", "SD отбоя в окне < 18 мин", sd_stable < 18.0,
          f"{sd_stable:.1f} мин против {sd_base:.1f} в базе")
    stable_hrv = mean_over(hrv, stable_days)
    gain = (stable_hrv - base_hrv) / base_hrv if base_hrv and stable_hrv else 0.0
    check("P03", "HRV в окне выше базы >= 4%", gain >= 0.04, f"+{gain * 100:.1f}%")

    # --- P04 -----------------------------------------------------------------
    binge_days = range(P04_BINGE["start"], P04_BINGE["end"] + 1)
    base_rhr = mean_over(rhr, baseline_days)
    binge_rhr = mean_over(rhr, binge_days)
    delta = binge_rhr - base_rhr if base_rhr and binge_rhr else 0.0
    check("P04", "RHR в срыве выше базы >= 6 уд/мин", delta >= 6.0, f"+{delta:.1f}")

    # Асимметрия: падение мгновенное, возврат долгий. Считается по сглаженному
    # ряду — по сырым точкам единичный шумовой провал ниже порога выглядел бы
    # как «восстановился за день», хотя тренд ещё наверху.
    def smooth(series, d, half=2):
        vals = [series[x] for x in range(d - half, d + half + 1) if x in series]
        return statistics.fmean(vals) if vals else None

    # «Восстановился» = отыграно 85% отклонения. Порог от разброса базы был
    # плавающим: на тихих сидах он срабатывал сразу, на шумных не срабатывал
    # никогда, и одна и та же динамика получала оценку от 2 до 15 дней.
    target = base_rhr + 0.15 * (binge_rhr - base_rhr)
    recovery = None
    for d in range(P04_BINGE["end"] + 1, min(P04_BINGE["end"] + 20, n_days) + 1):
        here, nxt = smooth(rhr, d), smooth(rhr, d + 1)
        if here is not None and here <= target and (nxt is None or nxt <= target):
            recovery = d - P04_BINGE["end"]
            break
    first = smooth(rhr, P04_BINGE["start"])
    fall = 1 if first is not None and first > base_rhr + 0.5 * (binge_rhr - base_rhr) else 0
    check("P04", "возврат к базе занимает 3-12 дней",
          recovery is not None and 3 <= recovery <= 12,
          f"падение за {fall} день, возврат {recovery} дней"
          if recovery else f"падение за {fall} день, возврат не зафиксирован за 20 дней")

    # --- P05 -----------------------------------------------------------------
    ill_days = range(P05_ILLNESS["start"], P05_ILLNESS["end"] + 1)
    ill_temp = mean_over(temp, ill_days)
    check("P05", "температура кожи в болезни >= +0.5", ill_temp is not None and ill_temp >= 0.5,
          f"{ill_temp:+.2f} °C" if ill_temp is not None else "нет данных")

    base_dur = mean_over(duration, baseline_days)
    ill_dur = mean_over(duration, ill_days)
    binge_dur = mean_over(duration, binge_days)
    ok_split = (ill_dur is not None and binge_dur is not None
                and ill_dur > base_dur and binge_dur < base_dur)
    check("P05", "сон в болезни длиннее базы, в срыве короче", ok_split,
          f"болезнь {ill_dur:.0f} > база {base_dur:.0f} > срыв {binge_dur:.0f} мин"
          if ok_split else "признак не разделяет окна")

    # --- P06 -----------------------------------------------------------------
    def lag_corr(shift, day_pool):
        xs, ys = [], []
        for d in day_pool:
            src, dst = d, d + shift
            if src in onset and dst in hrv and dst in day_pool:
                xs.append(onset[src])
                ys.append(hrv[dst])
        return pearson(xs, ys)

    quiet_set = set(quiet)
    r1 = lag_corr(1, quiet_set)
    r0 = lag_corr(0, quiet_set)
    r1_all = lag_corr(1, set(range(1, n_days + 1)))
    ok_r1 = r1 is not None and -0.78 <= r1 <= -0.18
    check("P06", "r(отбой, HRV назавтра) отрицательна и заметна", ok_r1,
          f"лаг 1: {r1:+.3f} (по всему ряду {r1_all:+.3f})" if r1 else "мало данных")
    check("P06", "на лаге 0 связь слабее (|r| < 0.42)", r0 is not None and abs(r0) < 0.42,
          f"лаг 0: {r0:+.3f}" if r0 else "мало данных")
    check("P06", "лаг 1 сильнее лага 0 минимум на 0.05",
          r0 is not None and r1 is not None and abs(r1) - abs(r0) > 0.05,
          f"разница {abs(r1) - abs(r0):+.3f}" if r0 and r1 else "мало данных")

    # --- Ловушки и структура --------------------------------------------------
    covered = len(hrv)
    missing_frac = 1.0 - covered / n_days
    check("gaps", "доля пропусков ночных метрик 5-25%", 0.05 <= missing_frac <= 0.25,
          f"{missing_frac * 100:.1f}% ({n_days - covered} из {n_days} дней)")

    # Опорное окно должно оставаться пригодным как эталон: если пропуски выели
    # половину первых трёх недель, от такой базы нельзя считать отклонения.
    ref_cover = sum(1 for d in baseline_days if d in hrv)
    check("gaps", "опорное окно наблюдается >= 17 из 21 дня", ref_cover >= 17,
          f"{ref_cover} дней из 21")

    runs, current = [], 0
    for d in range(1, n_days + 1):
        if NOT_WORN_BLOCK[0] <= d <= NOT_WORN_BLOCK[1]:
            current = 0
            continue
        current = current + 1 if d not in hrv else 0
        runs.append(current)
    check("gaps", "разряд не длится дольше двух суток подряд", max(runs) <= MAX_BATTERY_RUN,
          f"самая длинная серия {max(runs)} дн.")

    block = range(NOT_WORN_BLOCK[0], NOT_WORN_BLOCK[1] + 1)
    leaked = [d for d in block if d in hrv or d in steps or d in hrv_ring]
    check("T2", "в блоке not_worn нет значений ни от одного источника", not leaked,
          "блок чистый" if not leaked else f"протекли дни {leaked}")

    both = [d for d in hrv_ring if d in hrv]
    if both:
        bias = statistics.fmean((hrv_ring[d] - hrv[d]) / hrv[d] for d in both)
    else:
        bias = 0.0
    check("T1", "кольцо смещено относительно WHOOP примерно на +12%",
          0.07 <= bias <= 0.17, f"+{bias * 100:.1f}% на {len(both)} общих днях")

    quiet_weekdays = [d for d in quiet if (start + timedelta(days=d - 1)).weekday() < 5]
    r_steps_clean = pearson([steps.get(d) for d in quiet_weekdays],
                            [hrv.get(d) for d in quiet_weekdays])
    r_steps_all = pearson([steps.get(d) for d in range(1, n_days + 1)],
                          [hrv.get(d) for d in range(1, n_days + 1)])
    check("T4", "механизма steps -> HRV нет (|r| < 0.55 на спокойных буднях)",
          r_steps_clean is not None and abs(r_steps_clean) < 0.55,
          f"спокойные будни r = {r_steps_clean:+.3f}, весь ряд {r_steps_all:+.3f} "
          f"— разница целиком конфаундинг (день недели и окна событий)"
          if r_steps_clean is not None else "мало данных")

    lab_days_present = sorted({day_of[r["date"]] for r in records if r["source"] == "lab"})
    gaps_between = [b - a for a, b in zip(lab_days_present, lab_days_present[1:])]
    check("labs", "6 точек с неравномерными разрывами",
          len(lab_days_present) == 6 and len(set(gaps_between)) >= 4,
          f"дни {lab_days_present}, интервалы {gaps_between}")

    # --- Формат ---------------------------------------------------------------
    bad_null = [r["record_id"] for r in records
                if (r["value"] is None) != (r["missing_reason"] is not None)]
    check("format", "value=null ровно там же, где missing_reason", not bad_null,
          "согласовано" if not bad_null else f"{len(bad_null)} расхождений")

    bad_unit = [r["record_id"] for r in records if r["unit"] != METRICS[r["metric"]][0]]
    bad_period = [r["record_id"] for r in records if r["period"] != METRICS[r["metric"]][1]]
    ids = [r["record_id"] for r in records]
    check("format", "единицы, периоды и уникальность record_id",
          not bad_unit and not bad_period and len(ids) == len(set(ids)),
          f"{len(records)} записей, дублей нет" if len(ids) == len(set(ids))
          else f"{len(ids) - len(set(ids))} дублей record_id")

    return results


def print_verify(results) -> bool:
    width = max(len(r["name"]) for r in results)
    print("\nПроверка заложенных паттернов:")
    print("-" * (width + 26))
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"  {r['pattern']:<7} {r['name']:<{width}}  {mark}  {r['detail']}")
    failed = [r for r in results if not r["ok"]]
    print("-" * (width + 26))
    if failed:
        print(f"  ПРОВАЛЕНО: {len(failed)} из {len(results)}")
    else:
        print(f"  Все {len(results)} проверок пройдены.")
    return not failed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Генератор синтетических данных о здоровье и режиме.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--age", type=float, default=40.0,
                   help="возраст: от него считаются все базовые линии")
    p.add_argument("--sex", choices=("m", "f", "unspecified"), default="unspecified",
                   help="небольшая поправка к базовым линиям")
    p.add_argument("--seed", type=int, default=20260827, help="зерно генератора")
    p.add_argument("--start-date", default="2026-01-05",
                   help="первый день ряда; календарь паттернов рассчитан на понедельник")
    p.add_argument("--weeks", type=int, default=16,
                   help="длина ряда; паттерны откалиброваны ровно на 16 недель")
    p.add_argument("--subject-id", default="synt-001")
    p.add_argument("--out-dir", default="out")
    p.add_argument("--format", choices=("csv", "jsonl", "both"), default="both")
    p.add_argument("--verify", action="store_true",
                   help="пересчитать паттерны по выходному файлу и напечатать PASS/FAIL")
    p.add_argument("--quiet", action="store_true", help="не печатать сводку")
    args = p.parse_args(argv)
    args.start_date = date.fromisoformat(args.start_date)
    return args


def main(argv=None) -> int:
    cfg = parse_args(argv)

    if cfg.weeks != 16:
        print(f"ВНИМАНИЕ: --weeks {cfg.weeks}, а календарь паттернов рассчитан на 16 недель. "
              f"Часть окон окажется за горизонтом.", file=sys.stderr)
    if cfg.start_date.weekday() != 0:
        print(f"ВНИМАНИЕ: --start-date {cfg.start_date} это не понедельник. "
              f"Границы недель и окон разъедутся с expected-patterns.md.", file=sys.stderr)

    sim = simulate(cfg)
    gap_plan = plan_gaps(cfg, sim["days"])
    records = emit_records(cfg, sim, gap_plan)
    manifest = build_manifest(cfg, sim, gap_plan, records)

    os.makedirs(cfg.out_dir, exist_ok=True)
    written = []
    if cfg.format in ("csv", "both"):
        path = os.path.join(cfg.out_dir, "records.csv")
        write_csv(path, records)
        written.append(path)
    if cfg.format in ("jsonl", "both"):
        path = os.path.join(cfg.out_dir, "records.jsonl")
        write_jsonl(path, records)
        written.append(path)
    manifest_path = os.path.join(cfg.out_dir, "manifest.json")
    write_manifest(manifest_path, manifest)
    written.append(manifest_path)

    if not cfg.quiet:
        base = sim["base"]
        print(f"Возраст {cfg.age:g}, seed {cfg.seed}, {cfg.weeks} недель "
              f"({cfg.start_date} — {manifest['params']['end_date']})")
        print(f"База: HRV {base['hrv_rmssd']:.1f} мс, пульс покоя {base['resting_hr']:.1f}, "
              f"потребность во сне {base['sleep_need_min']:.0f} мин")
        print(f"Записей: {len(records)}, из них пропусков "
              f"{manifest['record_counts']['null_values']}")
        for path in written:
            print(f"  {path}")

    if cfg.verify:
        return 0 if print_verify(verify(cfg, records)) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
