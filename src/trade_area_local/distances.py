"""Local WGS84 distances, linear-memory neighbors and optional loopback OSRM.

All public coordinate pairs are (latitude, longitude). Invalid inputs raise
ValueError; unavailable or malformed routing responses raise RuntimeError.
No geographic distance is substituted for a failed road route.
"""

from ipaddress import ip_address
from http.client import HTTPException
import json
from numbers import Integral, Real
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import pandas as pd
from pyproj import Geod


_GEOD = Geod(ellps="WGS84")
_EARTH_RADIUS_KM = 6371.0088
_RADIUS_TOLERANCE_KM = 1e-9


def _number(value, name, *, minimum=None):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} deve ser um número finito.")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} fora do intervalo permitido.") from exc
    if not np.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"{name} fora do intervalo permitido.")
    return value


def _point(point):
    if not isinstance(point, (tuple, list)) or len(point) != 2:
        raise ValueError("Coordenadas exigem um par (latitude, longitude).")
    latitude = _number(point[0], "Latitude")
    longitude = _number(point[1], "Longitude")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Latitude/longitude fora do intervalo permitido.")
    return latitude, longitude


def _method(method):
    if not isinstance(method, str) or method not in ("geodesic", "haversine"):
        raise ValueError("Método deve ser geodesic ou haversine.")


def _distances(origin, latitudes, longitudes, method):
    if method == "geodesic":
        origin_lat = np.full_like(latitudes, origin[0], dtype=float)
        origin_lon = np.full_like(longitudes, origin[1], dtype=float)
        return np.asarray(_GEOD.inv(origin_lon, origin_lat, longitudes, latitudes)[2]) / 1000
    lat1 = np.radians(origin[0])
    lat2 = np.radians(latitudes)
    delta_lat = lat2 - lat1
    delta_lon = np.radians(longitudes - origin[1])
    hav = np.sin(delta_lat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(delta_lon / 2) ** 2
    # Round-off near antipodes must not produce sqrt(negative) or arcsin > 1.
    return 2 * _EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(hav, 0, 1)))


def distance_km(origin, destination, *, method="geodesic") -> float:
    """Compute geographic kilometers locally, defaulting to WGS84 geodesics."""
    origin, destination = _point(origin), _point(destination)
    _method(method)
    return float(_distances(origin, np.array([destination[0]]), np.array([destination[1]]), method)[0])


