"""Расчёт доменов Индекса зрелости здоровья: Р (регулярность) и У (устойчивость).

Весь модуль — чистые детерминированные функции над списком DailyRecord.
Никакой случайности, никакого времени, никаких обращений к модели: одинаковый
вход всегда даёт побитово одинаковый выход.

================================================================================
ОБЩАЯ ШКАЛА БАЛЛОВ
================================================================================
Оба домена сначала считаются как сырое значение raw в [0, 1], а затем
переводятся в балл 1..5 по одной и той же таблице порогов:

    raw >= 0.85  ->  5   зрелая практика: держится сама, без внешнего усилия
    raw >= 0.70  ->  4   устойчиво, с отдельными просадками
    raw >= 0.55  ->  3   работает, но требует внимания
    raw >= 0.40  ->  2   держится эпизодически
    иначе        ->  1   практики фактически не выстроены

Единая таблица порогов для обоих доменов — сознательное решение: баллы разных
доменов должны быть сопоставимы между собой, иначе итоговый индекс не
складывается.

Если домен посчитать нельзя (мало данных, нет плана, не наблюдалось ни одного
срыва), возвращается score=None с полем reason. Подставлять в такой ситуации
5 баллов нельзя: отсутствие наблюдения — это не доказательство зрелости.
"""

from __future__ import annotations

import statistics
from typing import Optional, Sequence

from hmi.model import (
    DAYS_IN_WEEK,
    DailyRecord,
    DomainResult,
    split_into_weeks,
    window_adherence,
)

# --- Общая шкала --------------------------------------------------------------

SCORE_THRESHOLDS: tuple[tuple[float, int], ...] = (
    (0.85, 5),
    (0.70, 4),
    (0.55, 3),
    (0.40, 2),
)
_EPS = 1e-9  # чтобы raw ровно на границе (0.70) не проваливался вниз из-за float


def score_from_raw(raw: float) -> int:
    """Перевод сырого значения [0, 1] в балл 1..5 по таблице SCORE_THRESHOLDS."""
    for threshold, score in SCORE_THRESHOLDS:
        if raw >= threshold - _EPS:
            return score
    return 1


# ==============================================================================
# ДОМЕН Р — РЕГУЛЯРНОСТЬ
# ==============================================================================
#
# Вопрос домена: насколько устойчиво человек соблюдает ритм практик на окне
# 8-12 недель.
#
# Окно наблюдения: последние R_WINDOW_WEEKS_MAX полных недель (по умолчанию 12).
# Если полных недель меньше R_WINDOW_WEEKS_MIN (8) — домен не считается.
#
# Пусть adh_w — доля выполнения плана в неделю w (см. window_adherence):
#
#     adh_w = min(1, выполнено_w / запланировано_w)
#
# ФОРМУЛА:
#
#     A = mean(adh_w)                              объём: сколько плана реально сделано
#     S = 1 - min(1, pstdev(adh_w) / R_SPREAD_CAP) ровность: насколько недели похожи
#     D = доля недель с adh_w >= R_WEEK_ALIVE      покрытие: сколько недель «состоялись»
#
#     R_raw = 0.50*A + 0.30*S + 0.20*D
#
# Почему так, а не просто A: два человека с одинаковым средним 0.70 — один
# делает 70% каждую неделю, другой чередует 100% и 40%. Это разная зрелость,
# и разводит их именно S. Компонент D добивает случай «месяц идеально, месяц
# пусто»: там и A, и S могут выглядеть терпимо, а мёртвых недель половина.
#
# КОНСТАНТЫ И ПОРОГИ ДОМЕНА Р:
#   R_SPREAD_CAP = 0.35  — разброс недель, при котором компонент ровности
#                          обнуляется полностью. 0.35 ≈ регулярное чередование
#                          «неделя на 100% / неделя на 30%».
#   R_WEEK_ALIVE = 0.60  — неделя считается состоявшейся, если сделано >= 60%
#                          плана. Ниже этого ритм на неделе фактически потерян.
#   Веса 0.50 / 0.30 / 0.20 — экспертные, подобраны так, чтобы объём оставался
#                          главным, но не единственным фактором. Вынесены в
#                          константы: их можно перекалибровать на реальных
#                          данных, не трогая логику.

