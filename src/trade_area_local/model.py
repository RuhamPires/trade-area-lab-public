"""Deterministic local grouping with explicitly synthetic behavioral inputs."""

import math
from numbers import Integral, Real

import numpy as np
import pandas as pd
from sklearn.cluster import AgglomerativeClustering

from .contracts import EVENT_COLUMNS, MOVE_COLUMNS, STATION_COLUMNS, ModelConfig, ModelResult
from .geometry import haversine_matrix, project_local, spherical_center


def sharing_matrix(station_ids: list[str], events: pd.DataFrame) -> np.ndarray:
    """Return directional distinct-vehicle overlap; the diagonal is excluded."""
    vehicles = events.groupby("station_id", sort=False)["vehicle_id"].agg(set).to_dict()
    sets = [vehicles.get(sid, set()) for sid in station_ids]
    sharing = np.zeros((len(sets), len(sets)), dtype=float)
    for i, own in enumerate(sets):
        if own:
            for j, other in enumerate(sets):
                if i != j:
                    sharing[i, j] = len(own & other) / len(own)
    return sharing


def competitive_rate(index: int, members: list[int], sharing: np.ndarray,
                     distances: np.ndarray) -> float:
    """Score the evaluated station against the other members of a group."""
    others = [j for j in members if j != index]
    if not others:
        return 0.0
    return float(np.mean(sharing[index, others]) / max(float(np.mean(distances[index, others])), .1))


def _relocate(station_ids, labels, sharing, distances, max_passes):
    """Sequential strict improvements; station and candidate ordering is stable."""
    labels = labels.copy()
    order = sorted(range(len(station_ids)), key=lambda i: station_ids[i])
    candidates = {
        i: sorted((j for j in order if i != j and sharing[i, j] > 0),
                  key=lambda j: (-sharing[i, j], station_ids[j]))[:3]
        for i in order
    }
    moves = []
    for pass_number in range(1, min(max_passes, 8) + 1):
        changed = False
        for i in order:
            current = labels[i]
            before = competitive_rate(i, np.flatnonzero(labels == current).tolist(), sharing, distances)
            best, best_score = current, before
            for destination in sorted({labels[j] for j in candidates[i]}):
                score = competitive_rate(i, np.flatnonzero(labels == destination).tolist(), sharing, distances)
                if score > before + 1e-9 and (best == current or score > best_score):
                    best, best_score = destination, score
            if best != current:
                labels[i] = best
                changed = True
                moves.append({"pass": pass_number, "station_id": station_ids[i],
                              "from_area": current, "to_area": best,
                              "cr_before": before, "cr_after": best_score})
        if not changed:
            break
    return labels, moves


