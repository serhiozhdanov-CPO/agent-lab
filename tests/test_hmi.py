"""Тесты доменов Р и У: детерминизм, пороги, краевые случаи.

Запуск: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hmi.domains import (  # noqa: E402
    R_SPREAD_CAP,
    U_TTR_MAX,
    compute_domain_r,
    compute_domain_u,
    score_from_raw,
)
from hmi.model import DailyRecord, window_adherence  # noqa: E402
from hmi.synth import generate_dataset, generate_timeline, PROFILES  # noqa: E402
from run_demo import build_results, to_json  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DAYS_PER_WEEK = 7
PLANNED_PER_DAY = 20  # 140 сессий в неделю: доли вида 0.6 / 0.7 выражаются точно


def timeline_from_weekly(fractions, context_weeks=()):
    """Строит таймлайн с заданными недельными долями выполнения плана.

    План — PLANNED_PER_DAY сессий каждый день, то есть 140 в неделю: этого
    хватает, чтобы доля недели была ровно равна заданной дроби, а не её
    округлению. Выполненные сессии распределяются по дням недели равномерно,
    иначе скользящие 7-дневные окна домена У ловили бы не срыв режима, а
    искусственный перекос внутри недели.
    """
    records = []
    for week_index, fraction in enumerate(fractions):
        weekly_done = round(DAYS_PER_WEEK * PLANNED_PER_DAY * fraction)
        per_day, remainder = divmod(weekly_done, DAYS_PER_WEEK)
        for weekday in range(DAYS_PER_WEEK):
            records.append(
                DailyRecord(
                    day=week_index * DAYS_PER_WEEK + weekday,
                    planned=PLANNED_PER_DAY,
                    done=per_day + (1 if weekday < remainder else 0),
                    context=("travel",) if week_index in context_weeks else (),
                )
            )
    return records


class TestDeterminism(unittest.TestCase):
    def test_dataset_is_reproducible(self):
        self.assertEqual(generate_dataset(), generate_dataset())

    def test_results_json_is_byte_identical(self):
        self.assertEqual(to_json(build_results()), to_json(build_results()))

    def test_deterministic_across_processes(self):
        """Разный PYTHONHASHSEED не должен менять результат."""
        outputs = []
        for seed in ("0", "1", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = subprocess.run(
                [sys.executable, "run_demo.py", "--json"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            outputs.append(proc.stdout)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])

    def test_profile_timeline_independent_of_call_order(self):
        """Таймлайн профиля зависит только от его seed, не от порядка вызовов."""
        first = generate_timeline(PROFILES[-1])
        generate_timeline(PROFILES[0])
        second = generate_timeline(PROFILES[-1])
        self.assertEqual(first, second)


class TestScale(unittest.TestCase):
    def test_thresholds_map_to_expected_scores(self):
        cases = [
            (1.00, 5), (0.85, 5), (0.8499, 4),
            (0.70, 4), (0.6999, 3),
            (0.55, 3), (0.5499, 2),
            (0.40, 2), (0.3999, 1), (0.0, 1),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(score_from_raw(raw), expected)

    def test_exact_threshold_is_not_lost_to_float_error(self):
        self.assertEqual(score_from_raw(0.1 + 0.6), 4)  # 0.7000000000000001
        self.assertEqual(score_from_raw(0.55 + 0.30 - 0.00), 5)


class TestDomainR(unittest.TestCase):
    def test_perfect_adherence_scores_five(self):
        result = compute_domain_r(timeline_from_weekly([1.0] * 12))
        self.assertEqual(result.score, 5)
        self.assertAlmostEqual(result.raw, 1.0)

    def test_zero_adherence_scores_one(self):
        result = compute_domain_r(timeline_from_weekly([0.0] * 12))
        self.assertEqual(result.score, 1)
        self.assertAlmostEqual(result.raw, 0.3)  # A=0, S=1 (разброса нет), D=0

    def test_stability_separates_equal_averages(self):
        """Одинаковое среднее, разная ровность -> разный балл."""
        steady = compute_domain_r(timeline_from_weekly([0.7] * 12))
        swinging = compute_domain_r(timeline_from_weekly([1.0, 0.4] * 6))
        self.assertAlmostEqual(steady.components["A_adherence"], 0.7, places=2)
        self.assertAlmostEqual(swinging.components["A_adherence"], 0.7, places=2)
        self.assertGreater(steady.raw, swinging.raw)
        self.assertEqual(steady.components["S_stability"], 1.0)
        # pstdev([1.0, 0.4] * 6) = 0.30 -> S = 1 - 0.30 / R_SPREAD_CAP
        self.assertAlmostEqual(
            swinging.components["S_stability"], 1 - 0.30 / R_SPREAD_CAP
        )

    def test_spread_at_cap_zeroes_stability(self):
        # pstdev([1.0, 0.3] * 6) = 0.35 = R_SPREAD_CAP
        result = compute_domain_r(timeline_from_weekly([1.0, 0.3] * 6))
        self.assertEqual(result.components["S_stability"], 0.0)

    def test_coverage_counts_weeks_at_or_above_60_percent(self):
        result = compute_domain_r(timeline_from_weekly([0.6] * 6 + [0.5] * 6))
        self.assertAlmostEqual(result.components["D_coverage"], 0.5)

    def test_uses_only_last_window_weeks(self):
        """Старые недели за пределами окна не влияют на балл."""
        recent = [0.9] * 8
        full = compute_domain_r(timeline_from_weekly([0.1] * 4 + recent), window_weeks=8)
        only = compute_domain_r(timeline_from_weekly(recent), window_weeks=8)
        self.assertEqual(full.raw, only.raw)

    def test_short_history_is_not_scored(self):
        result = compute_domain_r(timeline_from_weekly([0.9] * 7))
        self.assertIsNone(result.score)
        self.assertEqual(result.reason, "insufficient_data")

    def test_no_plan_is_not_scored(self):
        empty = [DailyRecord(day=d, planned=0, done=0) for d in range(84)]
        result = compute_domain_r(empty)
        self.assertIsNone(result.score)
        self.assertEqual(result.reason, "no_plan")

    def test_window_outside_supported_range_is_rejected(self):
        timeline = timeline_from_weekly([0.9] * 12)
        for bad_window in (7, 13):
            with self.subTest(window=bad_window):
                with self.assertRaises(ValueError):
                    compute_domain_r(timeline, window_weeks=bad_window)


class TestDomainU(unittest.TestCase):
    def test_no_episodes_returns_none_not_five(self):
        result = compute_domain_u(timeline_from_weekly([0.9] * 12))
        self.assertIsNone(result.score)
        self.assertEqual(result.reason, "no_disruption_episodes")

    def test_flat_low_baseline_is_not_scored(self):
        result = compute_domain_u(timeline_from_weekly([0.2] * 12))
        self.assertIsNone(result.score)
        self.assertEqual(result.reason, "no_baseline")

    def test_baseline_ignores_flagged_weeks(self):
        """Неделя-командировка не должна занижать базовую линию."""
        result = compute_domain_u(
            timeline_from_weekly([1.0] * 5 + [0.1] + [1.0] * 6, context_weeks={5})
        )
        self.assertEqual(result.diagnostics["baseline_source"], "calm_weeks")
        self.assertAlmostEqual(result.diagnostics["baseline"], 1.0)

    def test_fast_return_beats_slow_return(self):
        fast = compute_domain_u(timeline_from_weekly([1.0] * 5 + [0.1] + [1.0] * 6))
        slow = compute_domain_u(
            timeline_from_weekly([1.0] * 5 + [0.1, 0.3, 0.5, 0.7] + [1.0] * 3)
        )
        self.assertGreater(fast.raw, slow.raw)
        self.assertGreater(fast.score, slow.score)
        self.assertLess(
            fast.diagnostics["median_ttr_days"], slow.diagnostics["median_ttr_days"]
        )

    def test_unrecovered_episode_is_censored_at_ttr_max(self):
        result = compute_domain_u(timeline_from_weekly([1.0] * 6 + [0.1] * 6))
        episode = result.diagnostics["episodes"][-1]
        self.assertFalse(episode["recovered"])
        self.assertEqual(episode["ttr_days"], U_TTR_MAX)
        self.assertEqual(result.diagnostics["censored_episodes"], 1)
        self.assertEqual(result.components["C_completion"], 0.0)

    def test_episodes_do_not_overlap(self):
        result = compute_domain_u(
            timeline_from_weekly([1.0, 1.0, 0.1, 1.0, 1.0, 0.1, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
        )
        episodes = result.diagnostics["episodes"]
        self.assertEqual(len(episodes), 2)
        self.assertLess(episodes[0]["dip_end_day"], episodes[1]["onset_day"])

    def test_deeper_drop_scores_lower(self):
        shallow = compute_domain_u(timeline_from_weekly([1.0] * 5 + [0.5] + [1.0] * 6))
        deep = compute_domain_u(timeline_from_weekly([1.0] * 5 + [0.0] + [1.0] * 6))
        self.assertGreater(
            shallow.components["G_shallowness"], deep.components["G_shallowness"]
        )

    def test_short_history_is_not_scored(self):
        result = compute_domain_u(timeline_from_weekly([1.0] * 4 + [0.1] * 3))
        self.assertIsNone(result.score)
        self.assertEqual(result.reason, "insufficient_data")


class TestModel(unittest.TestCase):
    def test_done_cannot_exceed_planned(self):
        with self.assertRaises(ValueError):
            DailyRecord(day=0, planned=1, done=2)

    def test_negative_values_rejected(self):
        with self.assertRaises(ValueError):
            DailyRecord(day=0, planned=-1, done=0)

    def test_adherence_is_capped_at_one(self):
        records = [DailyRecord(0, 2, 2), DailyRecord(1, 0, 0)]
        self.assertEqual(window_adherence(records), 1.0)

    def test_adherence_without_plan_is_none(self):
        self.assertIsNone(window_adherence([DailyRecord(0, 0, 0)]))


class TestDemoOutput(unittest.TestCase):
    def test_demo_scores_are_stable(self):
        """Зафиксированный ожидаемый результат демо-прогона.

        Тест намеренно жёсткий: любое изменение формулы, порогов или
        генератора обязано быть осознанным и обновить эти числа.
        """
        results = build_results()
        expected = {
            "P-001": (5, None),
            "P-002": (3, 3),
            "P-003": (2, 2),
            "P-004": (3, 3),
            "P-005": (2, 3),
            "P-006": (2, 1),
        }
        actual = {
            person_id: (data["r"].score, data["u"].score)
            for person_id, data in results.items()
        }
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