R_WINDOW_WEEKS_MAX = 12
R_WINDOW_WEEKS_MIN = 8
R_SPREAD_CAP = 0.35
R_WEEK_ALIVE = 0.60
R_WEIGHTS = {"adherence": 0.50, "stability": 0.30, "coverage": 0.20}


def compute_domain_r(
    timeline: Sequence[DailyRecord],
    window_weeks: int = R_WINDOW_WEEKS_MAX,
) -> DomainResult:
    """Домен Р (регулярность), балл 1..5. Формула — в шапке секции выше."""
    if window_weeks < R_WINDOW_WEEKS_MIN or window_weeks > R_WINDOW_WEEKS_MAX:
        raise ValueError(
            f"окно домена Р должно быть {R_WINDOW_WEEKS_MIN}..{R_WINDOW_WEEKS_MAX} "
            f"недель, получено {window_weeks}"
        )

    weeks = split_into_weeks(timeline)
    if len(weeks) < R_WINDOW_WEEKS_MIN:
        return DomainResult(
            domain="Р",
            score=None,
            raw=None,
            reason="insufficient_data",
            diagnostics={
                "full_weeks": len(weeks),
                "required_weeks": R_WINDOW_WEEKS_MIN,
            },
        )

    # Берём последние window_weeks недель: домен описывает текущий ритм,
    # а не историю за всё время наблюдения.
    weeks = weeks[-window_weeks:]

    weekly = [window_adherence(w) for w in weeks]
    weekly = [a for a in weekly if a is not None]  # недели без плана не считаются
    if len(weekly) < R_WINDOW_WEEKS_MIN:
        return DomainResult(
            domain="Р",
            score=None,
            raw=None,
            reason="no_plan",
            diagnostics={"weeks_with_plan": len(weekly)},
        )

    adherence = statistics.fmean(weekly)
    spread = statistics.pstdev(weekly)  # pstdev: недели окна — вся совокупность
    stability = 1.0 - min(1.0, spread / R_SPREAD_CAP)
    coverage = sum(1 for a in weekly if a >= R_WEEK_ALIVE - _EPS) / len(weekly)

    raw = (
        R_WEIGHTS["adherence"] * adherence
        + R_WEIGHTS["stability"] * stability
        + R_WEIGHTS["coverage"] * coverage
    )

    return DomainResult(
        domain="Р",
        score=score_from_raw(raw),
        raw=raw,
        components={
            "A_adherence": adherence,
            "S_stability": stability,
            "D_coverage": coverage,
        },
        diagnostics={
            "weeks_used": len(weekly),
            "weekly_adherence": [round(a, 4) for a in weekly],
            "weekly_spread": spread,
        },
    )


