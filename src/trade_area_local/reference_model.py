"""Independent implementation of the documented reference rules.

This model consumes synthetic inputs. It does not reproduce commercial area IDs,
Spark quantile approximations, or an economic optimum.
"""
from dataclasses import asdict
from datetime import date

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from sklearn.cluster import AgglomerativeClustering

from .contracts import EVENT_COLUMNS, MOVE_COLUMNS, ModelResult
from .eligibility import filter_eligible_events, normalize_segment
from .geometry import haversine_matrix, project_local
from .reference_contracts import ReferenceConfig


CORRECTIONS = [
    "Ward usa projeção métrica local, preservando as coordenadas públicas.",
    "A diagonal de CC é excluída por identidade; sobreposição total entre postos distintos é válida.",
    "Cada iteração usa um snapshot e aplica todas as decisões simultaneamente.",
    "CR usa a linha do próprio posto e é recalculado para a composição final.",
    "Percentis contínuos com interpolação linear; não há promessa de identidade bit a bit ao Spark.",
    "Ajuste R/R divide distância por fator de cenário; não representa distância rodoviária.",
    "Núcleos e centros ficam congelados antes da alocação final; sem núcleo, áreas individuais.",
]


def sharing_matrix(station_ids, events, *, exclude_full_overlap=False):
    """Directional intersection of distinct vehicles divided by the station's own total."""
    count = len(station_ids)
    result = np.zeros((count, count), dtype=float)
    if events.empty:
        return result
    pairs = events[["station_id", "vehicle_id"]].drop_duplicates()
    mapping = {sid: i for i, sid in enumerate(station_ids)}
    rows = pairs.station_id.map(mapping)
    pairs = pairs.loc[rows.notna()]
    rows = rows.loc[rows.notna()].to_numpy(dtype=int)
    if not len(pairs):
        return result
    vehicles, unique = pd.factorize(pairs.vehicle_id, sort=True)
    incidence = csr_matrix((np.ones(len(rows), dtype=np.int32), (rows, vehicles)), shape=(count, len(unique)))
    totals = np.asarray(incidence.sum(axis=1)).ravel()
    overlap = (incidence @ incidence.T).toarray()
    np.divide(overlap, totals[:, None], out=result, where=totals[:, None] > 0)
    np.fill_diagonal(result, 0.)
    if exclude_full_overlap:
        result[result == 1.] = 0.
    return result


def competitive_rate(index, members, sharing, distances, distance_floor_km=.1):
    others = np.asarray([j for j in members if j != index], dtype=int)
    if not len(others):
        return 0.
    return float(sharing[index, others].mean() / max(float(distances[index, others].mean()), distance_floor_km))


def rank_candidates(index, station_ids, sharing, physical_distances, peers, count, config):
    """Rank stations first; repeated destination groups still consume candidate slots."""
    candidates = [j for j in peers if j != index and sharing[index, j] > 0]
    def score(j):
        value = sharing[index, j]
        if config.ranking == "cc_per_km":
            value /= max(float(physical_distances[index, j]), config.distance_floor_km)
        return -value, station_ids[j]
    return sorted(candidates, key=score)[:count]


def _adjusted_distances(physical, segments, config):
    adjusted = physical.copy()
    roads = np.asarray(segments) == "rodoviario"
    adjusted[np.ix_(roads, roads)] /= config.road_distance_factor
    return adjusted


