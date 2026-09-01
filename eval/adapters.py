#!/usr/bin/env python3
"""Адаптеры генераторов: приведение трёх разных эталонов к одной схеме.

В репозитории три генератора, и каждый описывает заложенные паттерны по-своему:
`health-synth` пишет `answer-key.json` с блоком `patterns`, `health-data` —
`manifest.json` с `patterns` + `negative_controls`, `synthetic-health` —
`manifest.json` со списками `patterns` и `traps`. Дни нумеруются то с нуля, то
с единицы; связь «поздний отбой → HRV» выражена то в процентах, то в долях, то
в миллисекундах.

Оценщик не должен знать ничего из этого. Каждый адаптер приводит свой эталон к
нормализованному `truth.json`, и дальше работает один код.

Нормализованная схема:

    {
      "schema_version": "1.0",
      "generator": "health-synth",
      "calendar":   {"start": "2026-01-05", "end": "2026-04-26", "days": 112},
      "baselines":  {"hr_resting": 51.2, "hrv_rmssd": 44.8, ...},
      "events":     [{"id", "kind", "from", "to", "tail_to"?, "optional"?, ...}],
      "control_windows": [{"id", "from", "to"}],
      "couplings":  [{"id", "cause", "effect", "lag_days", "effect_size",
                      "unit", "tolerance"}],
      "trends":     [{"id", "metric", "source"?, "verdict", "total_change"?,
                      "unit"?, "tolerance"?}],
      "missingness": {"mechanism", "gaps": [{"from", "to", "reason"}]},
      "source_divergence": [{"id", "source_a", "source_b", "metric", "kind",
                             "effect", "tolerance"}]
    }

Виды событий (`kind`): `timezone_shift`, `regimen_collapse`, `illness`,
`training_block`. Событие с `"optional": true` не штрафуется ни за пропуск, ни
за находку — так помечается фон, который эталон фиксирует, но обнаружение
которого от аналитики не требуется.
"""

from __future__ import annotations

from harness import TRUTH_SCHEMA_VERSION, day_span, parse_date

# --------------------------------------------------------------------------
# Правила редактирования документов, уезжающих к агенту.
#
# Убираем утверждения о том, что лежит в этих данных. Оставляем методические
# требования: «не смешивать источники внутри одного тренда» аналитик обязан
# знать до начала работы, а «в командировке смена пояса неотличима от срыва» —
# это уже подсказка, какие события искать.
# --------------------------------------------------------------------------

SHARED_REDACTIONS = (
    r"командировк",
    r"срыв\w*\s+режима",
    r"простуд|заболева|болезн",
    r"ложн\w+\s+тренд",
    r"двойн\w+\s+учёт",
    r"смещени\w*\s+относительно\s+WHOOP",
)

# Термины, при виде которых подготовка прогона обрывается. Уже, чем правила
# редактирования: сюда попадает только то, что не может быть методикой ни при
# какой формулировке.
SHARED_DENYLIST = (
    r"командировк",
    r"срыв\w*\s+режима",
    r"простуд",
)


class Adapter:
    """База. Наследник объявляет пути и реализует build_truth()."""

    name = ""
    script = ""
    truth_file = ""
    data_files = ("records.csv", "records.jsonl")
    docs = ()                       # [(путь от корня репозитория, имя в workspace)]
    drop_headings = ()
    redactions = SHARED_REDACTIONS
    denylist = SHARED_DENYLIST

    def args(self, out_dir: str, seed: int, age: int) -> list:
        return ["--seed", str(seed), "--age", str(age), "--out-dir", out_dir]

    def build_truth(self, raw: dict) -> dict:
        raise NotImplementedError

    def _skeleton(self, calendar: dict, baselines: dict) -> dict:
        return {
            "schema_version": TRUTH_SCHEMA_VERSION,
            "generator": self.name,
            "calendar": calendar,
            "baselines": baselines,
            "events": [],
            "control_windows": [],
            "couplings": [],
            "trends": [],
            "missingness": None,
            "source_divergence": [],
        }


# --------------------------------------------------------------------------
# health-synth
# --------------------------------------------------------------------------

