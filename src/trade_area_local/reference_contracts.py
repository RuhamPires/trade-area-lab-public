"""Validated, immutable parameters for the documented reference calculation."""
from dataclasses import dataclass, fields
import math
from numbers import Integral, Real


@dataclass(frozen=True)
class ReferenceConfig:
    max_passes: int = 8
    urban_ratio: int = 2
    road_ratio: int = 3
    candidates_road: int = 3
    candidates_urban: int = 1
    candidates_combined: int = 1
    road_distance_factor: float = 5.
    distance_floor_km: float = .1
    transaction_percentile: float = .01
    urban_percentile: float = .85
    road_percentile: float = .8
    mixed_percentile: float = .7
    within_percentile: float = .9
    area_percentile: float = .97
    profile_urban_threshold: float = .8
    allocation_radius_km: float = 500.
    exclude_full_overlap: bool = False
    cross_uf: bool = True
    ranking: str = "cc_per_km"

    def __post_init__(self):
        integer_names = {"max_passes", "urban_ratio", "road_ratio", "candidates_road",
                         "candidates_urban", "candidates_combined"}
        boolean_names = {"exclude_full_overlap", "cross_uf"}
        for field in fields(self):
            name, value = field.name, getattr(self, field.name)
            if name in integer_names:
                upper = 8 if name == "max_passes" else 1000
                valid = not isinstance(value, bool) and isinstance(value, Integral) and 1 <= value <= upper
            elif name in boolean_names:
                valid = isinstance(value, bool)
            elif name == "ranking":
                valid = isinstance(value, str) and value in {"cc_per_km", "cc"}
            else:
                valid = not isinstance(value, bool) and isinstance(value, Real) and math.isfinite(value)
                if valid:
                    valid = (0 <= value <= 1 if "percentile" in name or name == "profile_urban_threshold"
                             else value > 0)
            if not valid:
                raise ValueError(f"Parâmetro de referência {name} inválido.")
            if name in integer_names:
                object.__setattr__(self, name, int(value))
            elif name not in boolean_names and name != "ranking":
                object.__setattr__(self, name, float(value))
