"""Real road routes from an explicitly configured, loopback-only GraphHopper.

Input and snapped pairs use (latitude, longitude); GeoJSON uses (longitude,
latitude). Travel time comes from the routing model, without live traffic.
"""

from http.client import HTTPException
import json
import urllib.error
import urllib.request
from urllib.parse import urlencode

from .distances import _loopback_endpoint, _number, _point, distance_km

MAX_ROUTER_BYTES = 2_000_000
MAX_GEOMETRY_POINTS = 50_000


class RoadRoutingUnavailable(RuntimeError):
    """The configured router could not be reached safely."""


class RoadRouteError(RuntimeError):
    """No usable route was returned; geographic fallback is forbidden."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RoadRoutingUnavailable('Roteador local retornou redirecionamento; acesso recusado.')


def _transport(url, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(url, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code not in (400, 404, 422):
            raise
        response = exc
    with response:
        content = response.read(MAX_ROUTER_BYTES + 1)
    if len(content) > MAX_ROUTER_BYTES:
        raise RoadRouteError('Resposta do roteador excede o limite permitido.')
    return content


def _geojson(value, *, snapped=False):
    if not isinstance(value, dict) or value.get('type') != 'LineString':
        raise ValueError('Geometria inválida.')
    coordinates = value.get('coordinates')
    if (not isinstance(coordinates, list) or not 2 <= len(coordinates) <= MAX_GEOMETRY_POINTS
            or snapped and len(coordinates) != 2):
        raise ValueError('Quantidade de pontos inválida.')
    result = []
    for pair in coordinates:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError('Par de coordenadas inválido.')
        latitude, longitude = _point([pair[1], pair[0]])
        result.append([longitude, latitude])
    return {'type': 'LineString', 'coordinates': result}


def _parse(payload, origin, destination):
    try:
        if isinstance(payload, (str, bytes)):
            if len(payload.encode('utf-8') if isinstance(payload, str) else payload) > MAX_ROUTER_BYTES:
                raise ValueError('Resposta muito grande.')
            payload = json.loads(payload)
        if not isinstance(payload, dict) or not isinstance(payload.get('paths'), list):
            raise ValueError('Rota ausente.')
        path = payload['paths'][0]
        distance = _number(path['distance'], 'Distância viária', minimum=0) / 1000
        duration = _number(path['time'], 'Duração viária', minimum=0) / 60000
        geometry = _geojson(path['points'])
        snapped = _geojson(path['snapped_waypoints'], snapped=True)['coordinates']
        a, b = ([pair[1], pair[0]] for pair in snapped)
        return {
            'distance_km': distance, 'duration_minutes': duration,
            'source': 'graphhopper', 'profile': 'car', 'traffic': False,
            'origin': list(origin), 'destination': list(destination),
            'origin_snapped': a, 'destination_snapped': b,
            'origin_snap_distance_km': distance_km(origin, a),
            'destination_snap_distance_km': distance_km(destination, b),
            'geometry': geometry,
            'notice': 'Percurso viário GraphHopper; duração estimada pelo perfil de carro, sem trânsito em tempo real.',
        }
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise RoadRouteError('Roteador não encontrou percurso ou retornou uma resposta inválida.') from exc


def graphhopper_route(origin, destination, *, endpoint, timeout=10.0, transport=None):
    """Compute a real car route; never contact a host supplied by the request."""
    origin, destination = _point(origin), _point(destination)
    try:
        endpoint = _loopback_endpoint(endpoint)
    except ValueError as exc:
        raise ValueError('Endpoint de roteamento deve ser um endereço loopback válido.') from exc
    timeout = _number(timeout, 'Timeout', minimum=0)
    if not 0 < timeout <= 60:
        raise ValueError('Timeout deve estar entre zero exclusivo e 60 segundos.')
    query = urlencode([
        ('point', f'{origin[0]},{origin[1]}'),
        ('point', f'{destination[0]},{destination[1]}'),
        ('profile', 'car'), ('points_encoded', 'false'),
        ('instructions', 'false'), ('calc_points', 'true'),
    ])
    try:
        payload = (transport or _transport)(f'{endpoint}/route?{query}', timeout)
    except RoadRouteError:
        raise
    except (OSError, HTTPException, RuntimeError, ValueError) as exc:
        raise RoadRoutingUnavailable('Roteador local indisponível; verifique o serviço configurado.') from exc
    return _parse(payload, origin, destination)
