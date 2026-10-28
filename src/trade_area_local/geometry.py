"""Geometria local: entradas latitude/longitude em EPSG:4326 e distâncias km."""

import numpy as np
from pyproj import CRS, Transformer, network

network.set_network_enabled(False)
EARTH_RADIUS_KM = 6371.0088


def _coordinates(coords: np.ndarray) -> np.ndarray:
    values = np.asarray(coords, dtype=float)
    if values.ndim != 2 or values.shape[1] != 2 or not len(values):
        raise ValueError("Informe pelo menos um par de latitude e longitude.")
    if not np.isfinite(values).all() or (np.abs(values[:, 0]) > 90).any() or (np.abs(values[:, 1]) > 180).any():
        raise ValueError("Coordenadas fora dos limites de latitude/longitude.")
    return values


def haversine_matrix(coords: np.ndarray) -> np.ndarray:
    values = np.radians(_coordinates(coords))
    lat, lon = values[:, 0], values[:, 1]
    dlat, dlon = lat[:, None] - lat, lon[:, None] - lon
    a = np.sin(dlat / 2) ** 2 + np.cos(lat[:, None]) * np.cos(lat) * np.sin(dlon / 2) ** 2
    distance = 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    np.fill_diagonal(distance, 0.)
    return distance


def spherical_center(coords: np.ndarray) -> np.ndarray:
    values = _coordinates(coords)
    lat, lon = np.radians(values).T
    vector = np.array([np.mean(np.cos(lat) * np.cos(lon)), np.mean(np.cos(lat) * np.sin(lon)), np.mean(np.sin(lat))])
    if np.linalg.norm(vector) < 1e-12:
        return values[0].copy()
    return np.degrees([np.arctan2(vector[2], np.hypot(vector[0], vector[1])), np.arctan2(vector[1], vector[0])])


def project_local(coords: np.ndarray) -> tuple[np.ndarray, str]:
    values = _coordinates(coords)
    lat, lon = spherical_center(values)
    definition = f"+proj=aeqd +lat_0={lat:.12f} +lon_0={lon:.12f} +datum=WGS84 +units=m +no_defs"
    transformer = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_proj4(definition), always_xy=True)
    x, y = transformer.transform(values[:, 1], values[:, 0])
    projected = np.column_stack([x, y])
    if not np.isfinite(projected).all():
        raise ValueError("Não foi possível projetar as coordenadas deste recorte.")
    return projected, definition