# ==============================================================================
# ДОМЕН У — УСТОЙЧИВОСТЬ
# ==============================================================================
#
# Вопрос домена: как быстро человек возвращается к своей базовой линии после
# срыва режима или командировки.
#
# ШАГ 1. БАЗОВАЯ ЛИНИЯ.
#   baseline = median(adh_w по «спокойным» неделям)
#   Спокойная неделя — неделя без пометок контекста (командировка, болезнь).
#   Медиана, а не среднее: отдельный провал не должен занижать планку, к
#   которой человек возвращается. Если спокойных недель меньше
#   U_MIN_CALM_WEEKS (3), базовая линия берётся как медиана лучшей половины
#   всех недель — это грубее, поэтому факт фиксируется в diagnostics.
#   Если baseline < U_MIN_BASELINE (0.35), домен не считается: возвращаться
#   человеку некуда, ритма как такового нет.
#
# ШАГ 2. ДЕТЕКЦИЯ ЭПИЗОДОВ СРЫВА.
#   Скользящее окно длиной 7 дней, шаг 1 день. Окно, начинающееся в день s,
#   считается провальным, если
#       adh(s..s+6) < U_DIP_RATIO * baseline      (U_DIP_RATIO = 0.60)
#   Эпизод — максимальная серия подряд идущих провальных окон. Началом эпизода
#   считается первый день первого провального окна.
#
# ШАГ 3. ВРЕМЯ ВОЗВРАТА (TTR, time-to-recover).
#   TTR = (первый день d >= начала эпизода, для которого
#          adh(d..d+6) >= U_RECOVERED_RATIO * baseline) - начало эпизода
#   U_RECOVERED_RATIO = 0.85: возвратом считается не «стало чуть лучше»,
#   а выход на 85% своей базовой линии.
#   Если такого дня в данных нет, эпизод цензурируется: TTR = U_TTR_MAX (28
#   дней) и recovered=False. Цензура консервативная: незавершённый возврат
#   штрафуется как самый медленный из наблюдаемых. Число таких эпизодов
#   попадает в diagnostics["censored_episodes"], чтобы балл можно было
#   перепроверить, когда данных станет больше.
#   Эпизоды не пересекаются: следующий поиск начинается после точки возврата.
#
# ФОРМУЛА:
#
#     V = 1 - median(TTR) / U_TTR_MAX     скорость возврата
#     C = восстановленные / все эпизоды   доводит ли человек возврат до конца
#     G = 1 - mean(depth)                 мелкость провала,
#         где depth = clamp(1 - adh_дна / baseline, 0, 1)
#
#     U_raw = 0.55*V + 0.25*C + 0.20*G
#
# Медиана TTR, а не среднее: одна затяжная болезнь не должна определять оценку
# устойчивости целиком.
#
# КОНСТАНТЫ И ПОРОГИ ДОМЕНА У:
#   U_DIP_RATIO       = 0.60  срыв: провал ниже 60% базовой линии
#   U_RECOVERED_RATIO = 0.85  возврат: выход обратно на 85% базовой линии
#   U_TTR_MAX         = 28    4 недели — потолок и одновременно значение для
#                             цензурированных эпизодов; возврат дольше месяца
#                             по смыслу домена уже не «возврат»
#   U_MIN_BASELINE    = 0.35  ниже этого базовой линии нет
#   U_MIN_CALM_WEEKS  = 3     минимум недель для надёжной базовой линии
#   Веса 0.55 / 0.25 / 0.20 — экспертные, скорость возврата главная.
#
# ОТДЕЛЬНЫЙ СЛУЧАЙ: ЭПИЗОДОВ НЕ БЫЛО.
#   Устойчивость не наблюдалась — стресс-теста просто не случилось. Ставить 5
#   баллов нельзя (это оценка везения, а не зрелости), ставить 1 тем более.
#   Возвращается score=None, reason="no_disruption_episodes".

U_WINDOW_DAYS = DAYS_IN_WEEK
U_DIP_RATIO = 0.60
U_RECOVERED_RATIO = 0.85
U_TTR_MAX = 28
U_MIN_BASELINE = 0.35
U_MIN_CALM_WEEKS = 3
U_WEIGHTS = {"speed": 0.55, "completion": 0.25, "shallowness": 0.20}

_CONTEXT_DISRUPTORS = ("travel", "illness")


def _rolling_adherence(
    ordered: Sequence[DailyRecord],
) -> list[Optional[float]]:
    """adh скользящего окна U_WINDOW_DAYS для каждого дня старта s.

    Индекс списка = день начала окна. Длина = len(ordered) - U_WINDOW_DAYS + 1.
    None означает, что в окне не было плана.
    """
    n = len(ordered)
    return [
        window_adherence(ordered[s : s + U_WINDOW_DAYS])
        for s in range(n - U_WINDOW_DAYS + 1)
    ]