def nearby_stations(frame: pd.DataFrame, origin, *, radius_km=50.0, limit=100,
                    exclude_station_id=None, method="geodesic") -> pd.DataFrame:
    """Filter in one vector pass and order matches by distance, then text ID.

    Allocations are O(N); no pairwise matrix is constructed. Sorting operates
    only on rows inside the requested radius. The input frame is preserved.
    Radius inclusion allows one micrometer of floating-point round-off.
    """
    origin = _point(origin)
    _method(method)
    radius_km = _number(radius_km, "Raio", minimum=0)
    if isinstance(limit, (bool, np.bool_)) or not isinstance(limit, Integral) or limit < 0:
        raise ValueError("Limite deve ser um inteiro não negativo.")
    required = {"station_id", "latitude", "longitude", "has_coordinates"}
    if not isinstance(frame, pd.DataFrame) or not required.issubset(frame.columns):
        raise ValueError("Catálogo deve conter IDs, coordenadas e has_coordinates.")
    if not frame["station_id"].map(lambda value: isinstance(value, str) and bool(value.strip())).all():
        raise ValueError("station_id deve conter identificadores de texto não vazios.")
    if exclude_station_id is not None and not isinstance(exclude_station_id, str):
        raise ValueError("exclude_station_id deve ser texto.")
    latitude = pd.to_numeric(frame["latitude"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    longitude = pd.to_numeric(frame["longitude"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    usable = frame["has_coordinates"].eq(True).fillna(False).to_numpy(dtype=bool)
    usable &= np.isfinite(latitude) & np.isfinite(longitude)
    usable &= (np.abs(latitude) <= 90) & (np.abs(longitude) <= 180)
    if exclude_station_id is not None:
        usable &= frame["station_id"].ne(exclude_station_id).to_numpy(dtype=bool)
    if limit == 0:
        usable[:] = False
    result = frame.loc[usable].copy()
    if result.empty:
        result["distance_km"] = pd.Series(index=result.index, dtype=float)
        return result
    result["distance_km"] = _distances(origin, latitude[usable], longitude[usable], method)
    result = result.loc[result["distance_km"] <= radius_km + _RADIUS_TOLERANCE_KM]
    return result.sort_values(["distance_km", "station_id"], kind="stable").head(int(limit)).copy()


def _loopback_endpoint(endpoint):
    if not isinstance(endpoint, str) or any(char.isspace() or ord(char) < 32 for char in endpoint):
        raise ValueError("Endpoint OSRM deve ser um endereço loopback local.")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
        host = parsed.hostname
        if (parsed.scheme not in ("http", "https") or not host or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("Endpoint OSRM deve ser um endereço loopback local.")
        # Pin localhost to a literal so DNS and proxy configuration cannot
        # redirect coordinates to an external host.
        if host.lower() == "localhost":
            host = "127.0.0.1"
        address = ip_address(host)
        if not address.is_loopback:
            raise ValueError("Endpoint OSRM deve ser um endereço loopback local.")
        netloc = f"[{address}]" if address.version == 6 else str(address)
        if port is not None:
            if port == 0:
                raise ValueError("Porta OSRM inválida.")
            netloc += f":{port}"
        return urlunsplit((parsed.scheme, netloc, "", "", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("Endpoint OSRM deve ser um endereço loopback local válido.") from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("OSRM local retornou redirecionamento; requisição recusada.")


def _route_transport(url, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=timeout) as response:
        return json.load(response)


def _parse_route(payload):
    try:
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        if not isinstance(payload, dict) or payload.get("code") != "Ok":
            raise ValueError("Serviço sem rota disponível.")
        route = payload["routes"][0]
        distance = _number(route["distance"], "Distância OSRM", minimum=0) / 1000
        duration = _number(route["duration"], "Duração OSRM", minimum=0) / 60
        waypoints = payload["waypoints"]
        if not isinstance(waypoints, list) or len(waypoints) != 2:
            raise ValueError("OSRM não retornou os dois pontos ajustados.")
        result = {"distance_km": distance, "duration_minutes": duration, "source": "osrm"}
        for name, waypoint in zip(("origin", "destination"), waypoints):
            lon, lat = waypoint["location"]
            result[f"{name}_snapped"] = _point((lat, lon))
            if "distance" in waypoint:
                result[f"{name}_snap_distance_km"] = _number(waypoint["distance"], "Ajuste OSRM", minimum=0) / 1000
        return result
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("OSRM não encontrou rota ou retornou resposta inválida.") from exc


def route_distance(origin, destination, *, endpoint="http://127.0.0.1:5000", timeout=10.0, transport=None) -> dict:
    """Read actual route kilometers/minutes from an explicitly local OSRM.

    The optional transport(url, timeout) returns decoded JSON, JSON text or
    bytes. The default transport disables proxies and all HTTP redirects.
    Snap pairs are (latitude, longitude); optional snap distances use km.
    """
    origin, destination = _point(origin), _point(destination)
    endpoint = _loopback_endpoint(endpoint)
    timeout = _number(timeout, "Timeout", minimum=0)
    if timeout == 0:
        raise ValueError("Timeout deve ser positivo.")
    coordinates = f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
    url = f"{endpoint}/route/v1/driving/{coordinates}?overview=false&steps=false&alternatives=false"
    try:
        payload = (transport or _route_transport)(url, timeout)
    except (OSError, urllib.error.URLError, HTTPException, RuntimeError, ValueError) as exc:
        raise RuntimeError("OSRM local indisponível ou resposta inválida; verifique o servidor configurado.") from exc
    return _parse_route(payload)
