"""Индекс зрелости здоровья (Health Maturity Index) — домены Р и У.

Пакет содержит только детерминированные вычисления: одинаковый вход всегда
даёт одинаковый выход. Никаких обращений к модели, к сети, к системному
времени и к неинициализированным генераторам случайных чисел.
"""

from hmi.model import DailyRecord, DomainResult
from hmi.domains import compute_domain_r, compute_domain_u

__all__ = [
    "DailyRecord",
    "DomainResult",
    "compute_domain_r",
    "compute_domain_u",
]
