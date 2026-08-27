#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор синтетических данных о здоровье и режиме.

Пишет 16 недель суточных записей в формате, описанном в data-format.md,
с заранее заданными паттернами из expected-patterns.md. Ответ известен
заранее: manifest.json содержит точные окна событий, величины эффектов и
пороги, по которым можно объективно оценивать аналитику.

Зависимостей нет — только стандартная библиотека Python 3.9+.

    python3 health-data/generate.py --self-check

Данные синтетические. Формулы базовых линий — грубые популяционные
эвристики для правдоподобия, а не клинические нормы.
"""

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Период и окна паттернов. Индексы дней — от старта, включительно.
# Старт по умолчанию 2026-01-05 (понедельник), 112 дней = 16 недель.
# ---------------------------------------------------------------------------

DEFAULT_START = "2026-01-05"
DEFAULT_WEEKS = 16

TZ_HOME = ("Europe/Moscow", "+03:00")
TZ_TRIP = ("Asia/Shanghai", "+08:00")          # +5 ч к дому

P1_STABLE = (7, 34)        # W2-W5   устойчивый ритм
P2_TRIP_OUT = (42, 46)     # W7      командировка, локальное время +5 ч
P2_TRIP_BACK = (47, 51)    # W7-W8   возврат на запад и шлейф
P3_BINGE = (66, 69)        # W10     срыв режима, чт-вс
P3_BINGE_TAIL = (70, 74)
P6_ILLNESS = (86, 90)      # W13     простуда, ср-вс
P6_ILLNESS_TAIL = (91, 95)
BG_TRAINING = (91, 111)    # W14-W16 фоновый тренировочный блок
NC_QUIET_WEEK = (35, 41)   # W6      негативный контроль: заведомо пустая неделя
P7_LAB_DAYS = [3, 27, 45, 73, 91, 108]   # разрывы 24/18/28/18/17 дней

# Профили интенсивности события по дням окна (0..1).
JET_OUT = [0.55, 0.90, 1.00, 0.95, 0.85]         # восток: адаптация медленная
JET_BACK = [0.70, 0.35, 0.15, 0.05, 0.02]        # запад: быстрее
BINGE = [0.50, 0.80, 1.00, 1.00]
BINGE_TAIL = [0.78, 0.56, 0.38, 0.24, 0.12]
ILL = [0.60, 1.00, 1.00, 0.75, 0.45]
ILL_TAIL = [0.30, 0.20, 0.12, 0.07, 0.03]

# Коэффициенты эффектов при интенсивности 1.0.
K_JET = dict(rhr=6.5, hrv=0.23, rr=0.5, temp=0.15, eff=8.0, bed=60.0, wake=-15.0)
K_BINGE = dict(rhr=8.0, hrv=0.30, rr=0.8, temp=0.35, eff=6.0, bed=215.0, wake=75.0)
K_ILL = dict(rhr=9.0, hrv=0.35, rr=1.5, temp=0.90, eff=3.0, bed=-45.0, wake=105.0)

# Режим сна.
BASE_BEDTIME = 23 * 60 + 10        # минуты от полуночи предыдущего дня
BASE_WAKE = 24 * 60 + 7 * 60       # 07:00 следующего утра
SD_BED_NORMAL = 38.0
SD_BED_STABLE = 14.0
WEEKEND_BED = 50.0
WEEKEND_WAKE = 100.0

# P4: связь позднего отбоя с вариабельностью. Коэффициенты на 1 час позже
# личной базовой линии отбоя.
HRV_LAG1 = 0.060       # ВСР следующего дня, доля
HRV_LAG0 = 0.000       # ВСР той же ночи: эффекта нет, связь чисто отложенная
RHR_LAG1 = 1.6         # пульс покоя следующего дня, bpm
RHR_LAG0 = 0.0

# metric -> (unit, precision). precision -1 = целое, None = строка-время.
METRICS = {
    "sleep_start": ("clock", None),
    "sleep_end": ("clock", None),
    "sleep_duration": ("min", 0),
    "sleep_efficiency": ("%", 1),
    "awakenings": ("count", 0),
    "resting_hr": ("bpm", 0),
    "hrv_rmssd": ("ms", 0),
    # Зарезервировано: Apple Health отдаёт SDNN, а не RMSSD. Генератор эту
    # метрику не производит, но читатель формата обязан её принимать.
    "hrv_sdnn": ("ms", 0),
    "respiratory_rate": ("brpm", 1),
    "temp_deviation": ("celsius", 2),
    "steps": ("count", 0),
    "active_energy": ("kcal", 0),
    "workout_minutes": ("min", 0),
    "whoop.recovery_score": ("%", 0),
    "whoop.strain": ("score", 1),
    "sber.readiness": ("%", 0),
    "weight": ("kg", 1),
    "lab_crp": ("mg/L", 2),
    "lab_ferritin": ("ng/mL", 1),
    "lab_vitamin_d": ("ng/mL", 1),
    "lab_hba1c": ("%", 1),
    "lab_glucose_fasting": ("mmol/L", 2),
    "lab_tsh": ("mIU/L", 2),
    "data_gap": ("count", 0),
}

CSV_COLUMNS = [
    "date", "metric", "value", "unit", "source", "method", "source_device",
    "timezone", "window_start", "window_end", "quality", "record_id",
    "ingested_at", "note",
]

# Лабораторные точки. Значения заданы вручную и согласованы с событиями:
# день 45 внутри командировки, день 73 — через 4 дня после срыва,
# день 91 — на следующий день после болезни (острая фаза).
LAB_PANEL = {
    3:   dict(lab_crp=0.90, lab_ferritin=92.0, lab_vitamin_d=18.5,
              lab_hba1c=5.3, lab_glucose_fasting=5.10, lab_tsh=2.10),
    27:  dict(lab_crp=0.70, lab_ferritin=88.0, lab_vitamin_d=20.8,
              lab_hba1c=5.2, lab_glucose_fasting=4.90, lab_tsh=1.90),
    45:  dict(lab_crp=1.80, lab_ferritin=84.0, lab_vitamin_d=22.4,
              lab_hba1c=5.3, lab_glucose_fasting=5.40, lab_tsh=2.20),
    73:  dict(lab_crp=3.40, lab_ferritin=79.0, lab_vitamin_d=25.1,
              lab_hba1c=5.4, lab_glucose_fasting=5.60, lab_tsh=2.00),
    91:  dict(lab_crp=11.20, lab_ferritin=148.0, lab_vitamin_d=28.0,
              lab_hba1c=5.4, lab_glucose_fasting=5.20, lab_tsh=1.70),
    108: dict(lab_crp=1.20, lab_ferritin=61.0, lab_vitamin_d=31.2,
              lab_hba1c=5.2, lab_glucose_fasting=4.90, lab_tsh=2.10),
}


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def stream(seed, name):
    """Именованный поток случайных чисел.

    Каждая метрика получает свой генератор, поэтому добавление новой метрики
    не сдвигает уже существующие ряды при том же seed.
    """
    digest = hashlib.sha256("{}:{}".format(seed, name).encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def ar1(rng, n, rho, sd):
    """Автокоррелированный шум AR(1) — «липкий», из-за него не каждый
    короткий тренд читается однозначно."""
    out = []
    prev = rng.gauss(0.0, sd)
    step = math.sqrt(1.0 - rho * rho)
    for _ in range(n):
        prev = rho * prev + step * rng.gauss(0.0, sd)
        out.append(prev)
    return out


def profile(day, window, values):
    """Интенсивность события в день day по профилю values на окне window."""
    lo, hi = window
    if lo <= day <= hi:
        idx = day - lo
        if idx < len(values):
            return values[idx]
        return values[-1]
    return 0.0


def in_window(day, window):
    return window[0] <= day <= window[1]


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def fmt_clock(minutes):
    m = int(round(minutes)) % 1440
    return "{:02d}:{:02d}".format(m // 60, m % 60)


def iso(dt, offset):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + offset


def pearson(xs, ys):
    if len(xs) < 3:
        return float("nan")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    return num / (dx * dy) if dx and dy else float("nan")


def slope(ys):
    """Наклон линейной регрессии по индексу, единиц в день."""
    n = len(ys)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2.0
    my = statistics.fmean(ys)
    num = sum((i - mx) * (y - my) for i, y in enumerate(ys))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def baselines(age, sex):
    """Базовые линии от возраста. Эвристики, не клинические нормы."""
    rhr = 54.0 + 0.15 * max(0, age - 25)
    hrv = 62.0 * math.exp(-0.023 * (age - 20))
    sleep_target = (7.75 - 0.008 * (age - 20)) * 60.0
    hr_max = 208.0 - 0.7 * age            # Tanaka
    steps = 9000.0 - 25.0 * max(0, age - 30)
    weight = 78.0
    if sex == "f":
        rhr += 3.0
        hrv *= 0.96
        steps *= 0.97
        weight = 64.0
    return dict(resting_hr=round(rhr, 2), hrv_rmssd=round(hrv, 2),
                sleep_target_min=round(sleep_target, 1), hr_max=round(hr_max, 1),
                steps=round(steps), weight=weight,
                respiratory_rate=14.2, sleep_efficiency=91.0)


# ---------------------------------------------------------------------------
# Симуляция «истинных» суточных значений (до пропусков и источников)
# ---------------------------------------------------------------------------

def simulate(cfg):
    n = cfg.days
    base = baselines(cfg.age, cfg.sex)
    seed = cfg.seed

    r_bed = stream(seed, "bedtime")
    r_wake = stream(seed, "waketime")
    r_eff = stream(seed, "efficiency")
    r_rhr = stream(seed, "resting_hr")
    r_hrv = stream(seed, "hrv")
    r_rr = stream(seed, "respiratory_rate")
    r_temp = stream(seed, "temperature")
    r_steps = stream(seed, "steps")
    r_work = stream(seed, "workout")
    r_weight = stream(seed, "weight")
    r_dev = stream(seed, "devices")
    r_lab = stream(seed, "lab")

    # Автокоррелированный шум: события должны быть на 1.5-3 sigma выше него.
    nz_rhr = ar1(r_rhr, n, 0.45, 1.6)
    nz_hrv = ar1(r_hrv, n, 0.35, 0.055)      # в лог-шкале
    nz_rr = ar1(r_rr, n, 0.40, 0.35)
    nz_temp = ar1(r_temp, n, 0.40, 0.055)
    nz_steps = ar1(r_steps, n, 0.55, 0.20)   # в лог-шкале
    nz_eff = ar1(r_eff, n, 0.30, 2.2)

    days = []
    for i in range(n):
        d = cfg.start + timedelta(days=i)
        jet_out = profile(i, P2_TRIP_OUT, JET_OUT)
        jet_back = profile(i, P2_TRIP_BACK, JET_BACK)
        jet = max(jet_out, jet_back)
        binge = max(profile(i, P3_BINGE, BINGE), profile(i, P3_BINGE_TAIL, BINGE_TAIL))
        ill = max(profile(i, P6_ILLNESS, ILL), profile(i, P6_ILLNESS_TAIL, ILL_TAIL))
        train = 0.0
        if in_window(i, BG_TRAINING):
            train = min(1.0, (i - BG_TRAINING[0] + 1) / 14.0)
        days.append(dict(
            i=i, date=d, weekday=d.weekday(), weekend=d.weekday() >= 5,
            jet=jet, jet_out=jet_out, jet_back=jet_back,
            binge=binge, ill=ill, train=train,
            stable=in_window(i, P1_STABLE),
            abroad=in_window(i, P2_TRIP_OUT),
        ))

    # Медленный дрейф формы: подтягивается к цели с задержкой, поэтому
    # улучшение периода стабильности не исчезает мгновенно после него.
    form = 0.25
    for day in days:
        if day["binge"] > 0.4 or day["ill"] > 0.4:
            target = 0.0
        elif day["stable"]:
            target = 1.0
        elif day["train"] > 0:
            target = 0.85
        else:
            target = 0.35
        form += (target - form) * 0.09
        day["form"] = form

    # --- слой 1: режим сна -------------------------------------------------
    for day in days:
        i = day["i"]
        sd = SD_BED_STABLE if day["stable"] else SD_BED_NORMAL
        dev = r_bed.gauss(0.0, sd)
        if day["weekend"]:
            dev += WEEKEND_BED
        dev += K_JET["bed"] * day["jet"]
        dev += K_BINGE["bed"] * day["binge"] + r_bed.gauss(0.0, 35.0) * day["binge"]
        dev += K_ILL["bed"] * day["ill"]
        bed = BASE_BEDTIME + dev

        wake = BASE_WAKE + r_wake.gauss(0.0, 16.0)
        if day["weekend"]:
            wake += WEEKEND_WAKE
        wake += K_JET["wake"] * day["jet"]
        wake += K_BINGE["wake"] * day["binge"]
        wake += K_ILL["wake"] * day["ill"]

        day["bed_dev"] = dev
        day["bed_abs"] = bed
        day["wake_abs"] = wake
        day["time_in_bed"] = max(200.0, wake - bed)

        eff = (base["sleep_efficiency"] + nz_eff[i]
               - K_JET["eff"] * day["jet"]
               - K_BINGE["eff"] * day["binge"]
               - K_ILL["eff"] * day["ill"]
               - 0.9 * max(0.0, dev) / 60.0)
        day["sleep_efficiency"] = clamp(eff, 62.0, 98.0)
        day["sleep_duration"] = day["time_in_bed"] * day["sleep_efficiency"] / 100.0
        day["awakenings"] = max(0.0, (1.4 + 0.5 * r_bed.random())
                                * (1.0 + 1.0 * day["jet"] + 1.3 * day["binge"]
                                   + 0.8 * day["ill"]))

    # --- слой 2: физиология, включая связь лага-1 с отбоем (P4) ------------
    for day in days:
        i = day["i"]
        late_today = day["bed_dev"] / 60.0
        late_prev = days[i - 1]["bed_dev"] / 60.0 if i > 0 else 0.0

        rhr = base["resting_hr"]
        rhr += -2.2 * day["form"]
        rhr += 2.0 if day["weekday"] == 0 else 0.0          # эффект понедельника
        rhr += K_JET["rhr"] * day["jet"]
        rhr += K_BINGE["rhr"] * day["binge"]
        rhr += K_ILL["rhr"] * day["ill"]
        rhr += RHR_LAG1 * late_prev + RHR_LAG0 * late_today
        rhr += -0.8 * day["train"] + 0.9 * max(0.0, day["train"] - 0.7)
        rhr += nz_rhr[i]
        day["resting_hr"] = clamp(rhr, 38.0, 95.0)

        hrv = base["hrv_rmssd"] * (1.0 + 0.16 * day["form"])
        hrv *= (1.0 - HRV_LAG1 * late_prev)
        hrv *= (1.0 - HRV_LAG0 * late_today)
        hrv *= (1.0 - K_JET["hrv"] * day["jet"])
        hrv *= (1.0 - K_BINGE["hrv"] * day["binge"])
        hrv *= (1.0 - K_ILL["hrv"] * day["ill"])
        hrv *= (1.0 - 0.07 * max(0.0, day["train"] - 0.6))   # накопленная усталость
        hrv *= math.exp(nz_hrv[i])
        day["hrv_rmssd"] = clamp(hrv, 8.0, 160.0)

        day["respiratory_rate"] = clamp(
            base["respiratory_rate"] + nz_rr[i]
            + K_JET["rr"] * day["jet"] + K_BINGE["rr"] * day["binge"]
            + K_ILL["rr"] * day["ill"], 9.0, 24.0)

        day["temp_deviation"] = (nz_temp[i]
                                 + K_JET["temp"] * day["jet"]
                                 + K_BINGE["temp"] * day["binge"]
                                 + K_ILL["temp"] * day["ill"])

    # --- слой 3: активность ------------------------------------------------
    for day in days:
        i = day["i"]
        mult = 1.0
        if day["weekend"]:
            mult *= 0.72
            if r_steps.random() < 0.12:
                mult *= 2.0                                  # длинная прогулка
        mult *= (1.0 + 0.35 * day["train"])
        mult *= (1.0 - 0.80 * day["ill"])
        if day["abroad"]:
            mult *= 1.15
        if i == P2_TRIP_OUT[0] or i == P2_TRIP_BACK[0]:
            mult *= 1.55                                     # день перелёта
        if day["binge"] > 0:
            mult *= (1.0 - 0.25 * day["binge"] + 0.5 * (r_steps.random() - 0.5))
        day["steps"] = max(300.0, base["steps"] * mult * math.exp(nz_steps[i]))

        p_work = 0.45 if not day["weekend"] else 0.35
        p_work += 0.25 * day["train"]
        if day["ill"] > 0.3 or day["binge"] > 0.6:
            p_work = 0.05
        if r_work.random() < p_work:
            day["workout_minutes"] = 30.0 + 45.0 * r_work.random() + 15.0 * day["train"]
        else:
            day["workout_minutes"] = 0.0
        day["active_energy"] = (380.0 + 0.032 * day["steps"]
                                + 6.5 * day["workout_minutes"]
                                + r_work.gauss(0.0, 45.0))

        rec = 50.0 + 120.0 * (day["hrv_rmssd"] / base["hrv_rmssd"] - 1.0) \
            - 1.8 * (day["resting_hr"] - base["resting_hr"])
        day["whoop.recovery_score"] = clamp(rec, 1.0, 99.0)
        day["whoop.strain"] = clamp(
            4.0 + 8.0 * (day["steps"] / base["steps"])
            + 0.055 * day["workout_minutes"] + r_work.gauss(0.0, 0.7), 0.0, 21.0)
        # Кольцо считает по своей закрытой формуле — значения не совпадают.
        day["sber.readiness"] = clamp(0.75 * day["whoop.recovery_score"] + 22.0
                                      + r_dev.gauss(0.0, 3.0), 1.0, 99.0)

        if i % 3 == 0:
            day["weight"] = (base["weight"] - 0.9 * (i / max(1, n - 1))
                             + r_weight.gauss(0.0, 0.35))

    # --- слой 4: лаборатория ----------------------------------------------
    for day in days:
        panel = LAB_PANEL.get(day["i"])
        if panel:
            day["labs"] = {k: v * (1.0 + r_lab.gauss(0.0, 0.02))
                           for k, v in panel.items()}

    return days, base


# ---------------------------------------------------------------------------
# Пропуски: не носили, разрядилось, плохой контакт
# ---------------------------------------------------------------------------

def missingness(cfg, days):
    r = stream(cfg.seed, "missing")
    n = len(days)
    off = dict(whoop=set(), sber_ring=set(), apple_watch=set(), iphone=set())
    gaps = []                       # (день, трек, причина)

    if cfg.no_missing:
        for day in days:
            day["hrv_lost"] = False
        return off, gaps

    def block(track, reason, low, high, length):
        start = r.randint(low, high)
        for k in range(length):
            d = start + k
            if 0 <= d < n:
                off[track].add(d)
                gaps.append((d, track, reason))

    # «Разрядилось»: два блока подряд идущих дней.
    block("whoop", "battery_dead", 18, 30, r.choice([2, 3]))
    block("sber_ring", "battery_dead", 72, 82, r.choice([2, 3]))
    block("apple_watch", "battery_dead", 55, 62, 2)

    for day in days:
        i = day["i"]
        trip_extra = 0.10 if day["abroad"] or i in (P2_TRIP_BACK[0],) else 0.0
        for track, p in (("whoop", 0.05), ("sber_ring", 0.07), ("apple_watch", 0.05)):
            if i not in off[track] and r.random() < p + trip_extra:
                off[track].add(i)
                gaps.append((i, track, "not_worn"))
        if r.random() < 0.02:
            off["iphone"].add(i)
            gaps.append((i, "iphone", "sync_failed"))
        # Частичная потеря: сон записан, ВСР — нет (плохой контакт датчика).
        day["hrv_lost"] = i not in off["whoop"] and r.random() < 0.05
        if day["hrv_lost"]:
            gaps.append((i, "whoop", "bad_contact"))

    return off, gaps


# ---------------------------------------------------------------------------
# Раскладка по источникам и запись строк
# ---------------------------------------------------------------------------

SLEEP_WINDOW = {
    "sleep_start", "sleep_end", "sleep_duration", "sleep_efficiency",
    "awakenings", "resting_hr", "hrv_rmssd", "respiratory_rate",
    "temp_deviation", "whoop.recovery_score", "sber.readiness",
}


def fmt_value(metric, value):
    prec = METRICS[metric][1]
    if prec is None:
        return fmt_clock(value)
    if prec == 0:
        return str(int(round(value)))
    return "{:.{}f}".format(round(value, prec), prec)


def make_row(day, metric, value, source, method, device, tz, note=""):
    name, offset = tz
    d = day["date"]
    midnight = datetime(d.year, d.month, d.day)
    if metric in SLEEP_WINDOW:
        prev = midnight - timedelta(days=1)
        win_start = prev + timedelta(minutes=day["bed_abs"])
        win_end = prev + timedelta(minutes=day["wake_abs"])
    elif metric.startswith("lab_"):
        win_start = win_end = midnight + timedelta(hours=8, minutes=40)
    elif metric == "weight":
        win_start = win_end = midnight + timedelta(hours=7, minutes=30)
    else:
        win_start = midnight
        win_end = midnight + timedelta(days=1)

    quality = 1.0
    if metric.startswith("lab_") or metric in ("weight", "data_gap"):
        quality = 1.0                       # не зависит от ношения устройства
    elif day.get("abroad"):
        quality = 0.78                      # в поездке устройство снимали чаще
    elif metric in ("steps", "active_energy") and day.get("ill", 0) > 0.5:
        quality = 0.85

    key = "{}|{}|{}".format(d.isoformat(), metric, source)
    return {
        "date": d.isoformat(),
        "metric": metric,
        "value": fmt_value(metric, value),
        "unit": METRICS[metric][0],
        "source": source,
        "method": method,
        "source_device": device,
        "timezone": name,
        "window_start": iso(win_start, offset),
        "window_end": iso(win_end, offset),
        "quality": "{:.2f}".format(quality),
        "record_id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:16],
        "ingested_at": iso(midnight + timedelta(days=1, hours=6, minutes=15), offset),
        "note": note,
    }


def build_rows(cfg, days, off, gaps):
    r = stream(cfg.seed, "sources")
    rows = []

    for day in days:
        i = day["i"]
        tz = TZ_TRIP if day["abroad"] else TZ_HOME

        whoop_on = i not in off["whoop"]
        ring_on = i not in off["sber_ring"]
        watch_on = i not in off["apple_watch"]
        phone_on = i not in off["iphone"]

        if whoop_on:
            w = ("whoop", "whoop_4")
            rows.append(make_row(day, "sleep_start", day["bed_abs"], w[0], "device_derived", w[1], tz))
            rows.append(make_row(day, "sleep_end", day["wake_abs"], w[0], "device_derived", w[1], tz))
            rows.append(make_row(day, "sleep_duration", day["sleep_duration"], w[0], "device_derived", w[1], tz))
            rows.append(make_row(day, "sleep_efficiency", day["sleep_efficiency"], w[0], "device_derived", w[1], tz))
            rows.append(make_row(day, "awakenings", day["awakenings"], w[0], "device_derived", w[1], tz))
            rows.append(make_row(day, "resting_hr", day["resting_hr"], w[0], "device_derived", w[1], tz))
            rows.append(make_row(day, "respiratory_rate", day["respiratory_rate"], w[0], "device_derived", w[1], tz))
            rows.append(make_row(day, "whoop.strain", day["whoop.strain"], w[0], "vendor_algorithm", w[1], tz))
            if not day["hrv_lost"]:
                rows.append(make_row(day, "hrv_rmssd", day["hrv_rmssd"], w[0], "device_measured", w[1], tz))
                rows.append(make_row(day, "whoop.recovery_score", day["whoop.recovery_score"],
                                     w[0], "vendor_algorithm", w[1], tz))

        if ring_on:
            s = ("sber_ring", "sber_ring_1")
            rows.append(make_row(day, "temp_deviation", day["temp_deviation"], s[0], "device_measured", s[1], tz))
            rows.append(make_row(day, "sber.readiness", day["sber.readiness"], s[0], "vendor_algorithm", s[1], tz))
            # Кольцо калибровано иначе: систематическое смещение пульса покоя
            # на 1-2 bpm. Это негативный контроль, а не событие.
            if r.random() < 0.45:
                rows.append(make_row(day, "resting_hr", day["resting_hr"] + 1.4 + r.gauss(0.0, 0.5),
                                     s[0], "device_derived", s[1], tz,
                                     note="ring calibration differs from whoop"))
            if r.random() < 0.30:
                rows.append(make_row(day, "sleep_duration", day["sleep_duration"] + r.gauss(0.0, 14.0),
                                     s[0], "device_derived", s[1], tz))

        if watch_on:
            a = ("apple_health", "apple_watch_s9")
            rows.append(make_row(day, "active_energy", day["active_energy"], a[0], "aggregated_daily", a[1], tz))
            rows.append(make_row(day, "workout_minutes", day["workout_minutes"], a[0], "aggregated_daily", a[1], tz))

        if phone_on:
            rows.append(make_row(day, "steps", day["steps"], "apple_health", "aggregated_daily", "iphone", tz))

        if "weight" in day:
            rows.append(make_row(day, "weight", day["weight"], "apple_health", "manual_entry", "iphone", tz))

        for metric, value in sorted(day.get("labs", {}).items()):
            rows.append(make_row(day, metric, value, "lab_generic", "lab_assay", "venous_draw", tz))

    if cfg.emit_gaps:
        by_day = {d["i"]: d for d in days}
        for i, track, reason in sorted(gaps):
            day = by_day[i]
            tz = TZ_TRIP if day["abroad"] else TZ_HOME
            source, device = {
                "whoop": ("whoop", "whoop_4"),
                "sber_ring": ("sber_ring", "sber_ring_1"),
                "apple_watch": ("apple_health", "apple_watch_s9"),
                "iphone": ("apple_health", "iphone"),
            }[track]
            row = make_row(day, "data_gap", 0, source, "imputed", device, tz, note=reason)
            row["record_id"] = hashlib.sha1(
                "{}|data_gap|{}|{}".format(day["date"].isoformat(), source, track)
                .encode("utf-8")).hexdigest()[:16]
            rows.append(row)

    rows.sort(key=lambda x: (x["date"], x["metric"], x["source"]))
    return rows


# ---------------------------------------------------------------------------
# Чтение сгенерированных строк обратно (как это сделает аналитика)
# ---------------------------------------------------------------------------

SOURCE_PRIORITY = {"whoop": 3, "sber_ring": 2, "apple_health": 1, "lab_generic": 1}


def load_series(rows, start_date):
    """Строки -> {метрика: {индекс дня: значение}} с разрешением конфликтов
    источников по приоритету из data-format.md."""
    series, best = {}, {}
    for row in rows:
        metric = row["metric"]
        if metric == "data_gap":
            continue
        i = (date.fromisoformat(row["date"]) - start_date).days
        prio = SOURCE_PRIORITY.get(row["source"], 0)
        if best.get((metric, i), -1) >= prio:
            continue
        best[(metric, i)] = prio
        if METRICS[metric][1] is None:
            hh, mm = row["value"].split(":")
            value = int(hh) * 60 + int(mm)
            if int(hh) < 12:                 # после полуночи — следующие сутки
                value += 1440
        else:
            value = float(row["value"])
        series.setdefault(metric, {})[i] = value
    return series


def window_mean(mapping, window, skip=()):
    vals = [v for k, v in mapping.items()
            if window[0] <= k <= window[1] and k not in skip]
    return statistics.fmean(vals) if vals else float("nan")


def _bed_deviation(series):
    bed = series.get("sleep_start", {})
    if not bed:
        return {}
    med = statistics.median(bed.values())
    return {k: v - med for k, v in bed.items()}


def self_check(cfg, rows):
    """Пересчитывает статистики по уже записанным данным и сверяет их с
    порогами из expected-patterns.md."""
    s = load_series(rows, cfg.start)
    rhr, hrv = s.get("resting_hr", {}), s.get("hrv_rmssd", {})
    sleep, steps = s.get("sleep_duration", {}), s.get("steps", {})
    bed_dev = _bed_deviation(s)
    events = set(range(P2_TRIP_OUT[0], P2_TRIP_BACK[1] + 1)) \
        | set(range(P3_BINGE[0], P3_BINGE_TAIL[1] + 1)) \
        | set(range(P6_ILLNESS[0], P6_ILLNESS_TAIL[1] + 1))
    base_rhr = window_mean(rhr, P1_STABLE)
    base_sleep = window_mean(sleep, P1_STABLE)
    results = []

    def check(pattern, name, value, lo, hi, fmt="{:.3f}"):
        ok = (value == value) and lo <= value <= hi
        results.append(dict(pattern=pattern, check=name, value=value,
                            expected=[lo, hi], passed=bool(ok), fmt=fmt))

    # P1 — период устойчивого ритма. Разброс отбоя считаем после снятия
    # эффекта дня недели, иначе его маскирует регулярный сдвиг выходных.
    adj = {}
    for wd in range(7):
        same = {k: v for k, v in bed_dev.items()
                if (cfg.start + timedelta(days=k)).weekday() == wd}
        if same:
            m = statistics.fmean(same.values())
            adj.update({k: v - m for k, v in same.items()})
    windows = []
    for start in range(0, cfg.days - 27):
        vals = [v for k, v in adj.items() if start <= k <= start + 27]
        windows.append((statistics.pstdev(vals) if len(vals) > 5 else 1e9, start))
    best_start = min(windows)[1]
    overlap = (min(best_start + 27, P1_STABLE[1]) - max(best_start, P1_STABLE[0]) + 1)
    check("P1", "перекрытие самого ровного окна с W2-W5, дней",
          float(max(0, overlap)), 18, 28, "{:.0f}")
    check("P1", "SD отбоя в W2-W5 / SD по периоду",
          statistics.pstdev([v for k, v in adj.items() if in_window(k, P1_STABLE)])
          / statistics.pstdev(list(adj.values())), 0.15, 0.85)

    # P2 — командировка.
    tz_days = sorted({(date.fromisoformat(r["date"]) - cfg.start).days
                      for r in rows if r["timezone"] == TZ_TRIP[0]})
    check("P2", "первый день чужой таймзоны", float(tz_days[0] if tz_days else -1),
          P2_TRIP_OUT[0], P2_TRIP_OUT[0], "{:.0f}")
    check("P2", "последний день чужой таймзоны", float(tz_days[-1] if tz_days else -1),
          P2_TRIP_OUT[1], P2_TRIP_OUT[1], "{:.0f}")
    check("P2", "прирост пульса покоя в командировке, bpm",
          window_mean(rhr, P2_TRIP_OUT) - base_rhr, 3.0, 11.5, "{:.2f}")
    check("P2", "падение ВСР в командировке, %",
          100.0 * (1 - window_mean(hrv, P2_TRIP_OUT) / window_mean(hrv, P1_STABLE)),
          10.0, 35.0, "{:.1f}")
    # Асимметрия: на третий-пятый день после перелёта на восток организм ещё
    # не адаптирован, после возврата на запад — уже почти да.
    out_tail = window_mean(rhr, (P2_TRIP_OUT[1] - 2, P2_TRIP_OUT[1])) - base_rhr
    back_tail = window_mean(rhr, (P2_TRIP_BACK[1] - 2, P2_TRIP_BACK[1])) - base_rhr
    check("P2", "асимметрия восток/запад (остаточный сдвиг, bpm)",
          out_tail - back_tail, 1.0, 11.0, "{:.2f}")

    # P3 — срыв режима.
    check("P3", "прирост пульса покоя на срыве, bpm",
          window_mean(rhr, P3_BINGE) - base_rhr, 4.5, 17.0, "{:.2f}")
    check("P3", "сокращение сна на срыве, мин",
          base_sleep - window_mean(sleep, P3_BINGE), 60.0, 200.0, "{:.1f}")

    # P4 — поздний отбой и ВСР следующего дня.
    def corr(lag, skip=()):
        xs, ys = [], []
        for k, dev in bed_dev.items():
            if k in skip or (k + lag) in skip:
                continue
            if (k + lag) in hrv:
                xs.append(dev)
                ys.append(hrv[k + lag])
        return pearson(xs, ys)

    r1, r0 = corr(1), corr(0)
    check("P4", "корреляция отбоя с ВСР следующего дня (лаг 1)", r1, -0.80, -0.33, "{:+.3f}")
    check("P4", "корреляция той же ночи (лаг 0) слабее лага 1",
          abs(r1) - abs(r0), 0.02, 1.0, "{:+.3f}")
    check("P4", "лаг 1 без окон событий", corr(1, events), -0.80, -0.18, "{:+.3f}")

    # P5 — будни и выходные.
    mid = {k: (s["sleep_start"][k] + s["sleep_end"][k]) / 2.0
           for k in s.get("sleep_start", {}) if k in s.get("sleep_end", {})}
    we = [v for k, v in mid.items() if (cfg.start + timedelta(days=k)).weekday() >= 5]
    wd = [v for k, v in mid.items() if (cfg.start + timedelta(days=k)).weekday() < 5]
    check("P5", "социальный джетлаг, ч",
          (statistics.fmean(we) - statistics.fmean(wd)) / 60.0, 0.8, 1.8, "{:.2f}")
    st_we = [v for k, v in steps.items() if (cfg.start + timedelta(days=k)).weekday() >= 5]
    st_wd = [v for k, v in steps.items() if (cfg.start + timedelta(days=k)).weekday() < 5]
    check("P5", "шаги выходные / будни (медианы)",
          statistics.median(st_we) / statistics.median(st_wd), 0.50, 0.92)
    mon = [v for k, v in rhr.items()
           if (cfg.start + timedelta(days=k)).weekday() == 0 and k not in events]
    other = [v for k, v in rhr.items()
             if 0 < (cfg.start + timedelta(days=k)).weekday() < 5 and k not in events]
    check("P5", "эффект понедельника, bpm",
          statistics.fmean(mon) - statistics.fmean(other), 0.5, 5.5, "{:.2f}")

    # P6 — болезнь, конфаундер для срыва.
    check("P6", "пик отклонения температуры, °C",
          max(v for k, v in s["temp_deviation"].items() if in_window(k, P6_ILLNESS)),
          0.60, 1.30, "{:.2f}")
    check("P6", "сон длиннее базовой линии, мин",
          window_mean(sleep, P6_ILLNESS) - base_sleep, 40.0, 220.0, "{:.1f}")
    check("P6", "шаги во время болезни / базовая линия",
          window_mean(steps, P6_ILLNESS) / window_mean(steps, P1_STABLE), 0.05, 0.60)

    # P7 — лабораторные точки.
    lab_days = sorted({(date.fromisoformat(r["date"]) - cfg.start).days
                       for r in rows if r["metric"].startswith("lab_")})
    check("P7", "число лабораторных дат", float(len(lab_days)), 6, 6, "{:.0f}")
    gaps_between = [b - a for a, b in zip(lab_days, lab_days[1:])]
    check("P7", "разброс интервалов между заборами, дней",
          float(max(gaps_between) - min(gaps_between)), 5, 60, "{:.0f}")
    crp = s.get("lab_crp", {})
    check("P7", "день пикового CRP", float(max(crp, key=crp.get)), 91, 91, "{:.0f}")

    # Негативные контроли — аналитика не должна принимать их за события.
    def t_stat(lo, hi):
        vals = [steps[k] for k in range(lo, hi + 1) if k in steps]
        if len(vals) < 4:
            return None
        b = slope(vals)
        mx = (len(vals) - 1) / 2.0
        my = statistics.fmean(vals)
        rss = sum((y - (my + b * (i - mx))) ** 2 for i, y in enumerate(vals))
        sxx = sum((i - mx) ** 2 for i in range(len(vals)))
        if rss <= 0 or sxx <= 0:
            return None
        return abs(b) / math.sqrt(rss / (len(vals) - 2) / sxx)

    quiet = [t for t in (t_stat(a, a + 4) for a in range(cfg.days - 4)
                         if not (set(range(a, a + 5)) & events))
             if t is not None]
    check("NC1", "спокойных пятидневок с формально значимым наклоном шагов",
          float(sum(1 for t in quiet if t > 2.0)), 1, 60, "{:.0f}")
    all_steps = list(steps.values())
    check("NC1", "шаги в пустой неделе W6 против периода, sigma",
          (window_mean(steps, NC_QUIET_WEEK) - statistics.fmean(all_steps))
          / statistics.pstdev(all_steps), -1.6, 1.6, "{:+.2f}")
    pairs = {}
    for row in rows:
        if row["metric"] == "resting_hr":
            pairs.setdefault(row["date"], {})[row["source"]] = float(row["value"])
    diffs = [v["sber_ring"] - v["whoop"] for v in pairs.values()
             if "sber_ring" in v and "whoop" in v]
    check("NC2", "систематическое смещение кольца против WHOOP, bpm",
          statistics.fmean(diffs) if diffs else float("nan"), 0.5, 2.5, "{:.2f}")

    # Полнота данных.
    expected = cfg.days * 2
    present = len(rhr) + len(sleep)
    lo, hi = (0.0, 0.0) if cfg.no_missing else (1.0, 25.0)
    check("QA", "доля пропущенных дней по ключевым метрикам, %",
          100.0 * (1 - present / expected), lo, hi, "{:.1f}")

    return results


# ---------------------------------------------------------------------------
# Манифест: машиночитаемый ключ к данным
# ---------------------------------------------------------------------------

def build_manifest(cfg, base, rows, checks):
    def span(window):
        return dict(
            days=list(window),
            dates=[(cfg.start + timedelta(days=window[0])).isoformat(),
                   (cfg.start + timedelta(days=window[1])).isoformat()],
        )

    patterns = {
        "P1_stable_rhythm": dict(
            title="Период устойчивого ритма без срывов",
            window=span(P1_STABLE),
            injected=dict(bedtime_sd_min=SD_BED_STABLE,
                          baseline_bedtime="23:10",
                          form_target=1.0, rhr_gain_bpm=-2.2, hrv_gain_ratio=0.16)),
        "P2_timezone_trip": dict(
            title="Командировка со сменой часовых поясов",
            window=span(P2_TRIP_OUT), tail=span(P2_TRIP_BACK),
            injected=dict(timezone_from=TZ_HOME[0], timezone_to=TZ_TRIP[0],
                          shift_hours=5, eastbound_profile=JET_OUT,
                          westbound_profile=JET_BACK, coefficients=K_JET)),
        "P3_routine_collapse": dict(
            title="Срыв режима на несколько дней",
            window=span(P3_BINGE), tail=span(P3_BINGE_TAIL),
            injected=dict(profile=BINGE, tail_profile=BINGE_TAIL, coefficients=K_BINGE)),
        "P4_late_bedtime_hrv": dict(
            title="Поздний отбой -> падение ВСР на следующий день",
            window=dict(days=[0, cfg.days - 1],
                        dates=[cfg.start.isoformat(),
                               (cfg.start + timedelta(days=cfg.days - 1)).isoformat()]),
            injected=dict(hrv_drop_per_hour_lag1=HRV_LAG1,
                          hrv_drop_per_hour_lag0=HRV_LAG0,
                          rhr_rise_per_hour_lag1=RHR_LAG1,
                          rhr_rise_per_hour_lag0=RHR_LAG0)),
        "P5_weekday_weekend": dict(
            title="Будни против выходных, социальный джетлаг",
            window=dict(days=[0, cfg.days - 1]),
            injected=dict(weekend_bedtime_shift_min=WEEKEND_BED,
                          weekend_wake_shift_min=WEEKEND_WAKE,
                          weekend_steps_multiplier=0.72,
                          long_walk_probability=0.12,
                          monday_rhr_bpm=2.0)),
        "P6_illness": dict(
            title="Простуда — конфаундер для срыва режима",
            window=span(P6_ILLNESS), tail=span(P6_ILLNESS_TAIL),
            injected=dict(profile=ILL, tail_profile=ILL_TAIL, coefficients=K_ILL,
                          steps_multiplier=0.20, distinguishing_feature="сон растёт, а не падает")),
        "P7_lab_points": dict(
            title="Лабораторные точки с неравномерными разрывами",
            days=P7_LAB_DAYS,
            dates=[(cfg.start + timedelta(days=d)).isoformat() for d in P7_LAB_DAYS],
            gaps_days=[b - a for a, b in zip(P7_LAB_DAYS, P7_LAB_DAYS[1:])],
            injected=LAB_PANEL),
    }
    negative_controls = {
        "NC1_quiet_week": dict(
            title="Заведомо пустая неделя W6",
            window=span(NC_QUIET_WEEK),
            injected=dict(events="нет", note="тренды здесь рисует только шум AR(1)"),
            expected="любой тренд, найденный в W6, — ложное срабатывание"),
        "NC2_source_bias": dict(
            title="Расхождение пульса покоя между кольцом и WHOOP",
            injected=dict(ring_offset_bpm=1.4, ring_offset_sd=0.5),
            expected="это разница калибровки, а не событие"),
    }
    background = {
        "training_block": dict(title="Фоновый тренировочный блок W14-W16",
                               window=span(BG_TRAINING),
                               injected=dict(steps_multiplier_max=1.35,
                                             hrv_fatigue_max=0.07))}

    return dict(
        generated_by="health-data/generate.py",
        params=dict(age=cfg.age, sex=cfg.sex, seed=cfg.seed,
                    start_date=cfg.start.isoformat(), weeks=cfg.weeks,
                    days=cfg.days, no_missing=cfg.no_missing,
                    emit_gaps=cfg.emit_gaps),
        baselines=base,
        source_priority=SOURCE_PRIORITY,
        row_count=len(rows),
        patterns=patterns,
        negative_controls=negative_controls,
        background=background,
        checks=[dict(pattern=c["pattern"], check=c["check"],
                     expected=c["expected"], observed=round(c["value"], 4),
                     passed=c["passed"]) for c in checks],
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Генератор синтетических данных о здоровье и режиме.")
    p.add_argument("--age", type=int, default=38,
                   help="возраст; от него считаются базовые линии (по умолчанию 38)")
    p.add_argument("--sex", choices=["m", "f"], default="m")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--start-date", default=DEFAULT_START,
                   help="дата старта, понедельник (по умолчанию %s)" % DEFAULT_START)
    p.add_argument("--weeks", type=int, default=DEFAULT_WEEKS)
    p.add_argument("--out-dir", default="health-data/out")
    p.add_argument("--format", choices=["csv", "jsonl", "both"], default="both")
    p.add_argument("--emit-gaps", action="store_true",
                   help="писать строки-маркеры data_gap с причиной пропуска")
    p.add_argument("--no-missing", action="store_true",
                   help="отключить пропуски (для отладки аналитики)")
    p.add_argument("--self-check", action="store_true",
                   help="сверить данные с порогами из expected-patterns.md")
    cfg = p.parse_args(argv)
    cfg.start = date.fromisoformat(cfg.start_date)
    cfg.days = cfg.weeks * 7
    if cfg.age < 18 or cfg.age > 90:
        p.error("--age вне поддерживаемого диапазона 18-90")
    if cfg.weeks < DEFAULT_WEEKS:
        p.error("--weeks не может быть меньше %d: окна паттернов из "
                "expected-patterns.md не поместятся" % DEFAULT_WEEKS)
    return cfg


def write_outputs(cfg, rows, manifest):
    out = Path(cfg.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    if cfg.format in ("csv", "both"):
        path = out / "records.csv"
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        written.append(path)
    if cfg.format in ("jsonl", "both"):
        path = out / "records.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        written.append(path)
    path = out / "manifest.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    written.append(path)
    return written


def main(argv=None):
    cfg = parse_args(argv)
    days, base = simulate(cfg)
    off, gaps = missingness(cfg, days)
    rows = build_rows(cfg, days, off, gaps)
    checks = self_check(cfg, rows)
    manifest = build_manifest(cfg, base, rows, checks)
    written = write_outputs(cfg, rows, manifest)

    print("записей: {}  дней: {}  период: {} .. {}".format(
        len(rows), cfg.days, cfg.start.isoformat(),
        (cfg.start + timedelta(days=cfg.days - 1)).isoformat()))
    print("базовые линии (возраст {}): пульс покоя {} bpm, ВСР {} мс, сон {} мин".format(
        cfg.age, base["resting_hr"], base["hrv_rmssd"], base["sleep_target_min"]))
    for path in written:
        print("  ->", path)

    if cfg.self_check:
        print("\nПроверка паттернов (пороги — из expected-patterns.md):")
        width = max(len(c["check"]) for c in checks)
        for c in checks:
            print("  [{}] {:<4} {:<{w}}  {}  ожидалось [{}; {}]".format(
                "PASS" if c["passed"] else "FAIL", c["pattern"],
                c["check"], c["fmt"].format(c["value"]).rjust(8),
                c["fmt"].format(c["expected"][0]), c["fmt"].format(c["expected"][1]),
                w=width))
        failed = [c for c in checks if not c["passed"]]
        print("\nитого: {} из {} проверок пройдено".format(
            len(checks) - len(failed), len(checks)))
        return 1 if failed else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
