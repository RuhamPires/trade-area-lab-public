"""Three-state eligibility for artificial reference events; unknown never passes."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from numbers import Real

import numpy as np
import pandas as pd


@dataclass
class EligibilityResult:
    accepted: pd.DataFrame
    classified: pd.DataFrame
    audit: dict


def normalize_segment(value):
    if isinstance(value, str):
        return {"U": "urbano", "URBANO": "urbano", "R": "rodoviario",
                "RODOVIARIO": "rodoviario", "RODOVIÁRIO": "rodoviario"}.get(value.strip().upper())
    return None


def _period(base_date):
    if not isinstance(base_date, date) or isinstance(base_date, datetime):
        raise ValueError("Data-base deve ser uma data sem horário.")
    month = base_date.year * 12 + base_date.month - 1
    start_year, start_month = divmod(month - 4, 12)
    if start_year < 1:
        raise ValueError("Data-base insuficiente para quatro meses anteriores.")
    start = date(start_year, start_month + 1, 1)
    end = date(base_date.year, base_date.month, 1) - timedelta(days=1)
    return start, end


def _truth(value, expected):
    return int(bool(value) == expected) if isinstance(value, (bool, np.bool_)) else -1


def _number(value, expected):
    if isinstance(value, bool) or not isinstance(value, Real) or not np.isfinite(value):
        return -1
    return int(value == expected)


def _text(value, expected=None, different=False):
    if not isinstance(value, str):
        return -1
    normalized = value.strip().upper()
    if expected is None:
        return int(bool(normalized))
    if not normalized:
        return -1
    return int(normalized != expected if different else normalized == expected)


def _local_times(series):
    # Naive event clocks are local. Offset-bearing timestamps preserve their instant.
    parsed = pd.to_datetime(series, errors="coerce", utc=True, format="mixed")
    aware = series.astype(str).str.contains(r"(?:Z|[+-]\d{2}:?\d{2})\s*$", regex=True)
    result = parsed.dt.tz_convert("America/Sao_Paulo").copy()
    naive = ~aware & parsed.notna()
    if naive.any():
        result.loc[naive] = parsed.loc[naive].dt.tz_localize(None).dt.tz_localize("America/Sao_Paulo")
    return result


def filter_eligible_events(stations: pd.DataFrame, events: pd.DataFrame, base_date: date) -> EligibilityResult:
    """Classify each rule and retain all exclusion reasons without publishing raw rows."""
    start, end = _period(base_date)
    frame = events.copy(deep=True).reset_index(drop=True)
    rules = {}

    def values(name):
        return frame[name] if name in frame else pd.Series(None, index=frame.index, dtype=object)

    for name, expected in (("vehicle_valid", True), ("cancelled", False),
                           ("establishment_disabled", False)):
        rules[name] = values(name).map(lambda v: _truth(v, expected)).to_numpy(dtype=int)
    for name, expected in (("vehicle_status", 2), ("vehicle_type", 1),
                           ("establishment_type", 3), ("service_type", 1)):
        rules[name] = values(name).map(lambda v: _number(v, expected)).to_numpy(dtype=int)
    rules["plate_token"] = values("plate_token").map(_text).to_numpy(dtype=int)
    rules["shell_partner"] = values("shell_partner").map(lambda v: _text(v, "N")).to_numpy(dtype=int)
    rules["billing"] = values("billing").map(lambda v: _text(v, "N", different=True)).to_numpy(dtype=int)
    address = []
    for has_type10, address_type, principal in zip(values("has_type10"), values("address_type"),
                                                  values("is_principal_address")):
        has = _truth(has_type10, True)
        address.append(_number(address_type, 10) if has == 1 else
                       (_truth(principal, True) if has == 0 else -1))
    rules["address"] = np.asarray(address, dtype=int)
    segment = (stations.set_index("station_id")["segment"].map(normalize_segment).to_dict()
               if {"station_id", "segment"} <= set(stations) else {})
    rules["location"] = values("station_id").map(lambda sid: 1 if segment.get(sid) else -1).to_numpy(dtype=int)
    times = _local_times(values("occurred_at"))
    lower = pd.Timestamp(start, tz="America/Sao_Paulo")
    upper = pd.Timestamp(end + timedelta(days=1), tz="America/Sao_Paulo")
    rules["period"] = np.where(times.isna(), -1, ((times >= lower) & (times < upper)).astype(int))
    states = np.column_stack(list(rules.values()))
    rejected, unknown = (states == 0).any(axis=1), (states == -1).any(axis=1)
    statuses = np.where(rejected, "rejected", np.where(unknown, "unknown", "accepted"))
    frame["eligibility_status"] = statuses
    reasons = [[f"{name}:{'unknown' if column[i] == -1 else 'rejected'}"
                for name, column in rules.items() if column[i] != 1] for i in range(len(frame))]
    frame["eligibility_reasons"] = reasons
    per_rule = {name: {"accepted": int((column == 1).sum()), "rejected": int((column == 0).sum()),
                       "unknown": int((column == -1).sum())} for name, column in rules.items()}
    reason_counts = {}
    for row in reasons:
        for reason in row:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    audit = dict(total=len(frame), accepted=int((statuses == "accepted").sum()),
                 rejected=int((statuses == "rejected").sum()), unknown=int((statuses == "unknown").sum()),
                 excluded=int((statuses != "accepted").sum()), reasons=reason_counts, per_rule=per_rule,
                 period={"start": start.isoformat(), "end": end.isoformat()},
                 notice="Campos de elegibilidade artificiais; desconhecidos não aprovam eventos.")
    if events.attrs.get("reference_generator") == "reference_demo_80_10_10_v1":
        audit["simulation"] = dict(source="synthetic", generator="reference_demo_80_10_10_v1",
            planned_proportions={"accepted": .8, "rejected": .1, "unknown": .1},
            cycle_length=10, calibration="demonstration_only",
            notice="Composição artificial para demonstrar filtros e compartilhamento; sem calibração de mercado.")
    return EligibilityResult(frame.loc[statuses == "accepted"].copy(), frame, audit)
