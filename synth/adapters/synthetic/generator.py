"""Генератор синтетических данных как адаптер слоя приёма.

Сутки собираются слоями, в этом порядке:

    скрытые медленные состояния (вегетативный тонус, форма)
      → недельный ритм
      → наложение событий с лаговыми ядрами
      → приборный шум
      → слой пропусков

Пропуски накладываются последними и из отдельного потока случайных чисел,
не связанного ни с одним состоянием: по построению это MCAR (см. P-08).

Детерминизм: один seed, но у каждой подсистемы свой поток случайных чисел.
Благодаря этому добавление нового типа событий не сдвигает все остальные ряды.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from ...canonical import LAB_METRICS, METRICS, Observation, Source
from . import events as ev
from . import labs as lab
from . import profile as prof
from . import rhythm as rh

ADAPTER_VERSION = "0.1.0"
VENDOR = "synthetic"
LAB_VENDOR = "lab"
# Второй вендор для P-10: кольцо присылает свой пульс покоя за те же дни.
RING_VENDOR = "sber_ring"
RING_RHR_OFFSET = 1.4          # систематическая разница калибровки, не физиология
RING_RHR_OFFSET_SD = 0.5
RING_LOG_PROB = 0.45

# --- P-02 · связка пульс↔вариабельность --------------------------------------
# Общее скрытое состояние вегетативного тонуса: AR(1), стационарная дисперсия 1.
AUTONOMIC_PHI = 0.55
AUTONOMIC_RHR_LOADING = 1.8
AUTONOMIC_LOG_RMSSD_LOADING = -0.22
AUTONOMIC_RESP_LOADING = 0.25

# Шум в шагах автокоррелирован: активность держится сериями, и это не
# украшение. Именно из-за него в спокойных пятидневках сами собой заводятся
# формально значимые «тренды» — то, на чём стоит негативный контроль P-10.
STEPS_AR_PHI = 0.55

# --- P-08 · шум --------------------------------------------------------------
# Физиологическая изменчивость и приборный шум разделены: первая — свойство
# человека, второй — свойство устройства, и у будущих адаптеров он будет свой.
PHYSIO_RHR_SD = 1.3
DEVICE_RHR_SD = 0.8
PHYSIO_LOG_RMSSD_SD = 0.14
DEVICE_LOG_RMSSD_SD = 0.17
PHYSIO_SLEEP_DURATION_SD = 28.0
DEVICE_SLEEP_DURATION_SD = 12.0
SLEEP_ONSET_SD = 32.0
SLEEP_EFFICIENCY_SD = 2.2
RESP_SD = 0.35
TEMP_SD = 0.07
ENERGY_SD = 0.35

# --- P-08 · пропуски ---------------------------------------------------------
WEARABLE_DROP_PROB = 0.07
FORCED_GAP_DAYS = range(71, 74)      # устройство на зарядке
WEIGHT_LOG_PROB = 0.60
ENERGY_LOG_PROB = 0.85

# Прочее
WEIGHT_DRIFT_KG = -1.4
WEIGHT_SD = 0.4
ENERGY_INTERCEPT = 3.3
ENERGY_PER_RHR = -0.14
ENERGY_PER_LOG_RMSSD = 1.4
HRMAX_TRAINING_FRACTION = 0.86
HRMAX_REST_FRACTION = 0.62
ACTIVE_ENERGY_PER_STEP = 0.042
ACTIVE_ENERGY_PER_LOAD = 4.2
ACTIVE_ENERGY_SD = 45.0


@dataclass(frozen=True)
class GeneratorConfig:
    age: float = 38.0
    weeks: int = 16
    seed: int = 42
    start_date: date = date(2026, 1, 5)      # понедельник
    timezone: str = "Europe/Moscow"
    subject_id: str = "synth-0001"
    weight_kg: float = 78.0

    @property
    def total_days(self) -> int:
        return self.weeks * 7


@dataclass
class DayRecord:
    """Полностью собранные сутки — до наложения пропусков."""

    day: int
    day_date: date
    weekday: int
    values: dict[str, float] = field(default_factory=dict)
    timezone: str = ""
    sleep_start: datetime | None = None
    sleep_end: datetime | None = None
    debt_hours: float = 0.0
    fitness: float = 0.0


def _stream(seed: int, name: str) -> random.Random:
    """Отдельный поток случайных чисел на подсистему."""
    return random.Random(f"{seed}:{name}")


class SyntheticAdapter:
    """Адаптер слоя приёма, у которого источник данных — не устройство, а модель.

    Реализует тот же контракт (synth/adapters/base.py), что и будущие адаптеры
    Apple Health, WHOOP и кольца Сбера, и пишет тот же канонический формат.
    """

    name = VENDOR
    adapter_version = ADAPTER_VERSION

    def capabilities(self) -> set[str]:
        """Генератор умеет весь реестр — тем он и отличается от реального вендора."""
        return set(METRICS)

    def read(self, raw: GeneratorConfig) -> Iterator[Observation]:
        observations, _, _ = self.generate(raw)
        return iter(observations)

    # ------------------------------------------------------------------
    def generate(
        self, cfg: GeneratorConfig
    ) -> tuple[list[Observation], dict[str, Any], dict[str, Any]]:
        """Собрать поток наблюдений, манифест и ключ ответов."""
        tz = ZoneInfo(cfg.timezone)
        n = cfg.total_days

        rng_subject = _stream(cfg.seed, "subject")
        rng_auto = _stream(cfg.seed, "autonomic")
        rng_sleep = _stream(cfg.seed, "sleep")
        rng_activity = _stream(cfg.seed, "activity")
        rng_device = _stream(cfg.seed, "device")
        rng_alcohol = _stream(cfg.seed, "alcohol")
        rng_labs = _stream(cfg.seed, "labs")
        rng_energy = _stream(cfg.seed, "energy")
        rng_weight = _stream(cfg.seed, "weight")
        rng_missing = _stream(cfg.seed, "missing")
        rng_ring = _stream(cfg.seed, "ring")

        base = prof.baselines(cfg.age, rng_subject, cfg.weight_kg)
        dates = [cfg.start_date + timedelta(days=d) for d in range(n)]
        weekdays = [dt.weekday() for dt in dates]

        alcohol_by_day = ev.schedule_alcohol(n, weekdays, rng_alcohol)

        records = self._build_days(
            cfg, base, dates, weekdays, alcohol_by_day, tz,
            rng_auto, rng_sleep, rng_activity, rng_device, rng_energy, rng_weight,
        )
        panels = lab.draw_panels(n, rng_labs)

        observations = self._emit(cfg, records, panels, rng_missing, rng_ring)
        manifest = self._manifest(cfg, base, dates, observations)
        truth = self._ground_truth(cfg, base, records, panels, alcohol_by_day)
        return observations, manifest, truth

    # ------------------------------------------------------------------
    def _build_days(
        self, cfg, base, dates, weekdays, alcohol_by_day, tz,
        rng_auto, rng_sleep, rng_activity, rng_device, rng_energy, rng_weight,
    ) -> list[DayRecord]:
        n = cfg.total_days
        records: list[DayRecord] = []
        durations: list[float] = []

        # Скрытые состояния.
        z = rng_auto.gauss(0.0, 1.0)
        auto_innovation_sd = math.sqrt(1.0 - AUTONOMIC_PHI ** 2)
        steps_noise = rng_activity.gauss(0.0, rh.STEPS_SD)
        steps_innovation_sd = rh.STEPS_SD * math.sqrt(1.0 - STEPS_AR_PHI ** 2)
        alpha = ev.fitness_alpha()
        fitness = sum(rh.WORKOUT_BY_WEEKDAY) / 7.0    # старт с равновесия
        fitness_ref = fitness
        weight = base.weight_kg

        for d in range(n):
            weekday = weekdays[d]
            z = AUTONOMIC_PHI * z + rng_auto.gauss(0.0, auto_innovation_sd)

            effect = (
                ev.alcohol_effect(d, alcohol_by_day)
                .merge(ev.illness_effect(d))
                .merge(ev.detraining_effect(d))
                .merge(ev.short_sleep_effect(d, weekday))
                .merge(ev.travel_effect(d))
            )
            day_tz_name = ev.travel_timezone(d, cfg.timezone)
            day_tz = ZoneInfo(day_tz_name)

            # --- сон ---------------------------------------------------
            onset_t, duration_t, efficiency_t = rh.sleep_targets(weekday)
            onset = onset_t + effect.sleep_onset + rng_sleep.gauss(0.0, SLEEP_ONSET_SD)
            duration = (
                duration_t
                + effect.sleep_duration
                + rng_sleep.gauss(0.0, PHYSIO_SLEEP_DURATION_SD)
                + rng_device.gauss(0.0, DEVICE_SLEEP_DURATION_SD)
            )
            duration = max(duration, 120.0)
            efficiency = min(max(
                efficiency_t + effect.sleep_efficiency + rng_sleep.gauss(0.0, SLEEP_EFFICIENCY_SD),
                50.0), 99.0)
            offset = onset + duration / (efficiency / 100.0)

            durations.append(duration)
            debt = ev.sleep_debt_hours(durations, base.sleep_need_min)
            excess_debt = ev.excess_debt_hours(debt)

            # --- нагрузка и форма --------------------------------------
            load_t = rh.workout_target(weekday) * effect.load_mult
            load = max(0.0, load_t + (rng_activity.gauss(0.0, rh.WORKOUT_SD) if load_t > 0 else 0.0))
            fitness += alpha * (load - fitness)
            fitness_gap = fitness - fitness_ref

            # --- пульс покоя и вариабельность --------------------------
            rhr = (
                base.rhr
                + rh.rhr_offset(weekday)
                + AUTONOMIC_RHR_LOADING * z
                + effect.rhr
                - ev.FITNESS_RHR_GAIN * fitness_gap
                + ev.SLEEP_DEBT_RHR_PER_HOUR * excess_debt
                + rng_auto.gauss(0.0, PHYSIO_RHR_SD)
                + rng_device.gauss(0.0, DEVICE_RHR_SD)
            )
            log_rmssd = (
                base.log_rmssd
                + AUTONOMIC_LOG_RMSSD_LOADING * z
                + effect.log_rmssd
                + ev.FITNESS_LOG_RMSSD_GAIN * fitness_gap
                + ev.SLEEP_DEBT_LOG_RMSSD_PER_HOUR * excess_debt
                + rng_auto.gauss(0.0, PHYSIO_LOG_RMSSD_SD)
                + rng_device.gauss(0.0, DEVICE_LOG_RMSSD_SD)
            )
            rmssd = math.exp(log_rmssd)

            respiratory = (
                base.respiratory_rate
                + AUTONOMIC_RESP_LOADING * z
                + effect.respiratory
                + rng_device.gauss(0.0, RESP_SD)
            )
            temp_deviation = effect.temp_deviation + rng_device.gauss(0.0, TEMP_SD)

            # --- активность --------------------------------------------
            steps_noise = (STEPS_AR_PHI * steps_noise
                           + rng_activity.gauss(0.0, steps_innovation_sd))
            steps = max(0.0, (rh.steps_target(weekday) + steps_noise) * effect.steps_mult)
            active_energy = max(0.0,
                ACTIVE_ENERGY_PER_STEP * steps
                + ACTIVE_ENERGY_PER_LOAD * load
                + rng_activity.gauss(0.0, ACTIVE_ENERGY_SD))
            hr_max_fraction = HRMAX_TRAINING_FRACTION if load > 0 else HRMAX_REST_FRACTION
            hr_max_daily = base.hr_max * hr_max_fraction + rng_activity.gauss(0.0, 5.0)

            # --- самоотчёт ---------------------------------------------
            energy = (
                ENERGY_INTERCEPT
                + ENERGY_PER_RHR * (rhr - base.rhr)
                + ENERGY_PER_LOG_RMSSD * (log_rmssd - base.log_rmssd)
                + effect.energy
                + rng_energy.gauss(0.0, ENERGY_SD)
            )
            energy_score = min(max(round(energy), 1), 5)

            weight += WEIGHT_DRIFT_KG / n + rng_weight.gauss(0.0, WEIGHT_SD) * 0.35

            rec = DayRecord(day=d, day_date=dates[d], weekday=weekday, debt_hours=debt,
                            fitness=fitness, timezone=day_tz_name)
            # Полночь МЕСТНАЯ: во время поездки это полночь другого пояса, и
            # именно от неё отсчитывается sleep.onset.
            midnight = datetime.combine(dates[d], datetime.min.time(), tzinfo=day_tz)
            rec.sleep_start = midnight + timedelta(minutes=onset)
            rec.sleep_end = midnight + timedelta(minutes=offset)
            rec.values = {
                "hr.resting": round(rhr, 1),
                "hr.max_daily": round(hr_max_daily, 0),
                "hrv.rmssd": round(rmssd, 1),
                "respiratory.rate": round(respiratory, 1),
                "body.temp_deviation": round(temp_deviation, 2),
                "sleep.duration": round(duration, 0),
                "sleep.efficiency": round(efficiency, 1),
                "sleep.onset": round(onset, 0),
                "sleep.offset": round(offset, 0),
                "activity.steps": round(steps, 0),
                "activity.active_energy": round(active_energy, 0),
                "workout.load": round(load, 1),
                "context.alcohol_units": alcohol_by_day.get(d, 0.0),
                "subjective.energy": float(energy_score),
                "body.weight": round(weight, 1),
            }
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    def _emit(self, cfg, records, panels, rng_missing, rng_ring) -> list[Observation]:
        """Разложить сутки в наблюдения и наложить пропуски.

        Пропуск выражается тем, что наблюдение не порождается вовсе — ни нулём,
        ни null (решение 4 канонического формата).
        """
        device_source = Source(vendor=VENDOR, adapter_version=ADAPTER_VERSION, device="generator")
        lab_source = Source(vendor=LAB_VENDOR, adapter_version=ADAPTER_VERSION, device="synthetic-lab")
        ring_source = Source(vendor=RING_VENDOR, adapter_version=ADAPTER_VERSION, device="sber-ring")
        panels_by_day = {p.day: p for p in panels}
        out: list[Observation] = []

        for rec in records:
            wearable_missing = (
                rec.day in FORCED_GAP_DAYS or rng_missing.random() < WEARABLE_DROP_PROB
            )
            log_weight = rng_missing.random() < WEIGHT_LOG_PROB
            log_energy = rng_missing.random() < ENERGY_LOG_PROB

            for metric, value in rec.values.items():
                if metric in ("sleep.duration", "sleep.efficiency", "sleep.onset", "sleep.offset",
                              "hr.resting", "hrv.rmssd", "respiratory.rate", "body.temp_deviation",
                              "activity.steps", "activity.active_energy", "hr.max_daily",
                              "workout.load"):
                    if wearable_missing:
                        continue
                elif metric == "body.weight" and not log_weight:
                    continue
                elif metric == "subjective.energy" and not log_energy:
                    continue

                # Ночным метрикам ставим интервал сна: он и есть окно измерения.
                nightly = metric in ("hrv.rmssd", "respiratory.rate", "body.temp_deviation",
                                     "hr.resting", "sleep.duration", "sleep.efficiency")
                out.append(Observation(
                    subject_id=cfg.subject_id,
                    metric=metric,
                    value=value,
                    effective_date=rec.day_date,
                    timezone=rec.timezone,
                    source=device_source,
                    effective_start=rec.sleep_start if nightly else None,
                    effective_end=rec.sleep_end if nightly else None,
                    confidence=0.93 if nightly else None,
                ))

            # P-10: второй вендор за тот же день. Ключ дедупликации включает
            # вендора, поэтому это не дубль, а именно тот конфликт, ради которого
            # в формате есть приоритет источников (решение 6).
            if not wearable_missing and rng_ring.random() < RING_LOG_PROB:
                out.append(Observation(
                    subject_id=cfg.subject_id,
                    metric="hr.resting",
                    value=round(rec.values["hr.resting"]
                                + RING_RHR_OFFSET
                                + rng_ring.gauss(0.0, RING_RHR_OFFSET_SD), 1),
                    effective_date=rec.day_date,
                    timezone=rec.timezone,
                    source=ring_source,
                    confidence=0.88,
                ))

            panel = panels_by_day.get(rec.day)
            if panel is not None:
                for metric, value in panel.values.items():
                    out.append(Observation(
                        subject_id=cfg.subject_id,
                        metric=metric,
                        value=value,
                        effective_date=rec.day_date,
                        timezone=rec.timezone,
                        source=lab_source,
                        confidence=0.99,
                    ))

        out.sort(key=lambda o: (o.effective_date, o.metric, o.source.vendor))
        return out

    # ------------------------------------------------------------------
    def _manifest(self, cfg, base, dates, observations) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for obs in observations:
            counts[obs.metric] = counts.get(obs.metric, 0) + 1
        n = cfg.total_days
        return {
            "schema_version": "1.0",
            "subject_id": cfg.subject_id,
            "subject": {
                "age": cfg.age,
                "timezone": cfg.timezone,
                "baselines": {
                    "hr.resting": round(base.rhr, 2),
                    "hrv.rmssd": round(base.rmssd, 2),
                    "hr.max": round(base.hr_max, 1),
                    "sleep_need_min": round(base.sleep_need_min, 1),
                    "respiratory.rate": round(base.respiratory_rate, 2),
                },
            },
            "period": {
                "start": dates[0].isoformat(),
                "end": dates[-1].isoformat(),
                "days": n,
                "weeks": cfg.weeks,
            },
            "generator": {"name": "synth", "version": ADAPTER_VERSION, "seed": cfg.seed},
            "metrics": [
                {
                    "metric": m,
                    "unit": METRICS[m].unit,
                    "count": counts[m],
                    "coverage": round(counts[m] / n, 4),
                }
                for m in sorted(counts)
            ],
            "total_observations": len(observations),
        }

    # ------------------------------------------------------------------
    def _ground_truth(self, cfg, base, records, panels, alcohol_by_day) -> dict[str, Any]:
        """Ключ ответов. НЕ часть канонического формата — метаданные стенда."""
        n = cfg.total_days
        day_to_date = {r.day: r.day_date.isoformat() for r in records}
        inflamed = [p for p in panels if p.inflamed]
        return {
            "schema_version": "1.0",
            "subject_id": cfg.subject_id,
            "source_document": "expected-patterns.md",
            "note": "Ключ ответов для оценки аналитики. Аналитика не должна его читать.",
            "patterns": [
                {
                    "id": "P-01", "name": "Недельный ритм и социальный джетлаг",
                    "late_night_wake_days": list(rh.LATE_NIGHT_WAKE_DAYS),
                    "sleep_onset_weekend_shift_min": rh.SLEEP_ONSET_WEEKEND_SHIFT,
                    "sleep_duration_weekend_shift_min": rh.SLEEP_DURATION_WEEKEND_SHIFT,
                    "sleep_efficiency_weekend_shift_pp": rh.SLEEP_EFFICIENCY_WEEKEND_SHIFT,
                    "rhr_by_weekday_mon_to_sun": list(rh.RHR_BY_WEEKDAY),
                },
                {
                    "id": "P-02", "name": "Возрастные базовые линии и связка пульс↔вариабельность",
                    "age": cfg.age,
                    "expected_rhr_for_age": round(prof.expected_rhr(cfg.age), 2),
                    "expected_rmssd_for_age": round(prof.expected_rmssd(cfg.age), 2),
                    "subject_rhr_baseline": round(base.rhr, 2),
                    "subject_rmssd_baseline": round(base.rmssd, 2),
                    "autonomic_loadings": {
                        "hr.resting": AUTONOMIC_RHR_LOADING,
                        "ln(hrv.rmssd)": AUTONOMIC_LOG_RMSSD_LOADING,
                    },
                },
                {
                    "id": "P-03", "name": "Алкогольные вечера: лаговый след",
                    "lag_days": 1,
                    "evenings": [
                        {"day": d, "date": day_to_date[d], "units": u}
                        for d, u in alcohol_by_day.items()
                    ],
                },
                {
                    "id": "P-04", "name": "Окно острой болезни",
                    "acute_days": list(ev.ILLNESS_ACUTE_DAYS),
                    "acute_dates": [day_to_date[d] for d in ev.ILLNESS_ACUTE_DAYS],
                    "peak_day": ev.ILLNESS_PEAK_DAY,
                    "peak_hr_delta": ev.ILLNESS_PEAK_RHR,
                    "recovery_tau_days": ev.RECOVERY_TAU_DAYS,
                    "overshoot_days": list(ev.OVERSHOOT_DAYS),
                },
                {
                    "id": "P-05", "name": "Блок детренированности",
                    "block_days": [ev.DETRAINING_DAYS.start, ev.DETRAINING_DAYS.stop - 1],
                    "load_multiplier": ev.DETRAINING_LOAD_MULT,
                    "fitness_tau_days": ev.FITNESS_TAU_DAYS,
                    "worst_fitness_day": min(records, key=lambda r: r.fitness).day,
                },
                {
                    "id": "P-06", "name": "Накопленный недосып",
                    "window_days": ev.SLEEP_DEBT_WINDOW_DAYS,
                    "short_sleep_blocks": [
                        [ev.SHORT_SLEEP_MILD.start, ev.SHORT_SLEEP_MILD.stop - 1],
                        [ev.SHORT_SLEEP_MAIN.start, ev.SHORT_SLEEP_MAIN.stop - 1],
                    ],
                    "same_day_duration_coefficient": 0.0,
                    "max_debt_hours": round(max(r.debt_hours for r in records), 2),
                },
                {
                    "id": "P-07", "name": "Связность лабораторного ряда и искажённый замер",
                    "draw_days": [p.day for p in panels],
                    "draw_dates": [day_to_date[p.day] for p in panels],
                    "confounded_days": [p.day for p in inflamed],
                    "ferritin_true_at_draws": [
                        round(lab.ferritin_true_at(p.day, n), 1) for p in panels
                    ],
                    "flat_markers": ["lab.hba1c", "lab.tsh"],
                },
                {
                    "id": "P-09", "name": "Смена часового пояса в поездке",
                    "home_timezone": cfg.timezone,
                    "away_timezone": ev.TRAVEL_TIMEZONE,
                    "abroad_days": list(ev.TRAVEL_ABROAD_DAYS),
                    "abroad_dates": [day_to_date[d] for d in ev.TRAVEL_ABROAD_DAYS],
                    "return_days": list(ev.TRAVEL_RETURN_DAYS),
                    "east_profile": list(ev.TRAVEL_EAST_PROFILE),
                    "west_profile": list(ev.TRAVEL_WEST_PROFILE),
                    "asymmetry": "перелёт на восток переносится тяжелее возврата на запад",
                },
                {
                    "id": "P-10", "name": "Негативные контроли",
                    "empty_week_days": list(ev.NEGATIVE_CONTROL_WEEK),
                    "empty_week_dates": [day_to_date[d] for d in ev.NEGATIVE_CONTROL_WEEK],
                    "steps_ar_phi": STEPS_AR_PHI,
                    "second_vendor": RING_VENDOR,
                    "second_vendor_rhr_offset": RING_RHR_OFFSET,
                    "second_vendor_is_calibration_not_physiology": True,
                },
                {
                    "id": "P-08", "name": "Порог различимости",
                    "mechanism": "MCAR",
                    "wearable_drop_prob": WEARABLE_DROP_PROB,
                    "forced_gap_days": list(FORCED_GAP_DAYS),
                    "resolution_threshold_bpm": 1.5,
                },
            ],
        }
