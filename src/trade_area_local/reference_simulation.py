"""Deterministic reference scenarios, independent of any commercial data or rules engine."""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .contracts import SimulationConfig
from .eligibility import _period
from .geometry import haversine_matrix


def reference_period(base_date: date) -> tuple[date, date]:
    """Four completed months in the America/Sao_Paulo calendar, inclusive."""
    return _period(base_date)


_GOOD = dict(vehicle_valid=True, vehicle_status=2, vehicle_type=1, cancelled=False,
             establishment_disabled=False, establishment_type=3, shell_partner="N",
             service_type=1, billing="S", has_type10=True, address_type=10,
             is_principal_address=False)
_FAILURES = [dict(vehicle_valid=False), dict(vehicle_status=1), dict(vehicle_type=2),
             dict(plate_token=""), dict(cancelled=True), dict(establishment_disabled=True),
             dict(establishment_type=1), dict(shell_partner="S"), dict(service_type=2),
             dict(billing="N"), dict(has_type10=True, address_type=1)]
_UNKNOWN = [dict(vehicle_valid=None), dict(vehicle_status=None), dict(vehicle_type=None),
            dict(plate_token=None), dict(cancelled=None), dict(establishment_disabled=None),
            dict(establishment_type=None), dict(shell_partner=None), dict(service_type=None),
            dict(billing=None), dict(has_type10=None)]


def simulate_reference_inputs(stations: pd.DataFrame, simulation: SimulationConfig,
                              base_date: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep public coordinates/IDs and generate separate scenario R/U and fake eligibility."""
    start, end = reference_period(base_date)
    for name, value, low, high in (("seed", simulation.seed, 0, 2**32 - 1),
                                  ("vehicles", simulation.vehicles, 0, 10000),
                                  ("max_events_per_vehicle", simulation.max_events_per_vehicle, 1, 100)):
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or not low <= value <= high:
            raise ValueError(f"{name} deve ser inteiro entre {low} e {high}.")
    if not isinstance(stations, pd.DataFrame) or not 1 <= len(stations) <= 1000:
        raise ValueError("Informe de 1 a 1000 postos utilizáveis.")
    if not {"station_id", "segment", "latitude", "longitude"} <= set(stations):
        raise ValueError("Colunas de postos ausentes para a simulação de referência.")
    output = stations.copy(deep=True).sort_values("station_id", kind="stable").reset_index(drop=True)
    if not output.station_id.map(lambda v: isinstance(v, str) and bool(v.strip())).all() or not output.station_id.is_unique:
        raise ValueError("Identificadores de postos devem ser textos únicos e preenchidos.")
    rng = np.random.default_rng(simulation.seed)
    output["public_segment"] = output["public_segment"] if "public_segment" in output else output.segment
    segment = rng.choice(["urbano", "rodoviario"], size=len(output))
    segment[0] = "urbano"
    if len(output) > 1:
        segment[1] = "rodoviario"
    output["segment"] = segment
    output["scenario_segment"] = segment
    output["segment_source"] = "synthetic"
    output["segment_seed"] = int(simulation.seed)
    distances = haversine_matrix(output[["latitude", "longitude"]].to_numpy(dtype=float))
    ids = output.station_id.to_numpy()
    active_count = max(1, int(np.ceil(len(output) * .85)))
    if len(output) > 1:
        active_count = min(active_count, len(output) - 1)
    active = np.sort(rng.choice(len(output), size=active_count, replace=False))
    probability = np.exp(-distances[np.ix_(active, active)] / 5.)
    probability /= probability.sum(axis=1, keepdims=True)
    probability *= .35
    probability[np.diag_indices_from(probability)] += .65
    days = (end - start).days + 1
    rows = []
    first = pd.Timestamp(start, tz="America/Sao_Paulo")
    for vehicle in range(simulation.vehicles):
        count = int(rng.integers(1, simulation.max_events_per_vehicle + 1))
        anchor = int(rng.integers(active_count))
        chosen = rng.choice(active, size=count, p=probability[anchor])
        for station, second in zip(chosen, rng.integers(0, days * 86400, size=count)):
            token = f"DEMO-V{vehicle + 1:06d}"
            liters = round(float(rng.uniform(8, 95)), 2)
            row = dict(event_id=f"DEMO-E{len(rows) + 1:08d}", vehicle_id=token,
                       station_id=ids[station], occurred_at=first + timedelta(seconds=int(second)),
                       liters=liters, amount=round(liters * float(rng.uniform(5, 7.5)), 2),
                       is_synthetic=True, plate_token=token, **_GOOD)
            block, slot = divmod(len(rows), 10)
            # The 80/10/10 mix is chosen to demonstrate rules and shared behavior;
            # it is not estimated from any real transaction population.
            condition = block % len(_FAILURES)
            if slot == 8:
                row.update(_FAILURES[condition])
            elif slot == 9:
                row.update(_UNKNOWN[condition])
            elif slot % 2:
                row.update(has_type10=False, address_type=1, is_principal_address=True)
            rows.append(row)
    columns = ["event_id", "vehicle_id", "station_id", "occurred_at", "liters", "amount",
               "is_synthetic", "plate_token", *_GOOD]
    events = pd.DataFrame(rows, columns=columns)
    if events.empty:
        events["occurred_at"] = pd.Series(dtype="datetime64[ns, America/Sao_Paulo]")
    events.attrs["reference_generator"] = "reference_demo_80_10_10_v1"
    return output, events
