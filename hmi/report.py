"""Форматирование результатов расчёта доменов — только текст, без вычислений."""

from __future__ import annotations

from hmi.domains import SCORE_THRESHOLDS
from hmi.model import DomainResult

SCORE_LABELS = {
    5: "держится сама, без внешнего усилия",
    4: "устойчиво, с отдельными просадками",
    3: "работает, но требует внимания",
    2: "держится эпизодически",
    1: "практики фактически не выстроены",
}

REASON_LABELS = {
    "insufficient_data": "недостаточно данных (нужно >= 8 полных недель)",
    "no_plan": "в окне нет запланированных сессий",
    "no_baseline": "базовой линии нет — возвращаться не к чему",
    "no_disruption_episodes": "срывов не было, устойчивость не наблюдалась",
}


def score_cell(result: DomainResult) -> str:
    """Балл или 'н/д' — короткая ячейка для таблицы."""
    return str(result.score) if result.is_scored else "н/д"


def score_line(result: DomainResult) -> str:
    """Строка вида '4 (raw 0.731) — устойчиво, с отдельными просадками'."""
    if not result.is_scored:
        return f"н/д — {REASON_LABELS.get(result.reason, result.reason)}"
    return f"{result.score} (raw {result.raw:.3f}) — {SCORE_LABELS[result.score]}"


def components_line(result: DomainResult) -> str:
    """Раскладка формулы по компонентам."""
    if not result.components:
        return "  компоненты: —"
    parts = ", ".join(f"{k} = {v:.3f}" for k, v in result.components.items())
    return f"  компоненты: {parts}"


def thresholds_line() -> str:
    parts = [f">= {t:.2f} -> {s}" for t, s in SCORE_THRESHOLDS]
    return "пороги: " + "; ".join(parts) + "; иначе -> 1"


def render_table(rows: list[tuple[str, str, DomainResult, DomainResult]]) -> str:
    """Сводная таблица по всем людям датасета."""
    header = f"{'ID':<7} {'Архетип':<44} {'Р':>3} {'У':>3}"
    lines = [header, "-" * len(header)]
    for person_id, title, r_result, u_result in rows:
        lines.append(
            f"{person_id:<7} {title:<44} {score_cell(r_result):>3} {score_cell(u_result):>3}"
        )
    return "\n".join(lines)