def relocate_stage(station_ids, labels, sharing, physical_distances, segments, ufs, config, stage):
    """Run every configured snapshot iteration, including zero-change iterations."""
    if stage not in {"road", "urban", "combined"}:
        raise ValueError("Rodada de referência desconhecida.")
    labels = np.asarray(labels).copy()
    segments, ufs = np.asarray(segments), np.asarray(ufs)
    scope = (np.ones(len(labels), dtype=bool) if stage == "combined" else
             segments == ("rodoviario" if stage == "road" else "urbano"))
    order = sorted(np.flatnonzero(scope), key=lambda i: station_ids[i])
    count = getattr(config, f"candidates_{stage}")
    candidates = {i: rank_candidates(i, station_ids, sharing, physical_distances,
        [j for j in order if config.cross_uf or ufs[i] == ufs[j]], count, config) for i in order}
    score_distances = (_adjusted_distances(physical_distances, segments, config)
                       if stage == "combined" else physical_distances)
    moves, audit = [], []
    for pass_number in range(1, config.max_passes + 1):
        snapshot, decisions = labels.copy(), []
        groups = {group: np.flatnonzero(snapshot == group).tolist() for group in sorted(set(snapshot))}
        for i in order:
            current = snapshot[i]
            before = competitive_rate(i, groups[current], sharing, score_distances, config.distance_floor_km)
            best, best_score = current, before
            for destination in sorted({snapshot[j] for j in candidates[i]}):
                if destination == current:
                    continue
                score = competitive_rate(i, groups[destination], sharing, score_distances, config.distance_floor_km)
                if score > before + 1e-9 and (best == current or score > best_score):
                    best, best_score = destination, score
            if best != current:
                decisions.append((i, best))
                moves.append({"pass": pass_number, "station_id": station_ids[i],
                              "from_area": int(current), "to_area": int(best),
                              "cr_before": before, "cr_after": best_score, "stage": stage})
        for i, destination in decisions:
            labels[i] = destination
        audit.append(dict(stage=stage, **{"pass": pass_number}, moves=len(decisions),
                          areas=len(set(labels))))
    return labels, moves, audit