def _baseline(
    weeks: list[list[DailyRecord]],
) -> tuple[Optional[float], str]:
    """Базовая линия и способ, которым она получена."""
    calm = []
    for week in weeks:
        flagged = any(
            flag in _CONTEXT_DISRUPTORS for day in week for flag in day.context
        )
        adh = window_adherence(week)
        if not flagged and adh is not None:
            calm.append(adh)

    if len(calm) >= U_MIN_CALM_WEEKS:
        return statistics.median(calm), "calm_weeks"

    # Фолбэк: спокойных недель мало — берём медиану лучшей половины всех недель.
    all_weeks = [a for a in (window_adherence(w) for w in weeks) if a is not None]
    if not all_weeks:
        return None, "no_plan"
    top_half = sorted(all_weeks)[len(all_weeks) // 2 :]
    return statistics.median(top_half), "top_half_fallback"


def compute_domain_u(timeline: Sequence[DailyRecord]) -> DomainResult:
    """Домен У (устойчивость), балл 1..5. Формула — в шапке секции выше."""
    ordered = sorted(timeline, key=lambda r: r.day)
    weeks = split_into_weeks(ordered)

    if len(weeks) < R_WINDOW_WEEKS_MIN:
        return DomainResult(
            domain="У",
            score=None,
            raw=None,
            reason="insufficient_data",
            diagnostics={
                "full_weeks": len(weeks),
                "required_weeks": R_WINDOW_WEEKS_MIN,
            },
        )

    baseline, baseline_source = _baseline(weeks)
    if baseline is None:
        return DomainResult(
            domain="У", score=None, raw=None, reason="no_plan"
        )
    if baseline < U_MIN_BASELINE:
        return DomainResult(
            domain="У",
            score=None,
            raw=None,
            reason="no_baseline",
            diagnostics={"baseline": baseline, "baseline_source": baseline_source},
        )

    rolling = _rolling_adherence(ordered)
    dip_level = U_DIP_RATIO * baseline
    recovered_level = U_RECOVERED_RATIO * baseline

    episodes: list[dict] = []
    s = 0
    last_start = len(rolling) - 1
    while s <= last_start:
        adh = rolling[s]
        if adh is None or adh >= dip_level:
            s += 1
            continue

        # Начало эпизода: серия подряд идущих провальных окон.
        onset = s
        end = s
        trough = adh
        while end + 1 <= last_start:
            nxt = rolling[end + 1]
            if nxt is None or nxt >= dip_level:
                break
            end += 1
            trough = min(trough, nxt)

        # Возврат: первое окно на уровне >= 85% базовой линии.
        recovery_day = None
        for d in range(onset, last_start + 1):
            value = rolling[d]
            if value is not None and value >= recovered_level - _EPS:
                recovery_day = d
                break

        if recovery_day is None:
            ttr = U_TTR_MAX
            recovered = False
        else:
            ttr = min(recovery_day - onset, U_TTR_MAX)
            recovered = recovery_day - onset <= U_TTR_MAX

        depth = min(1.0, max(0.0, 1.0 - trough / baseline))
        episodes.append(
            {
                "onset_day": ordered[onset].day,
                "dip_end_day": ordered[end].day,
                "ttr_days": ttr,
                "recovered": recovered,
                "depth": round(depth, 4),
                "trough_adherence": round(trough, 4),
            }
        )

        # Эпизоды не пересекаются: продолжаем после точки возврата.
        s = max(end + 1, (recovery_day + 1) if recovery_day is not None else end + 1)

    if not episodes:
        return DomainResult(
            domain="У",
            score=None,
            raw=None,
            reason="no_disruption_episodes",
            diagnostics={
                "baseline": baseline,
                "baseline_source": baseline_source,
                "episodes": [],
            },
        )

    ttrs = [e["ttr_days"] for e in episodes]
    speed = 1.0 - min(1.0, statistics.median(ttrs) / U_TTR_MAX)
    completion = sum(1 for e in episodes if e["recovered"]) / len(episodes)
    shallowness = 1.0 - statistics.fmean(e["depth"] for e in episodes)

    raw = (
        U_WEIGHTS["speed"] * speed
        + U_WEIGHTS["completion"] * completion
        + U_WEIGHTS["shallowness"] * shallowness
    )

    return DomainResult(
        domain="У",
        score=score_from_raw(raw),
        raw=raw,
        components={
            "V_speed": speed,
            "C_completion": completion,
            "G_shallowness": shallowness,
        },
        diagnostics={
            "baseline": baseline,
            "baseline_source": baseline_source,
            "median_ttr_days": statistics.median(ttrs),
            "episodes": episodes,
            "censored_episodes": sum(1 for e in episodes if not e["recovered"]),
        },
    )