class HealthSynthAdapter(Adapter):
    name = "health-synth"
    script = "health-synth/generate.py"
    truth_file = "answer-key.json"
    docs = (("health-synth/data-format.md", "data-format.md"),)

    def build_truth(self, raw: dict) -> dict:
        params = raw["params"]
        pat = raw["patterns"]
        base = raw["baselines_from_age"]

        truth = self._skeleton(
            calendar={
                "start": params["start_date"],
                "end": params["end_date"],
                "days": params["total_days"],
                "home_tz": params.get("home_tz"),
            },
            baselines={
                "hr_resting": base["hr_resting_bpm"],
                "hrv_rmssd": base["hrv_rmssd_ms"],
                "sleep_duration": base["sleep_need_min"],
            },
        )

        trip = pat["P2_trip"]
        truth["events"].append({
            "id": "P2_trip", "kind": "timezone_shift",
            "from": trip["from"], "to": trip["to"],
            "tail_to": trip["tail"]["to"],
            "tz_from": trip.get("tz_from"), "tz_to": trip.get("tz_to"),
        })
        binge = pat["P4_binge"]
        truth["events"].append({
            "id": "P4_binge", "kind": "regimen_collapse",
            "from": binge["from"], "to": binge["to"],
            "tail_to": binge["tail"]["to"],
        })

        for key, ident in (("P1_baseline", "P1_baseline"), ("P3_steady", "P3_steady")):
            truth["control_windows"].append({
                "id": ident, "from": pat[key]["from"], "to": pat[key]["to"],
            })

        coupling = pat["P5_lag1_coupling"]
        truth["couplings"].append({
            "id": "P5_lag1", "cause": "sleep_onset", "effect": "hrv_rmssd",
            "lag_days": coupling["lag_days"],
            "effect_size": coupling["hrv_pct_per_hour_late"],
            "unit": "pct_per_hour_late",
            "tolerance": 4.0,
        })

        real = pat["P6a_real_trend"]
        truth["trends"].append({
            "id": "P6a_real", "metric": real["metric"], "verdict": "real",
            "total_change": real["total_change_bpm"], "unit": "bpm",
            "tolerance": 1.5,
        })
        decoy = pat["P6b_decoy_trend"]
        truth["trends"].append({
            "id": "P6b_decoy", "metric": decoy["metric"],
            "source": decoy["source"], "verdict": "artifact",
        })

        missing = pat["P7_missingness"]
        gaps = [{"from": missing["battery_dead"]["from"],
                 "to": missing["battery_dead"]["to"], "reason": "battery"}]
        forced = missing.get("forced_not_worn_during_binge") or []
        if forced:
            gaps.append({"from": forced[0], "to": forced[-1], "reason": "not_worn"})
        truth["missingness"] = {"mechanism": "MNAR", "gaps": gaps}

        bias = raw["sources"]["whoop_bias_vs_ring"]
        truth["source_divergence"].append({
            "id": "whoop_vs_ring_hrv", "source_a": "whoop", "source_b": "sber_ring",
            "metric": "hrv_rmssd", "kind": "multiplicative",
            "effect": bias["hrv_rmssd_mult"], "tolerance": 0.04,
        })
        return truth


# --------------------------------------------------------------------------
# health-data
# --------------------------------------------------------------------------

class HealthDataAdapter(Adapter):
    name = "health-data"
    script = "health-data/generate.py"
    truth_file = "manifest.json"
    docs = (("health-data/data-format.md", "data-format.md"),)

    def build_truth(self, raw: dict) -> dict:
        params = raw["params"]
        pat = raw["patterns"]
        base = raw["baselines"]
        start = parse_date(params["start_date"])
        _, end = day_span(start, params["days"])

        truth = self._skeleton(
            calendar={
                "start": params["start_date"],
                "end": end.isoformat(),
                "days": params["days"],
            },
            baselines={
                "hr_resting": base["resting_hr"],
                "hrv_rmssd": base["hrv_rmssd"],
                "sleep_duration": base["sleep_target_min"],
            },
        )

        trip = pat["P2_timezone_trip"]
        truth["events"].append({
            "id": "P2_timezone_trip", "kind": "timezone_shift",
            "from": trip["window"]["dates"][0], "to": trip["window"]["dates"][1],
            "tail_to": trip["tail"]["dates"][1],
            "tz_from": trip["injected"]["timezone_from"],
            "tz_to": trip["injected"]["timezone_to"],
        })
        collapse = pat["P3_routine_collapse"]
        truth["events"].append({
            "id": "P3_routine_collapse", "kind": "regimen_collapse",
            "from": collapse["window"]["dates"][0], "to": collapse["window"]["dates"][1],
            "tail_to": collapse["tail"]["dates"][1],
        })
        illness = pat["P6_illness"]
        truth["events"].append({
            "id": "P6_illness", "kind": "illness",
            "from": illness["window"]["dates"][0], "to": illness["window"]["dates"][1],
            "tail_to": illness["tail"]["dates"][1],
        })

        block = raw.get("background", {}).get("training_block")
        if block:
            truth["events"].append({
                "id": "training_block", "kind": "training_block",
                "from": block["window"]["dates"][0], "to": block["window"]["dates"][1],
                "optional": True,
            })

        truth["control_windows"].append({
            "id": "P1_stable_rhythm",
            "from": pat["P1_stable_rhythm"]["window"]["dates"][0],
            "to": pat["P1_stable_rhythm"]["window"]["dates"][1],
        })
        quiet = raw["negative_controls"]["NC1_quiet_week"]["window"]["dates"]
        truth["control_windows"].append({
            "id": "NC1_quiet_week", "from": quiet[0], "to": quiet[1],
        })

        # В эталоне доля падения HRV за час позднего отбоя; приводим к процентам.
        lag = pat["P4_late_bedtime_hrv"]["injected"]
        truth["couplings"].append({
            "id": "P4_lag1", "cause": "sleep_onset", "effect": "hrv_rmssd",
            "lag_days": 1,
            "effect_size": -100.0 * lag["hrv_drop_per_hour_lag1"],
            "unit": "pct_per_hour_late",
            "tolerance": 3.0,
        })

        bias = raw["negative_controls"]["NC2_source_bias"]["injected"]
        truth["source_divergence"].append({
            "id": "NC2_source_bias", "source_a": "sber_ring", "source_b": "whoop",
            "metric": "hr_resting", "kind": "additive",
            "effect": bias["ring_offset_bpm"],
            "tolerance": max(1.0, 2.0 * bias.get("ring_offset_sd", 0.5)),
        })

        return truth


