"""Postos fictícios opcionais e abastecimentos inteiramente sintéticos."""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .contracts import EVENT_COLUMNS, REJECTED_COLUMNS, STATION_COLUMNS, SimulationConfig, StationDataset
from .geometry import haversine_matrix


def _integer(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or not low <= value <= high:
        raise ValueError(f"{name} deve ser inteiro entre {low} e {high}.")
    return int(value)


def make_demo_stations(n: int = 80, seed: int = 42) -> StationDataset:
    """Demonstração fictícia: não representa estabelecimentos da ANP."""
    n = _integer(n, "Quantidade de postos", 1, 1000)
    seed = _integer(seed, "Semente", 0, 2**32 - 1)
    rng = np.random.default_rng(seed)
    centers = np.array([[-22.90, -47.06], [-22.96, -47.14], [-22.83, -47.03], [-22.88, -47.18]])
    points = centers[np.arange(n) % len(centers)] + rng.normal(0, .012, size=(n, 2))
    stations = pd.DataFrame({
        "station_id": [f"SIM-P{i+1:04d}" for i in range(n)],
        "name": [f"Posto fictício {i+1:03d}" for i in range(n)],
        "cnpj": [""] * n,
        "municipality": ["MUNICIPIO SINTETICO"] * n,
        "uf": ["SP"] * n,
        "latitude": points[:, 0], "longitude": points[:, 1],
        "segment": ["urbano" if i % 2 == 0 else "rodoviario" for i in range(n)],
        "source": ["synthetic"] * n,
        "coordinate_quality": ["synthetic"] * n,
    }, columns=STATION_COLUMNS)
    return StationDataset(stations, pd.DataFrame(columns=REJECTED_COLUMNS), {
        "source": "synthetic", "municipality": "MUNICIPIO SINTETICO", "uf": "SP",
        "seed": seed, "received_records": n, "accepted_stations": n,
        "rejected_stations": 0, "crs": "EPSG:4326",
    })


def simulate_events(stations: pd.DataFrame, config: SimulationConfig) -> pd.DataFrame:
    """Visitas com âncora geográfica; o gerador não consulta Trade Areas."""
    vehicles = _integer(config.vehicles, "Veículos", 0, 10000)
    maximum = _integer(config.max_events_per_vehicle, "Eventos por veículo", 1, 100)
    days = _integer(config.days, "Janela em dias", 1, 365)
    seed = _integer(config.seed, "Semente", 0, 2**32 - 1)
    if not isinstance(config.end_date, date) or isinstance(config.end_date, datetime):
        raise ValueError("Data final deve ser uma data sem horário.")
    if not 1 <= len(stations) <= 1000 or not set(STATION_COLUMNS) <= set(stations):
        raise ValueError("Informe de 1 a 1.000 postos normalizados.")
    ordered = stations.sort_values("station_id", kind="stable").reset_index(drop=True)
    if ordered.station_id.isna().any() or not ordered.station_id.is_unique:
        raise ValueError("Os identificadores dos postos devem ser únicos e preenchidos.")
    distances = haversine_matrix(ordered[["latitude", "longitude"]].to_numpy())
    empty = pd.DataFrame({
        "event_id": pd.Series(dtype="str"), "vehicle_id": pd.Series(dtype="str"),
        "station_id": pd.Series(dtype="str"), "occurred_at": pd.Series(dtype="datetime64[ns]"),
        "liters": pd.Series(dtype="float64"), "amount": pd.Series(dtype="float64"),
        "is_synthetic": pd.Series(dtype="bool"),
    })
    if vehicles == 0:
        return empty[EVENT_COLUMNS]
    rng = np.random.default_rng(seed)
    active_count = max(1, min(len(ordered) - 1, int(np.ceil(len(ordered) * .85)))) if len(ordered) > 1 else 1
    active = np.sort(rng.choice(len(ordered), size=active_count, replace=False))
    ids = ordered.station_id.to_numpy()
    probabilities = np.exp(-distances[np.ix_(active, active)] / 5.)
    probabilities /= probabilities.sum(axis=1, keepdims=True)
    probabilities *= .35
    probabilities[np.diag_indices_from(probabilities)] += .65
    start = pd.Timestamp(config.end_date - timedelta(days=days - 1))
    frames = []
    for vehicle in range(vehicles):
        count = int(rng.integers(1, maximum + 1))
        anchor = int(rng.integers(active_count))
        chosen = rng.choice(active, size=count, p=probabilities[anchor])
        seconds = rng.integers(0, days * 86400, size=count)
        liters = np.round(rng.uniform(8., 95., size=count), 2)
        frames.append(pd.DataFrame({
            "vehicle_id": f"SIM-V{vehicle+1:06d}", "station_id": ids[chosen],
            "occurred_at": start + pd.to_timedelta(seconds, unit="s"), "liters": liters,
            "amount": np.round(liters * rng.uniform(5., 7.5, size=count), 2), "is_synthetic": True,
        }))
    events = pd.concat(frames, ignore_index=True)
    events.insert(0, "event_id", [f"SIM-E{i+1:08d}" for i in range(len(events))])
    return events[EVENT_COLUMNS]
