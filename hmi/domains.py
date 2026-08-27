"""Расчёт доменов Индекса зрелости здоровья: Р (регулярность) и У (устойчивость).

Здесь только чистые функции: на вход — поденный дневник практик, на выход —
баллы и все промежуточные компоненты. Никаких обращений к модели, к сети, к
системному времени и к random. Одинаковый вход → одинаковый выход, всегда.

================================================================================
ДОМЕН Р — РЕГУЛЯРНОСТЬ
================================================================================

Вопрос домена: насколько устойчиво человек держит ритм практик на окне
8-12 недель. Не «сколько сделал всего», а «держится ли ритм сам по себе».

Окно: последние REG_WINDOW_WEEKS недель (12). Если данных меньше
REG_MIN_WEEKS (8) — домен не считается, возвращается None. Недели нарезаются
блоками по 7 дней от конца окна назад, чтобы последняя неделя всегда была
полной.

Три компонента, каждый нормирован в [0, 1].

  A — соблюдение плана (adherence)
      Для недели w:  a_w = min(1, sum(done_w) / sum(planned_w))
      A = среднее арифметическое a_w по всем неделям окна.
      Ограничение сверху единицей: перевыполнение плана в одну неделю не
      должно компенсировать провал в другую — это ровно то, что домен
      регулярности обязан ловить.

  S — стабильность между неделями
      CV = pstdev(a_w) / mean(a_w)      — коэффициент вариации
      S  = max(0, 1 - CV)
      Коэффициент вариации безразмерен, поэтому «0.9 ± 0.05» и «0.4 ± 0.05»
      получают разную оценку стабильности: для низкого среднего тот же
      абсолютный разброс относительно больше. Используется pstdev
      (популяционное СКО), а не stdev: у нас не выборка из генеральной
      совокупности, а все недели окна целиком, и pstdev не требует n >= 2.
      При mean(a_w) == 0 стабильность считается нулевой (делить не на что,
      и «стабильно ничего не делать» — не регулярность).

  R — отсутствие длинных провалов (rhythm)
      G = самая длинная серия подряд идущих дней с done == 0.
      R = clip(1 - (G - REG_GAP_FREE_DAYS) / REG_GAP_SPAN, 0, 1)
      При REG_GAP_FREE_DAYS = 2 и REG_GAP_SPAN = 12: провал в 1-2 дня не
      штрафуется вовсе (выходные, разгрузочный день), 8 дней подряд дают
      R = 0.5, 14 дней и больше — R = 0.
      Максимум, а не среднее по провалам: две недели тишины — это другой
      режим жизни, а не «немного хуже, чем обычно», и усреднение это прячет.

  Свёртка:
      P_raw = 0.50*A + 0.30*S + 0.20*R

      Веса: соблюдение плана — основа домена, поэтому половина. Стабильность
      весит больше провалов, потому что рваный ритм разрушает регулярность
      системнее, чем один локальный провал. Провалы всё же вынесены в
      отдельный компонент: недельная агрегация A и S размазывает пятидневную
      тишину внутри недели, а R её видит.

  Пороги (REG_THRESHOLDS), балл 1-5:
      P_raw >= 0.85  → 5   ритм держится сам, без усилия
      P_raw >= 0.70  → 4   стабильно, редкие сбои
      P_raw >= 0.55  → 3   держится, но требует контроля
      P_raw >= 0.35  → 2   рваный ритм
      иначе          → 1   ритма нет

================================================================================
ДОМЕН У — УСТОЙЧИВОСТЬ
================================================================================

Вопрос домена: как быстро человек возвращается к своей базовой линии после
срыва режима или командировки. Домен меряет не глубину провала (уехать в
командировку и не тренироваться — нормально), а скорость и полноту возврата.

  B — базовая линия
      Считается для каждого эпизода отдельно: медиана дневного adherence =
      done/planned по последним RES_BASELINE_DAYS (14) «чистым» дням
      (disruption == none), предшествующим эпизоду.

      Именно локальная база, а не медиана по всему окну. Иначе метрика
      ломается на самом важном случае: человек сорвался и больше не вернулся,
      его общая база из-за этого просела, и «возврат к базе» засчитывается
      мгновенно — тем быстрее, чем хуже он живёт после срыва. База должна
      быть той, что была до эпизода, иначе домен поощряет деградацию.

      Медиана, а не среднее: она не смещается от отдельных выбросов, а база
      должна отражать типичный день, а не хвосты. Если чистых дней перед
      эпизодом меньше RES_BASELINE_MIN_DAYS (7) — берётся общая база по окну
      (для первого эпизода, случившегося в самом начале наблюдения).
      Если база равна нулю — домен не считается: возвращаться некуда.

  Эпизод — непрерывный отрезок дней с disruption != none.
      Эпизод оценивается, только если после его конца в окне осталось не
      меньше RES_MIN_FOLLOWUP_DAYS (14) дней. Иначе по нему нельзя отличить
      быстрый возврат от медленного, и он исключается из расчёта (но
      попадает в отчёт как неоценённый).

  TTR — время возврата, дни
      От первого дня после конца эпизода ищем самый ранний день t, для
      которого среднее adherence на отрезке [t, t + RES_RECOVERY_WINDOW) не
      ниже RES_RECOVERY_RATIO * B. TTR = t - (день конца эпизода).
      Окно в RES_RECOVERY_WINDOW = 5 дней вперёд, а не одна точка: иначе
      один удачный день после недели простоя засчитался бы как возврат.
      Порог 0.80*B, а не B: возврат — это «снова в своём режиме», а не
      «идеально как раньше».
      Если возврата не случилось за RES_HORIZON_DAYS (21 день) или до конца
      доступных данных — эпизод помечается censored, TTR = RES_HORIZON_DAYS.

      r_i = clip(1 - (TTR_i - RES_TTR_FREE_DAYS) / RES_TTR_SPAN, 0, 1)
      При RES_TTR_FREE_DAYS = 2 и RES_TTR_SPAN = 16: возврат за 1-2 дня даёт
      r = 1.0, за 10 дней — 0.5, за 18 и больше — 0.

  C — полнота возврата
      Среднее adherence за RES_CONSOLIDATION_DAYS (14) дней начиная с дня
      возврата, делённое на B и обрезанное единицей.
      Отделяет «вернулся» от «вернулся, но осел на уровень ниже прежнего»:
      первое — устойчивость, второе — медленная эрозия режима.
      Для censored-эпизодов берутся те же 14 дней сразу после эпизода.

  Свёртка:
      U_raw = 0.70*median(r_i) + 0.30*mean(C_i)

      Медиана по r_i: одна затяжная катастрофа (болезнь на три недели) не
      должна определять весь домен — важно типичное поведение. По C_i
      наоборот среднее: там выбросов по построению нет (величина уже
      обрезана в [0, 1]), а среднее чувствительнее к системному недовозврату.

  Пороги (RES_THRESHOLDS) — те же границы, что у Р, ради сопоставимости
  доменов между собой. В скобках — примерный эквивалент по медианному TTR
  при полном возврате (C = 1):
      U_raw >= 0.85  → 5   возврат за ~3 дня
      U_raw >= 0.70  → 4   возврат за ~4-6 дней
      U_raw >= 0.55  → 3   возврат за ~7-10 дней
      U_raw >= 0.35  → 2   возврат за ~11-14 дней
      иначе          → 1   больше 14 дней или возврата нет

  Если в окне нет ни одного оцениваемого эпизода — домен возвращает None,
  а не 5. Отсутствие срывов не есть доказанная устойчивость: непроверенный
  режим просто не даёт оснований для балла.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from hmi.generate import NO_DISRUPTION

# --- Пороги и веса домена Р --------------------------------------------------

REG_WINDOW_WEEKS = 12
REG_MIN_WEEKS = 8
REG_GAP_FREE_DAYS = 2  # провал такой длины не штрафуется
REG_GAP_SPAN = 12  # ещё столько дней провала обнуляют компонент R
REG_WEIGHTS = {"adherence": 0.50, "stability": 0.30, "rhythm": 0.20}

# --- Пороги и веса домена У --------------------------------------------------

RES_WINDOW_DAYS = REG_WINDOW_WEEKS * 7  # окно то же, но без недельной сетки
RES_BASELINE_DAYS = 14  # чистых дней перед эпизодом формируют его базовую линию
RES_BASELINE_MIN_DAYS = 7  # меньше — берём общую базу по окну
RES_RECOVERY_WINDOW = 5  # дней вперёд, по которым подтверждается возврат
RES_RECOVERY_RATIO = 0.80  # доля базовой линии, считающаяся возвратом
RES_HORIZON_DAYS = 21  # дальше возврат уже не ищем
RES_MIN_FOLLOWUP_DAYS = 14  # минимум данных после эпизода, иначе не оцениваем
RES_CONSOLIDATION_DAYS = 14  # окно, на котором проверяется полнота возврата
RES_TTR_FREE_DAYS = 2  # возврат за столько дней — это максимум балла
RES_TTR_SPAN = 16  # ещё столько дней обнуляют компонент скорости
RES_WEIGHTS = {"speed": 0.70, "completeness": 0.30}

# --- Общая шкала 1-5 ---------------------------------------------------------

# (нижняя граница включительно, балл). Проверяются сверху вниз.
SCORE_THRESHOLDS: tuple[tuple[float, int], ...] = (
    (0.85, 5),
    (0.70, 4),
    (0.55, 3),
    (0.35, 2),
    (0.00, 1),
)
REG_THRESHOLDS = SCORE_THRESHOLDS
RES_THRESHOLDS = SCORE_THRESHOLDS


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _score_from_raw(raw: float, thresholds=SCORE_THRESHOLDS) -> int:
    for lower_bound, score in thresholds:
        if raw >= lower_bound:
            return score
    return 1


@dataclass(frozen=True)
class Day:
    """Один день дневника практик."""

    date: str
    planned: int
    done: int
    disruption: str

    @property
    def adherence(self) -> float:
        """Доля выполненного плана за день, обрезанная единицей."""
        if self.planned <= 0:
            return 0.0
        return _clip(self.done / self.planned)

    @property
    def disrupted(self) -> bool:
        return self.disruption != NO_DISRUPTION


@dataclass(frozen=True)
class RegularityResult:
    score: int | None
    raw: float | None
    adherence: float | None
    stability: float | None
    rhythm: float | None
    weeks: int
    longest_gap: int
    weekly_adherence: tuple[float, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class EpisodeResult:
    kind: str
    start_date: str
    end_date: str
    length_days: int
    baseline: float | None
    ttr_days: int | None
    censored: bool
    speed: float | None
    completeness: float | None
    evaluated: bool
    note: str = ""


@dataclass(frozen=True)
class ResilienceResult:
    score: int | None
    raw: float | None
    baseline: float | None  # база по всему окну; используется как запасная для эпизодов
    median_ttr: float | None
    speed: float | None
    completeness: float | None
    episodes: tuple[EpisodeResult, ...] = field(default=())
    note: str = ""


def rows_to_days(rows: list[dict[str, object]]) -> list[Day]:
    """Превращает строки CSV в дни, отсортированные по дате."""
    days = [
        Day(
            date=str(row["date"]),
            planned=int(row["planned"]),
            done=int(row["done"]),
            disruption=str(row["disruption"]),
        )
        for row in rows
    ]
    return sorted(days, key=lambda day: day.date)


def _window(days: list[Day], weeks: int) -> list[Day]:
    """Последние `weeks` полных недель, нарезанные от конца данных."""
    if weeks <= 0:
        return []
    return days[-weeks * 7 :]


def _longest_zero_streak(days: list[Day]) -> int:
    longest = 0
    current = 0
    for day in days:
        current = current + 1 if day.done == 0 else 0
        longest = max(longest, current)
    return longest


# =============================================================================
# ДОМЕН Р
# =============================================================================


def domain_regularity(days: list[Day]) -> RegularityResult:
    """Домен Р: регулярность. Формула и пороги описаны в докстринге модуля."""
    full_weeks = len(days) // 7
    if full_weeks < REG_MIN_WEEKS:
        return RegularityResult(
            score=None,
            raw=None,
            adherence=None,
            stability=None,
            rhythm=None,
            weeks=full_weeks,
            longest_gap=0,
            note=f"нужно минимум {REG_MIN_WEEKS} полных недель, есть {full_weeks}",
        )

    weeks_used = min(full_weeks, REG_WINDOW_WEEKS)
    window = _window(days, weeks_used)

    # A — соблюдение плана: недельный adherence, обрезанный единицей.
    weekly: list[float] = []
    for start in range(0, len(window), 7):
        chunk = window[start : start + 7]
        planned = sum(day.planned for day in chunk)
        done = sum(day.done for day in chunk)
        weekly.append(_clip(done / planned) if planned > 0 else 0.0)

    adherence = statistics.fmean(weekly)

    # S — стабильность: 1 минус коэффициент вариации недельных значений.
    if adherence == 0:
        stability = 0.0
    else:
        cv = statistics.pstdev(weekly) / adherence
        stability = max(0.0, 1.0 - cv)

    # R — отсутствие длинных провалов.
    longest_gap = _longest_zero_streak(window)
    rhythm = _clip(1.0 - (longest_gap - REG_GAP_FREE_DAYS) / REG_GAP_SPAN)

    raw = (
        REG_WEIGHTS["adherence"] * adherence
        + REG_WEIGHTS["stability"] * stability
        + REG_WEIGHTS["rhythm"] * rhythm
    )

    return RegularityResult(
        score=_score_from_raw(raw, REG_THRESHOLDS),
        raw=raw,
        adherence=adherence,
        stability=stability,
        rhythm=rhythm,
        weeks=weeks_used,
        longest_gap=longest_gap,
        weekly_adherence=tuple(weekly),
    )


# =============================================================================
# ДОМЕН У
# =============================================================================


def _find_episodes(days: list[Day]) -> list[tuple[int, int]]:
    """Индексы (start, end) непрерывных отрезков со сбоем режима, оба включительно.

    Соседние дни с разными типами сбоя намеренно склеиваются в один эпизод:
    заболеть сразу после командировки — это один непрерывный выход из режима,
    а не два, и возврат из него один. Тип эпизода берётся по первому дню.
    """
    episodes: list[tuple[int, int]] = []
    start: int | None = None
    for index, day in enumerate(days):
        if day.disrupted and start is None:
            start = index
        elif not day.disrupted and start is not None:
            episodes.append((start, index - 1))
            start = None
    if start is not None:
        episodes.append((start, len(days) - 1))
    return episodes


def _mean_adherence(days: list[Day], start: int, length: int) -> float | None:
    """Среднее adherence на отрезке [start, start+length). None, если данных нет."""
    chunk = days[start : start + length]
    if not chunk:
        return None
    return statistics.fmean(day.adherence for day in chunk)


def _local_baseline(days: list[Day], start: int, fallback: float) -> float:
    """База эпизода: медиана по последним чистым дням перед ним.

    Если чистых дней перед эпизодом набралось меньше RES_BASELINE_MIN_DAYS,
    берётся общая база по окну — судить по двум-трём дням нельзя.
    """
    clean_before = [day.adherence for day in days[:start] if not day.disrupted]
    if len(clean_before) < RES_BASELINE_MIN_DAYS:
        return fallback
    return statistics.median(clean_before[-RES_BASELINE_DAYS:])


def _evaluate_episode(days: list[Day], start: int, end: int, fallback: float) -> EpisodeResult:
    length = end - start + 1
    followup = len(days) - 1 - end
    baseline = _local_baseline(days, start, fallback)

    common = {
        "kind": days[start].disruption,
        "start_date": days[start].date,
        "end_date": days[end].date,
        "length_days": length,
        "baseline": baseline,
    }

    unevaluated = {
        "ttr_days": None,
        "censored": False,
        "speed": None,
        "completeness": None,
        "evaluated": False,
    }

    if followup < RES_MIN_FOLLOWUP_DAYS:
        return EpisodeResult(
            **common,
            **unevaluated,
            note=f"после эпизода только {followup} дн., нужно {RES_MIN_FOLLOWUP_DAYS}",
        )

    if baseline <= 0:
        # До эпизода практик не было вовсе — возвращаться некуда, и любая
        # цифра здесь была бы выдумкой.
        return EpisodeResult(
            **common,
            **unevaluated,
            note="нулевая база до эпизода — возврат не определён",
        )

    # Ищем первый день, с которого держится RES_RECOVERY_WINDOW дней на уровне
    # не ниже RES_RECOVERY_RATIO * baseline.
    target = RES_RECOVERY_RATIO * baseline
    recovery_index: int | None = None
    for ttr in range(1, RES_HORIZON_DAYS + 1):
        candidate = end + ttr
        if candidate + RES_RECOVERY_WINDOW > len(days):
            break
        mean = _mean_adherence(days, candidate, RES_RECOVERY_WINDOW)
        if mean is not None and mean >= target:
            recovery_index = candidate
            break

    censored = recovery_index is None
    ttr_days = RES_HORIZON_DAYS if censored else recovery_index - end
    speed = _clip(1.0 - (ttr_days - RES_TTR_FREE_DAYS) / RES_TTR_SPAN)

    # Полнота возврата: как человек живёт две недели после возврата.
    # Для censored-эпизодов — сразу после эпизода, возврата ведь не было.
    consolidation_start = end + 1 if censored else recovery_index
    consolidation = _mean_adherence(days, consolidation_start, RES_CONSOLIDATION_DAYS)
    completeness = 0.0 if consolidation is None else _clip(consolidation / baseline)

    return EpisodeResult(
        **common,
        ttr_days=ttr_days,
        censored=censored,
        speed=speed,
        completeness=completeness,
        evaluated=True,
        note="возврат не зафиксирован за горизонт" if censored else "",
    )


def domain_resilience(days: list[Day]) -> ResilienceResult:
    """Домен У: устойчивость. Формула и пороги описаны в докстринге модуля."""
    # Окно то же, что у Р, но нарезается по дням: устойчивость не привязана к
    # недельной сетке, и терять «неполную» неделю в конце ей незачем.
    window = days[-RES_WINDOW_DAYS:] if days else []

    clean = [day.adherence for day in window if not day.disrupted]
    if not clean:
        return ResilienceResult(
            score=None,
            raw=None,
            baseline=None,
            median_ttr=None,
            speed=None,
            completeness=None,
            note="в окне нет дней без сбоя — базовую линию не от чего считать",
        )

    baseline = statistics.median(clean)
    if baseline <= 0:
        return ResilienceResult(
            score=None,
            raw=None,
            baseline=baseline,
            median_ttr=None,
            speed=None,
            completeness=None,
            note="базовая линия равна нулю — возвращаться некуда",
        )

    episodes = tuple(
        _evaluate_episode(window, start, end, baseline)
        for start, end in _find_episodes(window)
    )
    evaluated = [episode for episode in episodes if episode.evaluated]

    if not evaluated:
        return ResilienceResult(
            score=None,
            raw=None,
            baseline=baseline,
            median_ttr=None,
            speed=None,
            completeness=None,
            episodes=episodes,
            note="в окне нет оцениваемых эпизодов — устойчивость не проверена",
        )

    speed = statistics.median(episode.speed for episode in evaluated)
    completeness = statistics.fmean(episode.completeness for episode in evaluated)
    raw = RES_WEIGHTS["speed"] * speed + RES_WEIGHTS["completeness"] * completeness

    return ResilienceResult(
        score=_score_from_raw(raw, RES_THRESHOLDS),
        raw=raw,
        baseline=baseline,
        median_ttr=statistics.median(episode.ttr_days for episode in evaluated),
        speed=speed,
        completeness=completeness,
        episodes=episodes,
    )