# --------------------------------------------------------------------------
# synthetic-health
# --------------------------------------------------------------------------

class SyntheticHealthAdapter(Adapter):
    name = "synthetic-health"
    script = "synthetic-health/generate_health_data.py"
    truth_file = "manifest.json"
    docs = (("synthetic-health/data-format.md", "data-format.md"),)

    _KINDS = {
        "P02": "timezone_shift",
        "P04": "regimen_collapse",
        "P05": "illness",
    }

    def build_truth(self, raw: dict) -> dict:
        params = raw["params"]
        base = raw["baselines"]
        by_id = {p["id"]: p for p in raw["patterns"]}

        truth = self._skeleton(
            calendar={
                "start": params["start_date"],
                "end": params["end_date"],
                "days": params["weeks"] * 7,
            },
            baselines={
                "hr_resting": base["resting_hr"],
                "hrv_rmssd": base["hrv_rmssd"],
                "sleep_duration": base["sleep_need_min"],
            },
        )

        for ident, kind in self._KINDS.items():
            pattern = by_id.get(ident)
            if not pattern or "window" not in pattern:
                continue
            window = pattern["window"]
            event = {
                "id": ident, "kind": kind,
                "from": window["start_date"], "to": window["end_date"],
            }
            if window.get("tail_end_date"):
                event["tail_to"] = window["tail_end_date"]
            if ident == "P02":
                event["tz_from"] = pattern.get("tz_from")
                event["tz_to"] = pattern.get("tz_to")
            truth["events"].append(event)

        steady = by_id.get("P03")
        if steady:
            truth["control_windows"].append({
                "id": "P03", "from": steady["window"]["start_date"],
                "to": steady["window"]["end_date"],
            })

        # Связь задана в мс на минуту позднего отбоя; переводим в мс на час.
        coupling = by_id.get("P06")
        if coupling:
            truth["couplings"].append({
                "id": "P06_lag1", "cause": "sleep_onset", "effect": "hrv_rmssd",
                "lag_days": coupling["lag_days"],
                "effect_size": -60.0 * coupling["ms_per_minute"],
                "unit": "ms_per_hour_late",
                "tolerance": 3.0,
            })

        traps = {t["id"]: t for t in raw.get("traps", [])}

        source_trap = traps.get("T1")
        if source_trap:
            truth["source_divergence"].append({
                "id": "T1", "source_a": "sber_ring", "source_b": "whoop",
                "metric": "hrv_rmssd", "kind": "multiplicative",
                "effect": 1.0 + source_trap["hrv_bias_frac"], "tolerance": 0.05,
            })

        gaps = []
        block = traps.get("T2")
        if block:
            ring = raw.get("ring_missing_days", [])
            dates = [d["date"] for d in ring if d.get("reason") == "not_worn"]
            if dates:
                gaps.append({"from": min(dates), "to": max(dates), "reason": "not_worn"})
        if gaps:
            truth["missingness"] = {"mechanism": "MNAR", "gaps": gaps}

        # T4: связи steps → hrv_rmssd в генераторе нет. Заявленная аналитикой
        # связь между ними — выдумка, и оценщик должен её штрафовать.
        if "T4" in traps:
            truth["couplings"].append({
                "id": "T4_absent", "cause": "steps", "effect": "hrv_rmssd",
                "lag_days": None, "effect_size": 0.0, "unit": "absent",
                "tolerance": 0.0, "must_be_absent": True,
            })

        return truth


ADAPTERS = {
    adapter.name: adapter
    for adapter in (HealthSynthAdapter(), HealthDataAdapter(), SyntheticHealthAdapter())
}