def _validate(stations, events, config):
    for name, lower, upper in (("target_size", 2, None), ("max_passes", 1, 8),
                               ("max_stations", 1, 1000)):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, Integral) or value < lower or (upper and value > upper):
            raise ValueError(f"Parâmetro {name} inválido: esperado inteiro entre {lower} e {upper or 'sem limite'}.")
    radius = config.allocation_radius_km
    if isinstance(radius, bool) or not isinstance(radius, Real) or not math.isfinite(radius) or radius <= 0:
        raise ValueError("allocation_radius_km deve ser finito e positivo.")
    for frame, columns, name in ((stations, STATION_COLUMNS, "postos"), (events, EVENT_COLUMNS, "eventos")):
        if not isinstance(frame, pd.DataFrame) or not set(columns).issubset(frame.columns):
            raise ValueError(f"Colunas obrigatórias ausentes em {name}.")
    if stations.empty:
        raise ValueError("Entrada vazia: informe pelo menos um posto utilizável.")
    if len(stations) > config.max_stations:
        raise ValueError(f"Limite de {config.max_stations} postos (máximo 1000). Reduza o recorte; nenhum posto foi truncado.")
    for frame, names in ((stations, ("station_id", "uf", "segment")),
                         (events, ("event_id", "vehicle_id", "station_id"))):
        for name in names:
            if not frame[name].map(lambda x: isinstance(x, str) and bool(x.strip())).all():
                raise ValueError(f"{name} deve conter texto não vazio.")
    if stations.station_id.duplicated().any() or events.event_id.duplicated().any():
        raise ValueError("Identificadores de postos e eventos devem ser únicos.")
    coords = stations[["latitude", "longitude"]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(coords).all() or (np.abs(coords[:, 0]) > 90).any() or (np.abs(coords[:, 1]) > 180).any():
        raise ValueError("Coordenadas de latitude/longitude inválidas.")
    if not events.station_id.isin(stations.station_id).all():
        raise ValueError("Eventos referenciam station_id desconhecido.")
    if not events.is_synthetic.map(lambda x: isinstance(x, (bool, np.bool_)) and bool(x)).all():
        raise ValueError("Somente eventos explicitamente sintéticos são aceitos.")
    if len(events):
        times = pd.to_datetime(events.occurred_at, errors="coerce", utc=True, format="mixed")
        if times.isna().any():
            raise ValueError("Datas de eventos inválidas.")
        values = events[["liters", "amount"]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError("Litros e valores devem ser finitos e positivos.")


def _ward(projected, station_ids, target_size):
    count = len(projected)
    groups = max(1, min(count, math.ceil(count / target_size)))
    raw = (np.zeros(count, dtype=int) if groups == 1 else
           AgglomerativeClustering(n_clusters=groups, linkage="ward").fit_predict(projected))
    # Canonicalize estimator labels by each group's first stable station ID.
    order = sorted(set(raw), key=lambda group: min(station_ids[i] for i in range(count) if raw[i] == group))
    mapping = {group: i for i, group in enumerate(order)}
    return np.array([mapping[group] for group in raw], dtype=int)


def _distance_to(point, center):
    return float(haversine_matrix(np.vstack([point, center]))[0, 1])


def _final_allocation(coords, active, labels, radius):
    """Freeze retained core centers before assigning any displaced/orphan point."""
    final = np.full(len(coords), -1, dtype=int)
    final[active] = labels
    dispersed = np.zeros(len(coords), dtype=bool)
    reasons = np.full(len(coords), "behavioral", dtype=object)
    original_centers = {int(group): spherical_center(coords[active[labels == group]]) for group in sorted(set(labels))}
    for i in active:
        dispersed[i] = _distance_to(coords[i], original_centers[int(final[i])]) > radius
    retained = active[~dispersed[active]]
    fallback = not len(retained)
    if fallback:
        centers = original_centers
        reasons[active[dispersed[active]]] = "dispersed_retained_fallback"
        pending = np.flatnonzero(final == -1)
    else:
        final[dispersed] = -1
        centers = {int(group): spherical_center(coords[retained[final[retained] == group]])
                   for group in sorted(set(final[retained]))}
        pending = np.flatnonzero(final == -1)
    next_group = int(max(labels)) + 1
    isolated = set()
    for i in pending:
        distance, group = min((_distance_to(coords[i], center), group) for group, center in centers.items())
        if distance <= radius:
            final[i] = group
            reasons[i] = "dispersed_reassigned" if dispersed[i] else "no_transactions_assigned"
        else:
            final[i] = next_group
            isolated.add(next_group)
            next_group += 1
            reasons[i] = "isolated"
    return final, dispersed, reasons, isolated, fallback


def run_model(stations: pd.DataFrame, events: pd.DataFrame, config: ModelConfig) -> ModelResult:
    """Run within UF/segment boundaries and conserve every validated station."""
    _validate(stations, events, config)
    output = stations.copy().sort_values("station_id", kind="stable").reset_index(drop=True)
    output[["latitude", "longitude"]] = output[["latitude", "longitude"]].astype(float)
    output["trade_area"] = ""
    output["mode"] = ""
    output["assignment_reason"] = ""
    output["has_transactions"] = output.station_id.isin(events.station_id)
    output["is_dispersed"] = False
    output["cr"] = np.nan
    output["transaction_source"] = "synthetic"
    moves = []
    partition_modes, projection_definitions = {}, {}
    isolated_areas, fallback_partitions = 0, 0
    for partition_number, ((uf, segment), partition) in enumerate(output.groupby(["uf", "segment"], sort=True), 1):
        positions = partition.index.to_numpy()
        ids = partition.station_id.tolist()
        coords = partition[["latitude", "longitude"]].to_numpy(dtype=float)
        projected, definition = project_local(coords)
        key = f"{uf}|{segment}"
        projection_definitions[key] = definition
        active = np.flatnonzero(partition.has_transactions.to_numpy())
        mode = "behavioral" if len(active) else "geographic_only"
        partition_modes[key] = mode
        prefix = f"TA-P{partition_number:04d}-"
        if not len(active):
            labels = _ward(projected, ids, config.target_size)
            dispersed = np.zeros(len(coords), dtype=bool)
            reasons = np.full(len(coords), "geographic_only", dtype=object)
        else:
            labels = _ward(projected[active], [ids[i] for i in active], config.target_size)
            sharing = sharing_matrix(ids, events)
            distances = haversine_matrix(coords)
            labels, partition_moves = _relocate([ids[i] for i in active], labels,
                sharing[np.ix_(active, active)], distances[np.ix_(active, active)], config.max_passes)
            for move in partition_moves:
                moves.append({**move, "from_area": f"{prefix}G{int(move['from_area']) + 1:04d}",
                              "to_area": f"{prefix}G{int(move['to_area']) + 1:04d}"})
            labels, dispersed, reasons, isolated, fallback = _final_allocation(
                coords, active, labels, config.allocation_radius_km)
            isolated_areas += len(isolated)
            fallback_partitions += int(fallback)
            for i in active:
                output.loc[positions[i], "cr"] = competitive_rate(
                    i, np.flatnonzero(labels == labels[i]).tolist(), sharing, distances)
        output.loc[positions, "trade_area"] = [f"{prefix}G{int(group) + 1:04d}" for group in labels]
        output.loc[positions, "mode"] = mode
        output.loc[positions, "assignment_reason"] = reasons
        output.loc[positions, "is_dispersed"] = dispersed
    move_frame = pd.DataFrame(moves, columns=MOVE_COLUMNS)
    area_sizes = {str(area): int(count) for area, count in output.groupby("trade_area", sort=True).size().items()}
    center_distances = []
    for _, group in output.groupby("trade_area", sort=True):
        coords = group[["latitude", "longitude"]].to_numpy(dtype=float)
        center = spherical_center(coords)
        center_distances.extend(_distance_to(point, center) for point in coords)
    defined_cr = output.cr.dropna()
    metrics = {
        "stations_total": int(len(output)),
        "stations_with_transactions": int(output.has_transactions.sum()),
        "stations_without_transactions": int((~output.has_transactions).sum()),
        "trade_areas": len(area_sizes), "isolated_areas": isolated_areas,
        "dispersed_stations": int(output.is_dispersed.sum()), "fallback_partitions": fallback_partitions,
        "moves_total": len(moves),
        "moves_per_pass": {str(p): int(count) for p, count in move_frame.groupby("pass", sort=True).size().items()},
        "mean_cr": float(defined_cr.mean()) if len(defined_cr) else None,
        "area_sizes": area_sizes,
        "mean_distance_to_center_km": float(np.mean(center_distances)),
        "max_distance_to_center_km": float(max(center_distances)),
        "partition_modes": partition_modes, "projection_definitions": projection_definitions,
    }
    return ModelResult(output, move_frame, metrics)
