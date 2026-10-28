"""Own regional Valhalla Actor. Graph creation is exclusively a build step.

No native dependency is imported until a configured route is requested.
Public request coordinates are lat/lon; OSRM GeoJSON and waypoints are lon/lat.
"""
from functools import lru_cache
import json
import os
from pathlib import Path
from threading import RLock

from .distances import _number, _point, distance_km
from .road_routing import MAX_ROUTER_BYTES, RoadRouteError, RoadRoutingUnavailable, _geojson

ENGINE_VERSION = '3.9.1'
BOUNDS = [-47.35, -23.4, -46.7, -22.65]
COVERAGE = {'name': 'Campinas–Jundiaí', 'bounds': BOUNDS, 'source_timestamp': '2026-10-26T21:24:46Z',
            'attribution': '© OpenStreetMap contributors', 'license': 'ODbL-1.0',
            'copyright_url': 'https://www.openstreetmap.org/copyright'}
_ACTOR_LOCK = RLock()


class RegionalRouteError(RoadRouteError):
    """Valid coordinates outside the public region or its routable roads."""
    status_code = 422


def configured_valhalla(app_root=None):
    explicit = os.environ.get('TRADE_AREA_VALHALLA_CONFIG')
    if explicit:
        return Path(explicit)
    root = Path(app_root) if app_root is not None else Path(__file__).resolve().parents[2]
    config = root / '.routing/runtime.json'
    return config if config.is_file() else None


@lru_cache(maxsize=1)
def _actor(config_path):
    # Called only under _ACTOR_LOCK: initialization and execution share ownership.
    import valhalla
    if valhalla.__version__ != ENGINE_VERSION:
        raise RuntimeError('Versão do motor incompatível.')
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    # Enforce the hosted budget even for a server-selected config.
    config['mjolnir'].update(max_cache_size=32 * 1024 * 1024, use_lru_mem_cache=True,
                             lru_mem_cache_hard_control=True, concurrency=1)
    config['thor'].update(max_reserved_labels_count_astar=16384, max_reserved_labels_count_bidir_astar=16384,
                          max_reserved_labels_count_dijkstras=16384, max_reserved_labels_count_bidir_dijkstras=16384,
                          clear_reserved_memory=True)
    return valhalla.Actor(config)


def _inside_region(point):
    west, south, east, north = BOUNDS
    return south <= point[0] <= north and west <= point[1] <= east


def _parse(payload, origin, destination):
    try:
        if isinstance(payload, (str, bytes)):
            encoded = payload.encode('utf-8') if isinstance(payload, str) else payload
            if len(encoded) > MAX_ROUTER_BYTES:
                raise ValueError('Resposta muito grande.')
            payload = json.loads(encoded)
        elif len(json.dumps(payload, allow_nan=False, separators=(',', ':')).encode('utf-8')) > MAX_ROUTER_BYTES:
            raise ValueError('Resposta muito grande.')
        if payload.get('code') != 'Ok':
            raise ValueError('Rota ausente.')
        route = payload['routes'][0]
        distance = _number(route['distance'], 'Distância', minimum=0) / 1000
        duration = _number(route['duration'], 'Duração', minimum=0) / 60
        geometry = _geojson(route['geometry'])
        waypoints = payload['waypoints']
        if not isinstance(waypoints, list) or len(waypoints) != 2:
            raise ValueError('Ajustes ausentes.')
        snaps, snap_distances = [], []
        for waypoint, original, edge in zip(waypoints, (origin, destination),
                                            (geometry['coordinates'][0], geometry['coordinates'][-1])):
            location = waypoint['location']
            if not isinstance(location, list) or len(location) != 2:
                raise ValueError('Ajuste inválido.')
            snapped = list(_point([location[1], location[0]]))
            snap_distance = _number(waypoint['distance'], 'Ajuste', minimum=0) / 1000
            actual_snap = distance_km(original, snapped)
            if (snap_distance > 1 or actual_snap > 1.01 or abs(actual_snap - snap_distance) > .05
                    or distance_km(snapped, [edge[1], edge[0]]) > .1):
                raise ValueError('Ajuste incoerente.')
            snaps.append(snapped)
            snap_distances.append(snap_distance)
        # Engine distance cannot undercut the shortest path between snapped points.
        straight = distance_km(*snaps)
        if distance + .05 < straight * .98 or (straight > .01 and duration <= 0):
            raise ValueError('Distância incoerente.')
        return {'distance_km': distance, 'duration_minutes': duration,
                'source': 'valhalla', 'profile': 'car', 'traffic': False,
                'origin': list(origin), 'destination': list(destination),
                'origin_snapped': snaps[0], 'destination_snapped': snaps[1],
                'origin_snap_distance_km': snap_distances[0], 'destination_snap_distance_km': snap_distances[1],
                'geometry': geometry, 'engine_version': ENGINE_VERSION, 'version': ENGINE_VERSION,
                'coverage': COVERAGE['name'], 'coverage_bounds': list(BOUNDS),
                'map_date': COVERAGE['source_timestamp'], 'attribution': COVERAGE['attribution'],
                'notice': 'Percurso viário Valhalla, cobertura Campinas–Jundiaí; duração estimada para carro, sem trânsito em tempo real.'}
    except (KeyError, IndexError, TypeError, AttributeError, ValueError, OverflowError, UnicodeError) as exc:
        raise RoadRouteError('O motor retornou um percurso inválido; tente outros pontos próximos às vias.') from exc


def valhalla_route(origin, destination, *, config_path, actor=None):
    origin, destination = _point(origin), _point(destination)
    if not all(_inside_region(point) for point in (origin, destination)):
        raise RegionalRouteError('Os dois pontos devem estar na cobertura Campinas–Jundiaí do mapa público.')
    request = {'locations': [{'lat': point[0], 'lon': point[1], 'search_cutoff': 1000} for point in (origin, destination)],
               'costing': 'auto', 'format': 'osrm', 'shape_format': 'geojson',
               'language': 'pt-BR', 'directions_type': 'none'}
    try:
        with _ACTOR_LOCK:
            result = (actor if actor is not None else _actor(str(Path(config_path).resolve()))).route(request)
    except Exception as exc:
        if getattr(exc, 'code', None) == 171 or getattr(exc, 'http_code', None) == 400:
            raise RegionalRouteError('Não foi possível ligar estes pontos às vias do recorte Campinas–Jundiaí. Escolha pontos próximos às vias cobertas.') from exc
        raise RoadRoutingUnavailable('Motor regional temporariamente indisponível; tente novamente.') from exc
    return _parse(result, origin, destination)
