"""События и медленные дрейфы: паттерны P-03, P-04, P-05, P-06.

Каждый блок параметров помечен ID паттерна из expected-patterns.md.
Правка любой константы здесь обязана сопровождаться правкой карточки паттерна,
иначе tools/check_patterns.py покраснеет — и это ровно то, чего мы хотим.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# P-03 · Алкогольные вечера: лаговый след
# --------------------------------------------------------------------------
ALCOHOL_EVENINGS = 11
ALCOHOL_UNITS_RANGE = (2.0, 6.0)
ALCOHOL_WEEKEND_WEIGHT = 6.0      # во сколько раз пт/сб вероятнее прочих дней
ALCOHOL_RHR_PER_UNIT = 1.1
ALCOHOL_RHR_INTERCEPT = 1.5
ALCOHOL_LOG_RMSSD = -0.32         # ≈ −27 %
ALCOHOL_SLEEP_EFFICIENCY = -4.0   # п.п.
ALCOHOL_RESPIRATORY = 1.2
ALCOHOL_ENERGY = -0.7

# --------------------------------------------------------------------------
# P-04 · Окно острой болезни
# --------------------------------------------------------------------------
ILLNESS_START_DAY = 62
ILLNESS_RHR_PROFILE = (8.0, 12.0, 10.0, 6.0)   # дни 62..65
ILLNESS_PEAK_RHR = max(ILLNESS_RHR_PROFILE)
ILLNESS_LOG_RMSSD_AT_PEAK = -0.51              # ≈ −40 %
ILLNESS_TEMP_PROFILE = (0.55, 0.85, 0.75, 0.55)   # дни 62..65, °C
ILLNESS_RESP_AT_PEAK = 2.5
ILLNESS_STEPS_MULT = 0.4                       # −60 %
ILLNESS_ENERGY_AT_PEAK = -2.5
RECOVERY_TAU_DAYS = 3.5
RECOVERY_LAST_DAY = 75
OVERSHOOT_DAYS = range(76, 83)                 # 76..82
OVERSHOOT_RHR = -1.8
OVERSHOOT_LOG_RMSSD = 0.077                    # ≈ +8 %

ILLNESS_ACUTE_DAYS = tuple(range(ILLNESS_START_DAY, ILLNESS_START_DAY + len(ILLNESS_RHR_PROFILE)))
ILLNESS_PEAK_DAY = ILLNESS_START_DAY + ILLNESS_RHR_PROFILE.index(ILLNESS_PEAK_RHR)

# --------------------------------------------------------------------------
# P-05 · Блок детренированности
# --------------------------------------------------------------------------
DETRAINING_DAYS = range(35, 56)   # 35..55, недели 6–8
DETRAINING_LOAD_MULT = 0.3
DETRAINING_STEPS_MULT = 0.75
FITNESS_TAU_DAYS = 16.0           # источник лага: форма — медленное среднее нагрузки
FITNESS_RHR_GAIN = 0.30           # уд/мин на единицу формы
FITNESS_LOG_RMSSD_GAIN = 0.0143

# --------------------------------------------------------------------------
# P-06 · Накопленный недосып
# --------------------------------------------------------------------------
SLEEP_DEBT_WINDOW_DAYS = 7
# Долг отсчитывается не от нуля, а от нормы обычной недели: при штатном режиме
# сна недельный дефицит и так составляет около 3 часов, и если считать его от
# нуля, получается постоянное смещение, из-за которого базовую линию субъекта
# уже не восстановить из данных (это ломало бы P-02). Кусается только избыток.
SLEEP_DEBT_REFERENCE_HOURS = 3.0
SLEEP_DEBT_LOG_RMSSD_PER_HOUR = -0.070
SLEEP_DEBT_RHR_PER_HOUR = 0.35
# Прямого коэффициента при СЕГОДНЯШНЕЙ длительности сна нет вовсе — в этом
# и состоит паттерн: признак нужно сконструировать, а не взять готовым.
SHORT_SLEEP_MAIN = range(84, 98)      # недели 13–14
SHORT_SLEEP_MAIN_MINUTES = -55.0      # только в будни
SHORT_SLEEP_MILD = range(14, 21)      # более ранний, слабый эпизод: даёт долгу разброс
SHORT_SLEEP_MILD_MINUTES = -35.0


# --------------------------------------------------------------------------
# P-09 · Смена часового пояса в поездке
# --------------------------------------------------------------------------
TRAVEL_TIMEZONE = "Asia/Shanghai"          # +5 часов к Europe/Moscow
TRAVEL_ABROAD_DAYS = range(25, 30)         # 25..29, дни за границей
TRAVEL_RETURN_DAYS = range(30, 35)         # 30..34, возврат и шлейф
# Профили интенсивности. Асимметрия здесь и есть паттерн: на восток к пятому дню
# адаптация ещё не наступила, на запад — почти наступила к третьему.
TRAVEL_EAST_PROFILE = (0.55, 0.90, 1.00, 0.95, 0.85)
TRAVEL_WEST_PROFILE = (0.70, 0.35, 0.15, 0.05, 0.02)
TRAVEL_RHR = 6.5
TRAVEL_LOG_RMSSD = -0.35                   # ≈ −30 %
TRAVEL_RESPIRATORY = 0.5
TRAVEL_TEMP = 0.15
TRAVEL_SLEEP_EFFICIENCY = -8.0
TRAVEL_SLEEP_ONSET = 60.0                  # по МЕСТНЫМ часам ложится позже
TRAVEL_SLEEP_DURATION = -45.0              # тело будит по домашним часам
TRAVEL_ENERGY = -0.8
TRAVEL_STEPS_MULT_FLIGHT = 1.55            # дни самих перелётов
TRAVEL_STEPS_MULT_ABROAD = 1.15
TRAVEL_FLIGHT_DAYS = (TRAVEL_ABROAD_DAYS.start, TRAVEL_RETURN_DAYS.start)

# --------------------------------------------------------------------------
# P-10 · Негативные контроли
# --------------------------------------------------------------------------
# Неделя, в которую не заложено ничего: ни события, ни дрейфа, ни алкоголя.
# Любой найденный в ней тренд — ложное срабатывание.
NEGATIVE_CONTROL_WEEK = range(7, 14)       # дни 7..13


@dataclass
class DayEffect:
    """Суммарное влияние всех событий на один день."""

    rhr: float = 0.0
    log_rmssd: float = 0.0
    sleep_efficiency: float = 0.0
    sleep_duration: float = 0.0
    sleep_onset: float = 0.0
    respiratory: float = 0.0
    temp_deviation: float = 0.0
    energy: float = 0.0
    steps_mult: float = 1.0
    load_mult: float = 1.0

    def merge(self, other: "DayEffect") -> "DayEffect":
        return DayEffect(
            rhr=self.rhr + other.rhr,
            log_rmssd=self.log_rmssd + other.log_rmssd,
            sleep_efficiency=self.sleep_efficiency + other.sleep_efficiency,
            sleep_duration=self.sleep_duration + other.sleep_duration,
            sleep_onset=self.sleep_onset + other.sleep_onset,
            respiratory=self.respiratory + other.respiratory,
            temp_deviation=self.temp_deviation + other.temp_deviation,
            energy=self.energy + other.energy,
            steps_mult=self.steps_mult * other.steps_mult,
            load_mult=self.load_mult * other.load_mult,
        )


def schedule_alcohol(n_days: int, weekday_of: list[int], rng: random.Random) -> dict[int, float]:
    """Выбрать вечера с алкоголем. Пятница и суббота заметно вероятнее прочих дней."""
    # Окна, в которые вечер с алкоголем не планируется. Причины разные, итог
    # один: след, попавший в любое из них, пришлось бы выбрасывать из сравнения,
    # и паттерн терял бы треть наблюдений.
    #   болезнь и восстановление — больной человек не пьёт;
    #   поездка — иначе P-03 и P-09 накладываются друг на друга;
    #   неделя негативного контроля — она обязана остаться пустой (P-10).
    forbidden = (set(range(ILLNESS_START_DAY - 2, OVERSHOOT_DAYS.stop))
                 | set(NEGATIVE_CONTROL_WEEK)
                 | set(TRAVEL_ABROAD_DAYS) | set(TRAVEL_RETURN_DAYS))

    def blocked(d: int) -> bool:
        # И сам вечер, и день следа обязаны быть вне запретных окон.
        return d in forbidden or d + 1 in forbidden

    weights = [
        0.0 if blocked(d)
        else ALCOHOL_WEEKEND_WEIGHT if weekday_of[d] in (4, 5)
        else 1.0
        for d in range(n_days)
    ]
    chosen: dict[int, float] = {}
    while len(chosen) < ALCOHOL_EVENINGS:
        day = rng.choices(range(n_days), weights=weights, k=1)[0]
        # Последний день исключаем: его след пришёлся бы за границу периода
        # и паттерн стал бы ненаблюдаемым.
        if day in chosen or day >= n_days - 1:
            continue
        chosen[day] = round(rng.uniform(*ALCOHOL_UNITS_RANGE) * 2) / 2  # шаг 0.5 единицы
    return dict(sorted(chosen.items()))


def alcohol_effect(day: int, alcohol_by_day: dict[int, float]) -> DayEffect:
    """След вечера дня D-1, проявляющийся на дне D. В день D эффекта нет."""
    units = alcohol_by_day.get(day - 1)
    if not units:
        return DayEffect()
    severity = units / 4.0
    return DayEffect(
        rhr=ALCOHOL_RHR_PER_UNIT * units + ALCOHOL_RHR_INTERCEPT,
        log_rmssd=ALCOHOL_LOG_RMSSD * severity,
        sleep_efficiency=ALCOHOL_SLEEP_EFFICIENCY * severity,
        respiratory=ALCOHOL_RESPIRATORY * severity,
        energy=ALCOHOL_ENERGY * severity,
    )


def illness_effect(day: int) -> DayEffect:
    """Острая фаза, затухание и перелёт через базовую линию."""
    if day in ILLNESS_ACUTE_DAYS:
        rhr = ILLNESS_RHR_PROFILE[day - ILLNESS_START_DAY]
        severity = rhr / ILLNESS_PEAK_RHR
        return DayEffect(
            rhr=rhr,
            log_rmssd=ILLNESS_LOG_RMSSD_AT_PEAK * severity,
            temp_deviation=ILLNESS_TEMP_PROFILE[day - ILLNESS_START_DAY],
            respiratory=ILLNESS_RESP_AT_PEAK * severity,
            energy=ILLNESS_ENERGY_AT_PEAK * severity,
            steps_mult=ILLNESS_STEPS_MULT,
        )

    last_acute = ILLNESS_ACUTE_DAYS[-1]
    if last_acute < day <= RECOVERY_LAST_DAY:
        decay = math.exp(-(day - last_acute) / RECOVERY_TAU_DAYS)
        tail_rhr = ILLNESS_RHR_PROFILE[-1]
        severity = tail_rhr / ILLNESS_PEAK_RHR
        return DayEffect(
            rhr=tail_rhr * decay,
            log_rmssd=ILLNESS_LOG_RMSSD_AT_PEAK * severity * decay,
            respiratory=ILLNESS_RESP_AT_PEAK * severity * decay * 0.5,
            energy=ILLNESS_ENERGY_AT_PEAK * severity * decay,
            # Возврат к активности идёт медленнее, чем уходит лихорадка.
            steps_mult=1.0 - (1.0 - ILLNESS_STEPS_MULT) * decay * 0.7,
        )

    if day in OVERSHOOT_DAYS:
        return DayEffect(rhr=OVERSHOOT_RHR, log_rmssd=OVERSHOOT_LOG_RMSSD, energy=0.4)

    return DayEffect()


def travel_timezone(day: int, home: str) -> str:
    """Часовой пояс субъекта на этот день."""
    return TRAVEL_TIMEZONE if day in TRAVEL_ABROAD_DAYS else home


def travel_effect(day: int) -> DayEffect:
    """Сдвиг пояса на восток, возврат на запад и разная тяжесть того и другого."""
    if day in TRAVEL_ABROAD_DAYS:
        intensity = TRAVEL_EAST_PROFILE[day - TRAVEL_ABROAD_DAYS.start]
        onset_shift = TRAVEL_SLEEP_ONSET
    elif day in TRAVEL_RETURN_DAYS:
        intensity = TRAVEL_WEST_PROFILE[day - TRAVEL_RETURN_DAYS.start]
        onset_shift = -TRAVEL_SLEEP_ONSET * 0.5   # дома тянет спать раньше
    else:
        return DayEffect()

    if day in TRAVEL_FLIGHT_DAYS:
        steps_mult = TRAVEL_STEPS_MULT_FLIGHT
    elif day in TRAVEL_ABROAD_DAYS:
        steps_mult = TRAVEL_STEPS_MULT_ABROAD
    else:
        steps_mult = 1.0

    return DayEffect(
        rhr=TRAVEL_RHR * intensity,
        log_rmssd=TRAVEL_LOG_RMSSD * intensity,
        sleep_efficiency=TRAVEL_SLEEP_EFFICIENCY * intensity,
        sleep_duration=TRAVEL_SLEEP_DURATION * intensity,
        sleep_onset=onset_shift * intensity,
        respiratory=TRAVEL_RESPIRATORY * intensity,
        temp_deviation=TRAVEL_TEMP * intensity,
        energy=TRAVEL_ENERGY * intensity,
        steps_mult=steps_mult,
    )


def detraining_effect(day: int) -> DayEffect:
    """Сам блок задаёт только падение нагрузки. Пульс и вариабельность реагируют
    не здесь, а через медленное состояние формы — отсюда и берётся лаг."""
    if day in DETRAINING_DAYS:
        return DayEffect(load_mult=DETRAINING_LOAD_MULT, steps_mult=DETRAINING_STEPS_MULT)
    return DayEffect()


def short_sleep_effect(day: int, weekday: int) -> DayEffect:
    """Отрезки укороченного сна, за счёт которых накапливается долг."""
    if weekday >= 5:
        return DayEffect()
    if day in SHORT_SLEEP_MAIN:
        return DayEffect(sleep_duration=SHORT_SLEEP_MAIN_MINUTES)
    if day in SHORT_SLEEP_MILD:
        return DayEffect(sleep_duration=SHORT_SLEEP_MILD_MINUTES)
    return DayEffect()


def sleep_debt_hours(durations: list[float], sleep_need_min: float) -> float:
    """Долг за последние SLEEP_DEBT_WINDOW_DAYS дней, включая сегодняшний, в часах."""
    window = durations[-SLEEP_DEBT_WINDOW_DAYS:]
    deficit = sum(max(0.0, sleep_need_min - d) for d in window)
    return deficit / 60.0


def excess_debt_hours(debt: float) -> float:
    """Избыток долга сверх нормы обычной недели — именно он влияет на метрики."""
    return debt - SLEEP_DEBT_REFERENCE_HOURS


def fitness_alpha() -> float:
    return 1.0 - math.exp(-1.0 / FITNESS_TAU_DAYS)
