"""Тесты доменов Р и У.

Главное, что здесь проверяется, — детерминированность: одинаковый вход даёт
одинаковый выход, и закоммиченный CSV совпадает с тем, что выдаёт генератор.
Остальное — граничные случаи формул.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hmi.domains import (  # noqa: E402
    Day,
    domain_regularity,
    domain_resilience,
    rows_to_days,
)
from hmi.generate import (  # noqa: E402
    DATA_PATH,
    NO_DISRUPTION,
    PROFILES,
    generate_all,
    read_csv,
)

# Ожидаемые баллы на закоммиченных данных. Если формула или пороги меняются,
# эти числа обязаны меняться осознанно, а не «само поехало».
EXPECTED = {
    "steady": {"regularity": 4, "resilience": 5},
    "erratic": {"regularity": 2, "resilience": 4},
    "typical": {"regularity": 4, "resilience": 5},
}


def _days_for(profile: str) -> list[Day]:
    rows = [row for row in read_csv() if row["profile"] == profile]
    return rows_to_days(rows)


def _flat_day(adherence_num: int, planned: int = 4, disruption: str = NO_DISRUPTION) -> Day:
    return Day(date="2026-01-01", planned=planned, done=adherence_num, disruption=disruption)


def test_generator_is_deterministic() -> None:
    assert generate_all() == generate_all()


def test_committed_csv_matches_generator() -> None:
    assert read_csv(DATA_PATH) == generate_all(), (
        "data/synthetic_practices.csv разошёлся с генератором — перегенерируйте "
        "через `python -m hmi.generate`"
    )


def test_domains_are_deterministic() -> None:
    for spec in PROFILES:
        days = _days_for(spec.name)
        assert domain_regularity(days) == domain_regularity(days)
        assert domain_resilience(days) == domain_resilience(days)


def test_expected_scores_on_synthetic_data() -> None:
    for spec in PROFILES:
        days = _days_for(spec.name)
        assert domain_regularity(days).score == EXPECTED[spec.name]["regularity"], spec.name
        assert domain_resilience(days).score == EXPECTED[spec.name]["resilience"], spec.name


def test_perfect_adherence_scores_five() -> None:
    days = [
        Day(date=f"2026-01-{i + 1:02d}", planned=4, done=4, disruption=NO_DISRUPTION)
        for i in range(84)
    ]
    result = domain_regularity(days)
    assert result.score == 5
    assert result.adherence == 1.0
    assert result.stability == 1.0  # нулевой разброс между неделями
    assert result.rhythm == 1.0
    assert result.longest_gap == 0


def test_zero_adherence_scores_one() -> None:
    days = [
        Day(date=f"2026-01-{i + 1:02d}", planned=4, done=0, disruption=NO_DISRUPTION)
        for i in range(84)
    ]
    result = domain_regularity(days)
    assert result.score == 1
    assert result.stability == 0.0  # «стабильно ничего не делать» — не регулярность
    assert result.rhythm == 0.0


def test_short_history_does_not_score_regularity() -> None:
    days = [_flat_day(4) for _ in range(7 * 7)]  # 7 недель < REG_MIN_WEEKS
    result = domain_regularity(days)
    assert result.score is None
    assert "минимум" in result.note


def test_same_mean_but_ragged_rhythm_scores_lower() -> None:
    """Один и тот же средний объём: ровно по 2 в день против «то 4, то 0»."""
    even = [_flat_day(2) for _ in range(84)]
    ragged = [_flat_day(4 if (i // 7) % 2 == 0 else 0) for i in range(84)]

    even_result = domain_regularity(even)
    ragged_result = domain_regularity(ragged)

    assert even_result.adherence == ragged_result.adherence
    assert even_result.raw > ragged_result.raw
    assert even_result.score > ragged_result.score


def test_resilience_without_episodes_is_none_not_five() -> None:
    days = [_flat_day(4) for _ in range(84)]
    result = domain_resilience(days)
    assert result.score is None
    assert "не проверена" in result.note


def test_fast_recovery_beats_slow_recovery() -> None:
    def build(recovery_days: int) -> list[Day]:
        days: list[Day] = []
        for i in range(84):
            if 20 <= i <= 25:
                days.append(_flat_day(0, disruption="travel"))
            elif 25 < i <= 25 + recovery_days:
                days.append(_flat_day(1))  # ещё не вернулся к базе
            else:
                days.append(_flat_day(4))
        return days

    fast = domain_resilience(build(2))
    slow = domain_resilience(build(15))

    assert fast.median_ttr is not None and slow.median_ttr is not None
    assert fast.median_ttr < slow.median_ttr
    assert fast.raw > slow.raw
    assert fast.score >= slow.score


def test_no_recovery_within_horizon_is_censored() -> None:
    days: list[Day] = []
    for i in range(84):
        if 20 <= i <= 25:
            days.append(_flat_day(0, disruption="travel"))
        elif i > 25:
            days.append(_flat_day(1))  # после сбоя так и не вернулся
        else:
            days.append(_flat_day(4))

    result = domain_resilience(days)
    episode = result.episodes[0]
    assert episode.evaluated
    assert episode.censored
    assert episode.speed == 0.0
    assert result.score == 1


def test_episode_at_window_edge_is_not_evaluated() -> None:
    days = [_flat_day(4) for _ in range(84)]
    days[-3:] = [_flat_day(0, disruption="travel") for _ in range(3)]

    result = domain_resilience(days)
    assert len(result.episodes) == 1
    assert result.episodes[0].evaluated is False
    assert result.score is None  # оценивать нечего, а не «плохо»


def test_adjacent_disruption_kinds_merge_into_one_episode() -> None:
    days = [_flat_day(4) for _ in range(84)]
    for i in range(20, 24):
        days[i] = _flat_day(0, disruption="travel")
    for i in range(24, 27):
        days[i] = _flat_day(0, disruption="illness")

    result = domain_resilience(days)
    assert len(result.episodes) == 1
    assert result.episodes[0].length_days == 7
    assert result.episodes[0].kind == "travel"


def test_empty_input_does_not_crash() -> None:
    assert domain_regularity([]).score is None
    assert domain_resilience([]).score is None
