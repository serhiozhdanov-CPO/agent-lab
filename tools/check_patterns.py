#!/usr/bin/env python3
"""Исполняемые критерии из expected-patterns.md.

    python tools/check_patterns.py data/synth-0001/

Считает по сгенерированному потоку ровно те статистики, что записаны в поле
«Критерий обнаружения» каждой карточки, и печатает таблицу PASS/FAIL. Если
генератор перестал закладывать паттерн — здесь красное.

Скрипт намеренно НЕ читает ground_truth.json: он видит ровно то же, что увидит
проверяемая аналитика. Единственное, чем он пользуется помимо потока, —
manifest.json, который открыт и для аналитики тоже.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from synth.adapters.synthetic import profile as prof  # noqa: E402
from synth.canonical import WEARABLE_METRICS, read_jsonl  # noqa: E402

Series = dict[int, float]


# --------------------------------------------------------------------------
# Мелкая статистика на stdlib
# --------------------------------------------------------------------------
def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def median(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else float("nan")


def ols(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Наклон, свободный член и R² простой линейной регрессии."""
    n = len(xs)
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx:
        return float("nan"), float("nan"), float("nan")
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot else float("nan")
    return slope, intercept, r2


def rolling_mean(series: Series, day: int, window: int) -> float:
    vals = [series[d] for d in range(day - window + 1, day + 1) if d in series]
    return mean(vals) if len(vals) >= max(2, window // 2) else float("nan")


def paired(a: Series, b: Series) -> tuple[list[float], list[float]]:
    days = sorted(set(a) & set(b))
    return [a[d] for d in days], [b[d] for d in days]


# --------------------------------------------------------------------------
# Загрузка
# --------------------------------------------------------------------------
class Dataset:
    def __init__(self, directory: str):
        with open(os.path.join(directory, "manifest.json"), encoding="utf-8") as fh:
            self.manifest = json.load(fh)
        self.start = date.fromisoformat(self.manifest["period"]["start"])
        self.days = self.manifest["period"]["days"]
        self.age = self.manifest["subject"]["age"]
        self.sleep_need_min = self.manifest["subject"]["baselines"]["sleep_need_min"]

        self.series: dict[str, Series] = {}
        for rec in read_jsonl(os.path.join(directory, "observations.jsonl")):
            day = (date.fromisoformat(rec["effective_date"]) - self.start).days
            self.series.setdefault(rec["metric"], {})[day] = rec["value"]

    def s(self, metric: str) -> Series:
        return self.series.get(metric, {})

    def weekday(self, day: int) -> int:
        return (self.start.weekday() + day) % 7

    def quiet_days(self) -> set[int]:
        """Дни, свободные от событий: среда–пятница вне окна болезни и не после алкоголя.

        На таких днях надбавка дня недели равна нулю, поэтому уровень ряда ближе
        всего к базовой линии — именно на них её и надо оценивать.
        """
        after_alcohol = {d + 1 for d, v in self.s("context.alcohol_units").items() if v > 0}
        return {
            d for d in range(self.days)
            if self.weekday(d) in (2, 3, 4)
            and not (60 <= d <= 83)
            and d not in after_alcohol
        }

    def log_rmssd(self) -> Series:
        return {d: math.log(v) for d, v in self.s("hrv.rmssd").items()}

    def sleep_debt(self, window: int = 7, min_obs: int = 4) -> Series:
        """Долг сна, восстановленный из НАБЛЮДЁННЫХ длительностей.

        Аналитика видит ряд с пропусками, поэтому дефицит считается как средний
        по имеющимся дням окна и домножается на длину окна.
        """
        duration = self.s("sleep.duration")
        out: Series = {}
        for day in range(self.days):
            vals = [duration[d] for d in range(day - window + 1, day + 1) if d in duration]
            if len(vals) < min_obs:
                continue
            deficit = mean([max(0.0, self.sleep_need_min - v) for v in vals])
            out[day] = deficit * window / 60.0
        return out


# --------------------------------------------------------------------------
# Проверки
# --------------------------------------------------------------------------
Result = tuple[str, bool, str]
LATE_NIGHT_WAKE_DAYS = (5, 6)


def check_p01(ds: Dataset) -> list[Result]:
    def split(metric: str) -> tuple[list[float], list[float]]:
        s = ds.s(metric)
        wk = [v for d, v in s.items() if ds.weekday(d) not in LATE_NIGHT_WAKE_DAYS]
        we = [v for d, v in s.items() if ds.weekday(d) in LATE_NIGHT_WAKE_DAYS]
        return wk, we

    out: list[Result] = []

    wk, we = split("sleep.onset")
    shift = median(we) - median(wk)
    out.append(("сдвиг засыпания в выходные ≥ +35 мин", shift >= 35, f"{shift:+.0f} мин"))

    wk, we = split("sleep.duration")
    shift = median(we) - median(wk)
    out.append(("длительность сна в выходные ≥ +15 мин", shift >= 15, f"{shift:+.0f} мин"))

    wk, we = split("sleep.efficiency")
    drop = mean(wk) - mean(we)
    out.append(("эффективность сна в выходные ниже на ≥ 1.0 п.п.", drop >= 1.0, f"{drop:.2f} п.п."))

    rhr = ds.s("hr.resting")
    by_dow = {w: mean([v for d, v in rhr.items() if ds.weekday(d) == w]) for w in range(7)}
    monday = by_dow[0]
    midweek = mean([v for d, v in rhr.items() if ds.weekday(d) in (2, 3, 4)])
    is_max = monday == max(by_dow.values())
    gap = monday - midweek
    out.append((
        "понедельник — максимум недели и ≥ +1.5 уд/мин к ср–пт",
        is_max and gap >= 1.5,
        f"пн {monday:.1f}, ср–пт {midweek:.1f}, разница {gap:+.2f}"
        + ("" if is_max else "; пн НЕ максимум"),
    ))
    return out


def check_p02(ds: Dataset) -> list[Result]:
    out: list[Result] = []

    quiet = ds.quiet_days()
    med_rhr = median([v for d, v in ds.s("hr.resting").items() if d in quiet])
    expected = prof.expected_rhr(ds.age)
    delta = abs(med_rhr - expected)
    # Допуск обязан вместить индивидуальное смещение N(0, 3): возраст задаёт
    # уровень, а не точное значение конкретного человека.
    out.append((f"медиана пульса на спокойных днях в ±5.0 от формулы возраста ({expected:.1f})",
                delta <= 5.0,
                f"медиана {med_rhr:.1f} по {len([d for d in ds.s('hr.resting') if d in quiet])} "
                f"дням, отклонение {delta:.2f}"))

    med_rmssd = median([v for d, v in ds.s("hrv.rmssd").items() if d in quiet])
    ratio = med_rmssd / prof.expected_rmssd(ds.age)
    out.append(("отношение медианы RMSSD на спокойных днях к формуле в [0.65, 1.45]",
                0.65 <= ratio <= 1.45,
                f"медиана {med_rmssd:.1f} мс, формула {prof.expected_rmssd(ds.age):.1f}, "
                f"отношение {ratio:.2f}"))

    xs, ys = paired(ds.s("hr.resting"), ds.log_rmssd())
    r = pearson(xs, ys)
    out.append(("корреляция пульса и ln(RMSSD) в [−0.80, −0.35]",
                -0.80 <= r <= -0.35, f"r = {r:+.3f} по {len(xs)} дням"))
    return out


# Окно болезни и восстановления. Внутри него пульс и вариабельность сдвинуты
# на порядок сильнее любого другого эффекта, поэтому во всех контрастах, где
# группа сравнения — «все остальные дни», это окно исключается.
ILLNESS_WINDOW = range(60, 84)


def check_p03(ds: Dataset) -> list[Result]:
    alcohol = ds.s("context.alcohol_units")
    drink_days = {d for d, v in alcohol.items() if v > 0}
    next_days = {d + 1 for d in drink_days}
    out: list[Result] = []

    def is_rest(d: int) -> bool:
        return d not in next_days and d not in drink_days and d not in ILLNESS_WINDOW

    rhr = ds.s("hr.resting")
    after = [v for d, v in rhr.items() if d in next_days and d not in ILLNESS_WINDOW]
    rest = [v for d, v in rhr.items() if is_rest(d)]
    lift = mean(after) - mean(rest)
    out.append((f"пульс покоя на дне D+1 выше на ≥ 4.0 уд/мин (n={len(after)})",
                lift >= 4.0, f"{lift:+.2f} уд/мин"))

    same = [v for d, v in rhr.items() if d in drink_days and d not in ILLNESS_WINDOW]
    same_lift = mean(same) - mean(rest)
    # Порог 2.0, а не 1.5: вечера с алкоголем сами по себе смещены к пятнице
    # и субботе, поэтому сравнение с «остальными днями» не идеально чистое.
    out.append(("в день D эффект < 2.0 уд/мин", abs(same_lift) < 2.0, f"{same_lift:+.2f} уд/мин"))

    rmssd = ds.s("hrv.rmssd")
    after_h = mean([v for d, v in rmssd.items() if d in next_days and d not in ILLNESS_WINDOW])
    rest_h = mean([v for d, v in rmssd.items() if is_rest(d)])
    rel = (after_h - rest_h) / rest_h * 100
    out.append(("RMSSD на дне D+1 ниже на ≥ 12 %", rel <= -12.0, f"{rel:+.1f} %"))
    return out


def check_p04(ds: Dataset) -> list[Result]:
    rhr = ds.s("hr.resting")
    out: list[Result] = []

    roll = {d: rolling_mean(rhr, d, 3) for d in range(ds.days)}
    roll = {d: v for d, v in roll.items() if not math.isnan(v)}
    peak_day = max(roll, key=lambda d: roll[d])
    med = median(list(rhr.values()))
    excess = roll[peak_day] - med
    out.append(("пик 3-дневного среднего пульса — в днях 61–67, ≥ +7 уд/мин к медиане",
                61 <= peak_day <= 67 and excess >= 7.0,
                f"пик на дне {peak_day}, превышение {excess:+.2f} уд/мин"))

    # Требовать подряд идущие дни нельзя: устройство пропадает примерно в 7 %
    # ночей, и вероятность разрыва внутри трёхдневного окна около 20 %.
    # Аналитик на ряду с пропусками считал бы так же — по попаданию в окно.
    temp = ds.s("body.temp_deviation")
    hot = sorted(d for d, v in temp.items() if v >= 0.35)
    inside = [d for d in hot if 60 <= d <= 68]
    outside = [d for d in hot if not 60 <= d <= 68]
    out.append(("≥ 3 дня с температурой ≥ 0.35 °C, и все они внутри дней 60–68",
                len(inside) >= 3 and not outside,
                f"внутри окна {inside or '—'}"
                + (f", вне окна {outside}" if outside else "")))

    overshoot = mean([v for d, v in rhr.items() if 76 <= d <= 82])
    out.append(("перелёт: среднее по дням 76–82 ниже медианы ряда",
                overshoot < med, f"{overshoot:.2f} против медианы {med:.2f}"))
    return out


def check_p05(ds: Dataset) -> list[Result]:
    load = ds.s("workout.load")
    rhr = ds.s("hr.resting")
    out: list[Result] = []

    inside = mean([v for d, v in load.items() if 35 <= d <= 55])
    outside = mean([v for d, v in load.items() if not 35 <= d <= 55])
    share = inside / outside if outside else float("nan")
    out.append(("нагрузка в днях 35–55 ≤ 50 % от остальных",
                share <= 0.50, f"{share * 100:.1f} %"))

    after = mean([v for d, v in rhr.items() if 49 <= d <= 62])
    before = mean([v for d, v in rhr.items() if 7 <= d <= 34])
    lift = after - before
    out.append(("пульс в днях 49–62 выше, чем в 7–34, на ≥ 2.5 уд/мин",
                lift >= 2.5, f"{lift:+.2f} уд/мин"))

    # Отложенность отклика. Кросс-корреляция здесь бесполезна: 7-дневное среднее
    # нагрузки — почти ступенька, и её корреляция с пульсом максимальна около
    # нулевого лага независимо от того, быстрый отклик или медленный. Лаг видно
    # по положению пика: блок начинается на дне 35, а пульс достигает максимума
    # только к его концу.
    #
    # Дни-следы алкоголя исключаются: три таких дня подряд внутри десятидневного
    # окна сдвигают среднее почти на 1 уд/мин и смазывают картину. Аналитик,
    # нашедший P-03, сделал бы то же самое.
    after_alcohol = {d + 1 for d, v in ds.s("context.alcohol_units").items() if v > 0}
    clean = {d: v for d, v in rhr.items() if d not in after_alcohol}
    roll: dict[int, float] = {}
    for d in range(28, 58):   # верхняя граница такая, чтобы окно не задело болезнь
        vals = [clean[x] for x in range(d - 4, d + 5) if x in clean]
        if len(vals) >= 6:
            roll[d] = mean(vals)
    peak = max(roll, key=lambda d: roll[d]) if roll else -1
    out.append((
        "пик 9-дневного среднего пульса на отрезке 28–57 — не раньше дня 46",
        peak >= 46,
        f"пик на дне {peak}, это {peak - 35} дней после начала блока",
    ))
    return out


# Окна, свободные от болезни (P-04, дни 62–83) и от отклика на детренированность
# (P-05, дни 35–62). Обе просадки вариабельности там на порядок больше эффекта
# долга сна и просто забивают корреляцию — аналитик, нашедший P-04 и P-05,
# исключил бы эти отрезки ровно так же.
P06_CLEAN_DAYS = set(range(0, 35)) | set(range(84, 112))


def check_p06(ds: Dataset) -> list[Result]:
    keep = P06_CLEAN_DAYS
    log_h = {d: v for d, v in ds.log_rmssd().items() if d in keep}
    debt = {d: v for d, v in ds.sleep_debt().items() if d in keep}
    duration = {d: v for d, v in ds.s("sleep.duration").items() if d in keep}
    out: list[Result] = []

    xs, ys = paired(debt, log_h)
    r_debt = pearson(xs, ys)
    xs2, ys2 = paired(duration, log_h)
    r_dur = pearson(xs2, ys2)
    # Разность, а не отношение: связь с сегодняшним сном по построению около
    # нуля, а отношение с почти нулевым знаменателем скачет от 0.5 до 99 и
    # ничего не измеряет.
    gap = abs(r_debt) - abs(r_dur)
    out.append(("связь с долгом сильнее связи с сегодняшним сном на ≥ 0.15 по |r|",
                gap >= 0.15,
                f"|r(долг)| = {abs(r_debt):.3f}, |r(сон сегодня)| = {abs(r_dur):.3f}, "
                f"разность {gap:+.3f}"))

    out.append((f"корреляция ln(RMSSD) с долгом отрицательна и ≤ −0.25 (n={len(xs)})",
                r_debt <= -0.25, f"r = {r_debt:+.3f}"))

    # Окна по две недели, а не по одной: при суточном разбросе RMSSD ≈ 26 %
    # среднее по семи дням имеет стандартную ошибку около 10 % и на разнице
    # в 5 % ничего показать не может.
    rmssd = ds.s("hrv.rmssd")
    # Окна взяты по установившемуся состоянию, без переходных дней: долг
    # набирается скользящим окном в неделю, поэтому первые дни отрезка ещё
    # низкие, а первые дни после него — ещё высокие.
    tired = mean([v for d, v in rmssd.items() if 88 <= d <= 97])
    rested = mean([v for d, v in rmssd.items() if 102 <= d <= 111])
    rel = (tired - rested) / rested * 100
    out.append(("RMSSD в разгар недосыпа (88–97) ниже, чем в 102–111, на ≥ 5 %",
                rel <= -5.0, f"{rel:+.1f} %"))
    return out


def check_p07(ds: Dataset) -> list[Result]:
    ferritin = ds.s("lab.ferritin")
    crp = ds.s("lab.crp")
    out: list[Result] = []

    confounded = [d for d, v in crp.items() if v > 5.0]
    clean_days = [d for d in sorted(ferritin) if d not in confounded]

    slope5, _, r2_5 = ols([float(d) for d in clean_days], [ferritin[d] for d in clean_days])
    out.append((f"наклон ферритина без искажённой точки отрицателен, R² ≥ 0.97 (n={len(clean_days)})",
                slope5 < 0 and r2_5 >= 0.97,
                f"наклон {slope5:+.3f} нг/мл в день, R² = {r2_5:.4f}"))

    all_days = sorted(ferritin)
    slope6, icept6, r2_6 = ols([float(d) for d in all_days], [ferritin[d] for d in all_days])
    out.append(("качество подгонки по всем точкам рушится: R² < 0.50",
                r2_6 < 0.50,
                f"R² по всем {r2_6:.4f} против {r2_5:.4f} по чистым; "
                f"наклон {slope6:+.3f} против {slope5:+.3f} — остался правдоподобным"))

    slope5_, icept5, _ = ols([float(d) for d in clean_days], [ferritin[d] for d in clean_days])
    last = float(max(all_days))
    pred_clean = icept5 + slope5_ * last
    pred_all = icept6 + slope6 * last
    divergence = abs(pred_all - pred_clean) / pred_clean
    out.append((f"оценка уровня на день {last:.0f} расходится более чем на 20 %",
                divergence > 0.20,
                f"по чистым {pred_clean:.1f}, по всем {pred_all:.1f} нг/мл "
                f"({divergence * 100:.0f} %)"))

    ok = len(confounded) == 1 and 60 <= confounded[0] <= 68
    out.append(("ровно одна точка с CRP > 5 мг/л, и она внутри окна болезни",
                ok, f"точки с CRP > 5: {confounded or '—'}"))

    hba1c = list(ds.s("lab.hba1c").values())
    spread = max(hba1c) - min(hba1c)
    out.append(("размах HbA1c ≤ 0.2 п.п. (тренда нет)", spread <= 0.2, f"размах {spread:.2f} п.п."))
    return out


def check_p08(ds: Dataset) -> list[Result]:
    rhr = ds.s("hr.resting")
    out: list[Result] = []

    coverage = len(rhr) / ds.days
    out.append(("покрытие пульса покоя в [0.83, 0.95]",
                0.83 <= coverage <= 0.95, f"{len(rhr)}/{ds.days} = {coverage:.3f}"))

    gap = [71, 72, 73]
    leaked = [m for m in WEARABLE_METRICS for d in gap if d in ds.s(m)]
    out.append(("дни 71–73 без метрик носимого устройства", not leaked,
                "чисто" if not leaked else f"протекли: {sorted(set(leaked))}"))

    missing = {d for d in range(ds.days) if d not in rhr}
    calm = {d: v for d, v in rhr.items() if d not in ILLNESS_WINDOW}
    before = [v for d, v in calm.items() if d + 1 in missing]
    delta = abs(mean(before) - mean(list(calm.values()))) if before else 0.0
    # Порог ±2.5, а не ±1.0: дней перед пропуском около восьми, стандартная
    # ошибка их среднего ≈ 0.55 уд/мин, поэтому отклонение до 2.5 полностью
    # совместимо со случайными пропусками. Это ровно тот урок, о котором P-08.
    out.append(("пропуски не связаны с исходом: дни перед пропуском в ±2.5 уд/мин",
                delta <= 2.5, f"отклонение {delta:.2f} уд/мин по {len(before)} дням"))
    return out


CHECKS = [
    ("P-01", "Недельный ритм и социальный джетлаг", check_p01),
    ("P-02", "Возрастные базовые линии и связка пульс↔вариабельность", check_p02),
    ("P-03", "Алкогольные вечера: лаговый след", check_p03),
    ("P-04", "Окно острой болезни", check_p04),
    ("P-05", "Блок детренированности", check_p05),
    ("P-06", "Накопленный недосып", check_p06),
    ("P-07", "Связность лабораторного ряда и искажённый замер", check_p07),
    ("P-08", "Порог различимости", check_p08),
]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("использование: python tools/check_patterns.py <каталог набора>", file=sys.stderr)
        return 2

    ds = Dataset(argv[1])
    print(f"Набор: {ds.manifest['subject_id']}, возраст {ds.age:g}, "
          f"{ds.days} дней, seed {ds.manifest['generator']['seed']}\n")

    failed = 0
    for pid, name, fn in CHECKS:
        print(f"{pid} · {name}")
        for title, ok, detail in fn(ds):
            mark = "PASS" if ok else "FAIL"
            if not ok:
                failed += 1
            print(f"  [{mark}] {title}\n         {detail}")
        print()

    total = sum(len(fn(ds)) for _, _, fn in CHECKS)
    if failed:
        print(f"ИТОГ: {total - failed}/{total} проверок пройдено, {failed} провалено")
        return 1
    print(f"ИТОГ: все {total} проверок пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
