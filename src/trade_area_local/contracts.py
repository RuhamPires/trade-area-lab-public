"""Contratos compartilhados pelos módulos, sem acesso a rede ou arquivos."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

NOTICE = "Transações simuladas — resultados para demonstração, sem inferência de mercado real."
STATION_COLUMNS = ["station_id", "name", "cnpj", "municipality", "uf", "latitude", "longitude", "segment", "source", "coordinate_quality"]
EVENT_COLUMNS = ["event_id", "vehicle_id", "station_id", "occurred_at", "liters", "amount", "is_synthetic"]
REJECTED_COLUMNS = ["station_id", "reason"]
MOVE_COLUMNS = ["pass", "station_id", "from_area", "to_area", "cr_before", "cr_after"]


@dataclass
class StationDataset:
    stations: pd.DataFrame
    rejected: pd.DataFrame
    metadata: dict


@dataclass(frozen=True)
class ModelConfig:
    target_size: int = 4
    max_passes: int = 8
    allocation_radius_km: float = 50.0
    max_stations: int = 1000


@dataclass(frozen=True)
class SimulationConfig:
    seed: int = 42
    vehicles: int = 1200
    max_events_per_vehicle: int = 30
    days: int = 90
    end_date: date = field(default_factory=lambda: date.today() - timedelta(days=1))


@dataclass
class ModelResult:
    stations: pd.DataFrame
    moves: pd.DataFrame
    metrics: dict


@dataclass
class RunArtifacts:
    run_dir: Path
    result: ModelResult
    manifest: dict
    stations: pd.DataFrame
    events: pd.DataFrame
    rejected: pd.DataFrame