def initialize_ward(stations, config):
    """Partition by UF/R-U, using floor(n/ratio) before any low-volume removal."""
    labels = np.full(len(stations), -1, dtype=int)
    definitions, next_group = {}, 0
    for (uf, segment), group in stations.groupby(["uf", "segment"], sort=True):
        positions = group.index.to_numpy(dtype=int)
        ratio = config.urban_ratio if segment == "urbano" else config.road_ratio
        count = max(1, min(len(group), len(group) // ratio))
        projected, definition = project_local(group[["latitude", "longitude"]].to_numpy(dtype=float))
        raw = (np.zeros(len(group), dtype=int) if count == 1 else
               AgglomerativeClustering(n_clusters=count, linkage="ward").fit_predict(projected))
        ids = group.station_id.to_numpy()
        order = sorted(set(raw), key=lambda label: min(ids[raw == label]))
        mapping = {label: next_group + j for j, label in enumerate(order)}
        labels[positions] = [mapping[label] for label in raw]
        definitions[f"{uf}|{segment}"] = definition
        next_group += count
    return labels, definitions


def area_profile(segments, config):
    if not len(segments):
        return None
    urban = int((np.asarray(segments) == "urbano").sum())
    return "Rodoviária" if urban == 0 else ("Urbana" if urban / len(segments) > config.profile_urban_threshold else "Mista")


def _distance_statistics(labels, keep, distances):
    metrics, areas = {}, {}
    for group in sorted(set(labels[keep])):
        indices = np.flatnonzero(keep & (labels == group))
        if len(indices) < 2:
            continue
        matrix = distances[np.ix_(indices, indices)]
        values = matrix.sum(axis=1) / (len(indices) - 1)
        metrics.update({int(i): float(value) for i, value in zip(indices, values)})
        areas[int(group)] = float(values.mean())
    return metrics, areas


def _quantile(values, q):
    return float(np.quantile(values, q, method="linear")) if len(values) else None


def remove_outliers(labels, segments, physical_distances, config):
    """Use TA distributions by profile, internal P90, then recomputed physical TA maxima."""
    labels, segments = np.asarray(labels), np.asarray(segments)
    keep = np.ones(len(labels), dtype=bool)
    adjusted = _adjusted_distances(physical_distances, segments, config)
    station_metrics, area_metrics = _distance_statistics(labels, keep, adjusted)
    profiles = {int(g): area_profile(segments[labels == g], config) for g in sorted(set(labels))}
    thresholds, selected, within_thresholds, within_removed = {}, [], {}, []
    percentiles = {"Urbana": config.urban_percentile, "Rodoviária": config.road_percentile,
                   "Mista": config.mixed_percentile}
    for profile, percentile in percentiles.items():
        population = [value for g, value in area_metrics.items() if profiles[g] == profile]
        threshold = _quantile(population, percentile)
        thresholds[profile] = threshold
        if threshold is None:
            continue
        selected.extend(g for g, value in area_metrics.items() if profiles[g] == profile and value > threshold)
    for group in sorted(selected):
        indices = [i for i in station_metrics if labels[i] == group]
        threshold = _quantile([station_metrics[i] for i in indices], config.within_percentile)
        within_thresholds[str(group)] = threshold
        within_removed.extend(i for i in indices if station_metrics[i] > threshold)
    keep[within_removed] = False
    physical_metrics, _ = _distance_statistics(labels, keep, physical_distances)
    maxima = {int(g): max(physical_metrics[i] for i in physical_metrics if labels[i] == g)
              for g in sorted(set(labels[keep])) if any(labels[i] == g for i in physical_metrics)}
    threshold = _quantile(list(maxima.values()), config.area_percentile)
    removed_groups = [int(g) for g, value in maxima.items() if threshold is not None and value >= threshold]
    keep[np.isin(labels, removed_groups)] = False
    audit = dict(percentile_method="linear", profile_thresholds=thresholds,
        profile_populations={name: sum(profiles[g] == name for g in area_metrics) for name in percentiles},
        adjusted_area_means={str(g): v for g, v in area_metrics.items()}, selected_areas=sorted(selected),
        within_thresholds=within_thresholds, within_removed=sorted(within_removed),
        physical_area_maxima={str(g): v for g, v in maxima.items()}, area_threshold=threshold,
        area_removed=removed_groups, retained=int(keep.sum()), removed=int((~keep).sum()),
        profile_rule_status="applied" if area_metrics else "not_applicable_empty_distribution",
        area_rule_status="applied" if maxima else "not_applicable_empty_distribution")
    return keep, ~keep, audit


def _distance_to(point, other):
    return float(haversine_matrix(np.vstack([point, other]))[0, 1])


def allocate_variants(coords, station_ids, ufs, labels, kernel, physical_distances, amounts, config):
    """Frozen standard members/centers, strict radius, revenue then code tie-break."""
    labels, kernel = np.asarray(labels), np.asarray(kernel, dtype=bool)
    final = np.full(len(labels), -1, dtype=int)
    final[kernel] = labels[kernel]
    reasons = np.full(len(labels), "reference_kernel", dtype=object)
    standards = np.flatnonzero(kernel).tolist()
    groups = sorted(set(final[kernel]))
    centers = {g: coords[kernel & (labels == g)].mean(axis=0) for g in groups}
    group_ufs = {g: set(np.asarray(ufs)[kernel & (labels == g)]) for g in groups}
    next_group = max([int(label) for label in labels if label >= 0], default=-1) + 1
    isolated, audit = set(), []
    for i in sorted(np.flatnonzero(~kernel), key=lambda j: station_ids[j]):
        candidates = [j for j in standards if physical_distances[i, j] < config.allocation_radius_km
                      and (config.cross_uf or ufs[i] == ufs[j])]
        if not candidates:
            final[i] = next_group
            isolated.add(next_group)
            next_group += 1
            reasons[i] = "isolated_no_kernel" if not standards else "isolated_no_standard_within_radius"
            audit.append(dict(station_id=station_ids[i], reason=reasons[i], standard_id=None,
                              standard_distance_km=None, center_area=None, center_distance_km=None))
            continue
        nearest = min(candidates, key=lambda j: (physical_distances[i, j], -amounts[j], station_ids[j]))
        nearest_distance = float(physical_distances[i, nearest])
        center_distance, center_group = min((_distance_to(coords[i], centers[g]), int(g)) for g in groups
                                            if config.cross_uf or ufs[i] in group_ufs[g])
        if nearest_distance <= center_distance:
            final[i], reasons[i] = labels[nearest], "assigned_to_standard"
        else:
            final[i], reasons[i] = center_group, "assigned_to_center"
        audit.append(dict(station_id=station_ids[i], reason=reasons[i], standard_id=station_ids[nearest],
                          standard_distance_km=nearest_distance, center_area=center_group,
                          center_distance_km=center_distance))
    return final, reasons, isolated, audit


def _validate(stations, events):
    if not isinstance(stations, pd.DataFrame) or not 1 <= len(stations) <= 1000:
        raise ValueError("Informe de 1 a 1000 postos utilizáveis; nenhum posto é truncado.")
    if not {"station_id", "uf", "segment", "latitude", "longitude"} <= set(stations):
        raise ValueError("Colunas essenciais dos postos ausentes.")
    for name in ("station_id", "uf"):
        if not stations[name].map(lambda v: isinstance(v, str) and bool(v.strip())).all():
            raise ValueError(f"{name} deve conter texto não vazio.")
    if not stations.station_id.is_unique:
        raise ValueError("Identificadores de postos duplicados.")
    if stations.segment.map(normalize_segment).isna().any():
        raise ValueError("Referência requer segmento R/U explicitamente simulado ou informado pelo usuário.")
    haversine_matrix(stations[["latitude", "longitude"]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float))
    if not isinstance(events, pd.DataFrame) or not set(EVENT_COLUMNS) <= set(events):
        raise ValueError("Colunas essenciais de eventos ausentes.")
    for name in ("event_id", "vehicle_id", "station_id"):
        if not events[name].map(lambda v: isinstance(v, str) and bool(v.strip())).all():
            raise ValueError(f"{name} deve conter texto não vazio.")
    if not events.event_id.is_unique or not events.station_id.isin(stations.station_id).all():
        raise ValueError("Identificadores de eventos duplicados ou posto desconhecido.")
    if not events.is_synthetic.map(lambda v: isinstance(v, (bool, np.bool_)) and bool(v)).all():
        raise ValueError("Somente transações explicitamente simuladas são aceitas.")
    values = events[["liters", "amount"]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("Litros e valores devem ser finitos e positivos.")


def _area_id(group):
    return f"TA-REF-{int(group) + 1:05d}"


def run_reference_model(stations: pd.DataFrame, events: pd.DataFrame, config: ReferenceConfig,
                        *, base_date: date) -> ModelResult:
    """Execute all reference conditions and conserve every spatially accepted station once."""
    if not isinstance(config, ReferenceConfig):
        raise ValueError("Configuração de referência inválida.")
    _validate(stations, events)
    output = stations.copy(deep=True).sort_values("station_id", kind="stable").reset_index(drop=True)
    output["segment"] = output.segment.map(normalize_segment)
    coords = output[["latitude", "longitude"]].to_numpy(dtype=float)
    ids, segments, ufs = output.station_id.tolist(), output.segment.tolist(), output.uf.tolist()
    eligibility = filter_eligible_events(output, events, base_date)
    accepted = eligibility.accepted
    aggregates = accepted.groupby("station_id").agg(transaction_count=("event_id", "size"),
        liters=("liters", "sum"), authorized_amount=("amount", "sum"),
        eligible_vehicle_count=("vehicle_id", "nunique"))
    for name in ("transaction_count", "liters", "authorized_amount", "eligible_vehicle_count"):
        output[name] = output.station_id.map(aggregates[name]).fillna(0)
    output[["transaction_count", "eligible_vehicle_count"]] = output[["transaction_count", "eligible_vehicle_count"]].astype(int)
    output["has_transactions"] = output.transaction_count > 0
    positive = np.flatnonzero(output.has_transactions.to_numpy())
    threshold = _quantile(output.transaction_count.to_numpy()[positive], config.transaction_percentile)
    low_volume = (output.transaction_count.to_numpy() < threshold) & output.has_transactions.to_numpy() if threshold is not None else np.zeros(len(output), dtype=bool)
    labels, definitions = np.full(len(output), -1, dtype=int), {}
    if len(positive):
        positive_stations = output.iloc[positive].copy().reset_index(drop=True)
        positive_labels, definitions = initialize_ward(positive_stations, config)
        labels[positive] = positive_labels
    initial_areas = len(set(labels[positive]))
    active = np.flatnonzero(output.has_transactions.to_numpy() & ~low_volume)
    physical = haversine_matrix(coords)
    sharing = sharing_matrix(ids, accepted, exclude_full_overlap=config.exclude_full_overlap)
    moves, iterations = [], []
    active_labels = labels[active]
    for stage in ("road", "urban", "combined"):
        active_labels, stage_moves, stage_audit = relocate_stage([ids[i] for i in active], active_labels,
            sharing[np.ix_(active, active)], physical[np.ix_(active, active)],
            [segments[i] for i in active], [ufs[i] for i in active], config, stage)
        moves.extend(stage_moves)
        iterations.extend(stage_audit)
    labels[active] = active_labels
    keep, removed, outlier_audit = remove_outliers(active_labels, [segments[i] for i in active],
        physical[np.ix_(active, active)], config)
    outlier_audit["within_removed_station_ids"] = [ids[active[i]] for i in outlier_audit["within_removed"]]
    kernel, dispersed = np.zeros(len(output), dtype=bool), np.zeros(len(output), dtype=bool)
    kernel[active[keep]] = True
    dispersed[active[removed]] = True
    final, reasons, isolated, allocation_audit = allocate_variants(coords, ids, ufs, labels, kernel,
        physical, output.authorized_amount.to_numpy(dtype=float), config)
    output["trade_area"] = [_area_id(group) for group in final]
    output["mode"] = "reference"
    output["assignment_reason"] = reasons
    output["is_dispersed"] = dispersed
    output["is_low_volume"] = low_volume
    output["is_standard"] = kernel
    output["transaction_source"] = "synthetic"
    adjusted = _adjusted_distances(physical, segments, config)
    output["cr"] = [competitive_rate(i, np.flatnonzero(final == final[i]), sharing, adjusted, config.distance_floor_km)
                    if output.has_transactions.iloc[i] else np.nan for i in range(len(output))]
    output["area_profile"] = [area_profile(np.asarray(segments)[final == group], config) for group in final]
    center_distances = []
    for group in sorted(set(final)):
        points = coords[final == group]
        center = points.mean(axis=0)
        center_distances.extend(_distance_to(point, center) for point in points)
    move_frame = pd.DataFrame([{**move, "from_area": _area_id(move["from_area"]),
                               "to_area": _area_id(move["to_area"])} for move in moves], columns=[*MOVE_COLUMNS, "stage"])
    area_sizes = {str(area): int(count) for area, count in output.groupby("trade_area", sort=True).size().items()}
    defined_cr = output.cr.dropna()
    metrics = dict(stations_total=len(output), stations_with_transactions=len(positive),
        stations_without_transactions=len(output) - len(positive), trade_areas=len(area_sizes),
        isolated_areas=len(isolated), dispersed_stations=int(dispersed.sum()), fallback_partitions=0,
        moves_total=len(moves), moves_per_pass={str(p): int(n) for p, n in move_frame.groupby("pass").size().items()},
        mean_cr=float(defined_cr.mean()) if len(defined_cr) else None, area_sizes=area_sizes,
        mean_distance_to_center_km=float(np.mean(center_distances)), max_distance_to_center_km=float(max(center_distances)),
        projection_definitions=definitions,
        reference=dict(period=eligibility.audit["period"], config=asdict(config), eligibility=eligibility.audit,
            initial_areas=initial_areas, low_volume={"threshold": threshold,
                "population": len(positive), "station_ids": output.loc[low_volume, "station_id"].tolist()},
            iterations=iterations, outliers=outlier_audit, kernel_station_ids=output.loc[kernel,"station_id"].tolist(),
            allocation=allocation_audit, corrections=CORRECTIONS.copy(),
            candidate_policy="cross_uf" if config.cross_uf else "within_uf",
            no_kernel=not bool(kernel.any())))
    return ModelResult(output, move_frame, metrics)
