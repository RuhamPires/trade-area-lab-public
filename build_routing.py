"""Build the allowlisted public OSM region natively on Linux, then smoke-test it.

Run during hosting build: pip install -r requirements.txt && python build_routing.py
No network fetch, input selection or graph construction happens in an API request.
Any build/smoke failure exits nonzero before the new graph is promoted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

PBF_SOURCE = 'data/routing-pilot/2026-10-28/regional-map-20261028T042454Z-382199/campinas-jundiai-roads.osm.pbf'
PBF_SHA256 = '92c9fbd8709381d3d9ff0d0169d79104827ab14841c99737115a7c4eea3c0c10'
PBF_BYTES = 11612428
PUBLIC_MANIFEST = {
    'schema_version': 1, 'package': 'pyvalhalla', 'version': '3.9.1',
    'pbf_file': 'map.osm.pbf', 'pbf_bytes': PBF_BYTES, 'pbf_sha256': PBF_SHA256,
    'coverage': 'Campinas–Jundiaí', 'bounds': [-47.35, -23.4, -46.7, -22.65],
    'source_timestamp': '2026-10-26T21:24:46Z', 'license': 'ODbL-1.0',
    'attribution': '© OpenStreetMap contributors',
    'copyright_url': 'https://www.openstreetmap.org/copyright',
    'source': 'Public OpenStreetMap regional roads extract; no proprietary data',
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def validate_source(root):
    source = Path(root) / 'routing-public'
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    if manifest != PUBLIC_MANIFEST:
        raise ValueError('Manifesto público de roteamento inválido ou não permitido.')
    pbf = source / 'map.osm.pbf'
    if pbf.is_symlink() or pbf.stat().st_size != PBF_BYTES or sha256(pbf) != PBF_SHA256:
        raise ValueError('PBF público diferente do recorte regional permitido.')
    return pbf


def make_config(valhalla, directory):
    directory = Path(directory).resolve()
    config = valhalla.get_config(tile_dir=directory / 'tiles', tile_extract='')
    config['logging'].update(type='', color=False)
    config['mjolnir'].update(concurrency=1, max_cache_size=32 * 1024 * 1024,
                             id_table_size=10_000_000, use_lru_mem_cache=True,
                             lru_mem_cache_hard_control=True, admin='', timezone='',
                             landmarks='', traffic_extract='', transit_dir=str(directory / 'transit'),
                             transit_feeds_dir=str(directory / 'transit-feeds'),
                             include_bicycle=False, include_pedestrian=False)
    config['mjolnir']['data_processing'].update(use_admin_db=False, apply_country_overrides=False)
    config['thor'].update(max_reserved_labels_count_astar=16384, max_reserved_labels_count_bidir_astar=16384,
                          max_reserved_labels_count_dijkstras=16384, max_reserved_labels_count_bidir_dijkstras=16384,
                          clear_reserved_memory=True)
    config['loki']['service_defaults']['search_cutoff'] = 1000
    return config


def process_peak_bytes(pid):
    try:
        fields = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
        return int(fields['VmHWM'].split()[0]) * 1024
    except (OSError, ValueError, KeyError):
        return 0


def run_limited(command, directory, *, name, timeout, memory_bytes):
    """Fail the hosting build, preserving logs/receipts, before promotion."""
    started, peak, stop_reason = time.monotonic(), 0, None
    env = {**os.environ, 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'}
    with (directory / f'{name}.log').open('xb') as log:
        process = subprocess.Popen(command, cwd=directory, stdout=log, stderr=subprocess.STDOUT, env=env)
        while process.poll() is None:
            peak = max(peak, process_peak_bytes(process.pid))
            if time.monotonic() - started > timeout:
                stop_reason = f'timeout_{timeout}s'
            elif peak > memory_bytes:
                stop_reason = 'memory_limit'
            if stop_reason:
                process.kill()
                process.wait(timeout=10)
                break
            time.sleep(.05)
    if sys.platform == 'linux' and name == 'build':
        import resource
        peak = max(peak, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024)
    if name == 'smoke' and (directory / 'smoke-result.json').is_file():
        smoke_result = json.loads((directory / 'smoke-result.json').read_text(encoding='utf-8'))
        peak = max(peak, smoke_result['peak_rss_bytes'])
        print(json.dumps({'smoke_result': smoke_result}), flush=True)
    receipt = {'platform': sys.platform, 'version': PUBLIC_MANIFEST['version'],
               'completed_at_utc': datetime.now(timezone.utc).isoformat(),
               'seconds': time.monotonic() - started, 'exit_code': process.returncode,
               'stop_reason': stop_reason, 'peak_rss_bytes': peak,
               'input_pbf_sha256': PBF_SHA256}
    (directory / f'{name}-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({name: receipt}), flush=True)
    if process.returncode != 0 or stop_reason:
        raise RuntimeError(f'{name} falhou; consulte {name}.log e {name}-receipt.json. Implantação anterior preservada.')
    return receipt


def smoke(root, config_path):
    """One Linux process keeps Actor, catalog and real model imports resident."""
    import resource
    sys.path.insert(0, str(root / 'src'))
    import valhalla
    from trade_area_local.valhalla_routing import RegionalRouteError, _actor, valhalla_route
    from trade_area_local.web_api import WebRequestError, _road_route, create_app, execute_web_run
    from trade_area_local.web_catalog import get_default_catalog

    if valhalla.__version__ != '3.9.1':
        raise RuntimeError('Versão nativa incompatível.')
    origin, destination = [-22.9633, -47.13205667], [-22.9002136, -47.0706199]
    os.environ['TRADE_AREA_VALHALLA_CONFIG'] = str(config_path)
    os.environ.pop('TRADE_AREA_ROUTER_URL', None)
    started = time.monotonic()
    catalog = get_default_catalog()
    create_app(catalog)
    route = _road_route({'origin': origin, 'destination': destination}, catalog)
    assert 10 < route['distance_km'] < 15 and route['duration_minutes'] > 0
    assert len(route['geometry']['coordinates']) > 2
    try:
        valhalla_route([0, 0], destination, config_path=config_path)
        raise AssertionError('Rota fora do recorte foi aceita.')
    except RegionalRouteError:
        pass
    try:
        _road_route({'origin': origin, 'destination': destination, 'profile': 'truck'}, catalog)
        raise AssertionError('Configuração de cliente aceita.')
    except WebRequestError as exc:
        assert exc.status_code == 422
    # Exercise the real native error in addition to the adapter coverage guard.
    try:
        _actor(str(config_path.resolve())).route({'locations': [{'lat': 51.5, 'lon': -.1}, {'lat': 51.51, 'lon': -.11}], 'costing': 'auto'})
        raise AssertionError('Motor roteou fora do recorte.')
    except valhalla.ValhallaError as exc:
        assert exc.code == 171 and exc.http_code == 400
    after_route = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    selected = catalog.rows(available_only=True)[:1000]
    # Full supported station count, public coordinates and explicitly synthetic events.
    result = execute_web_run({'catalog_version': catalog.version,
                              'station_ids': [row['station_id'] for row in selected],
                              'mode': 'reference', 'base_date': '2026-10-27',
                              'seed': 42, 'vehicles': 2000, 'max_events': 30, 'days': 90}, catalog)
    assert len(result['stations']) == len(selected)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    receipt = {'platform': sys.platform, 'version': valhalla.__version__,
               'distance_km': route['distance_km'], 'duration_minutes': route['duration_minutes'],
               'geometry_coordinates': len(route['geometry']['coordinates']),
               'catalog_version': catalog.version, 'model_stations': len(selected),
               'mode': 'reference', 'synthetic_vehicles': 2000, 'synthetic_max_events': 30,
               'after_route_peak_rss_bytes': after_route, 'peak_rss_bytes': peak,
               'seconds': time.monotonic() - started, 'network_route_calls': 0,
               'outside_native_code': 171, 'client_profile_rejected': True}
    (config_path.parent / 'smoke-result.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt), flush=True)
    if peak >= 450 * 1024**2 or receipt['seconds'] > 60:
        raise RuntimeError('Smoke excedeu o orçamento de memória/tempo; não promover.')


def build(root):
    if sys.platform != 'linux':
        raise RuntimeError('O pacote hospedado exige construção nativa Linux; tiles Windows não são reutilizados.')
    root = Path(root).resolve()
    pbf = validate_source(root)
    import valhalla
    if valhalla.__version__ != '3.9.1':
        raise RuntimeError('Instale pyvalhalla==3.9.1 antes do build.')
    final = root / '.routing'
    if final.exists():
        raise RuntimeError('Diretório .routing já existe; não sobrescrever um grafo ativo.')
    output = root / ('.routing-build-' + uuid.uuid4().hex[:12])
    output.mkdir()
    (output / 'tiles').mkdir()
    config_file = output / 'runtime.json'
    config_file.write_text(json.dumps(make_config(valhalla, output), indent=2) + '\n', encoding='utf-8')
    executable = Path(valhalla.__file__).resolve().parent / 'bin/valhalla_build_tiles'
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError('Executável nativo do wheel oficial ausente.')
    run_limited([str(executable), '-c', str(config_file), '-j', '1', str(pbf)], output,
                name='build', timeout=300, memory_bytes=2 * 1024**3)
    tiles = sorted((output / 'tiles').rglob('*.gph'))
    if not tiles:
        raise RuntimeError('Build não produziu tiles; não promover.')
    run_limited([sys.executable, str(Path(__file__).resolve()), '--root', str(root), '--smoke', str(config_file)],
                output, name='smoke', timeout=60, memory_bytes=450 * 1024**2)
    # Only the Linux-native graph that passed smoke becomes discoverable by the API.
    # get_config resolves tile_dir strictly; final does not exist before rename.
    # Preserve the proven config and relocate its three graph-relative directories.
    config = json.loads(config_file.read_text(encoding='utf-8'))
    config['mjolnir'].update(tile_dir=str(final / 'tiles'), transit_dir=str(final / 'transit'),
                             transit_feeds_dir=str(final / 'transit-feeds'))
    config_file.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    (output / 'manifest.json').write_text(json.dumps({**PUBLIC_MANIFEST, 'tile_files': len(tiles),
                                                    'tile_bytes': sum(path.stat().st_size for path in tiles)}, indent=2) + '\n', encoding='utf-8')
    output.rename(final)
    print(json.dumps({'routing_ready': True, 'tile_files': len(tiles), 'runtime': '.routing/runtime.json'}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    script = Path(__file__).resolve()
    parser.add_argument('--root', type=Path, default=script.parents[1] if script.parent.name == 'scripts' else script.parent)
    parser.add_argument('--smoke', type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        smoke(args.root.resolve(), args.smoke.resolve()) if args.smoke else build(args.root)
    except Exception as exc:
        print(f'Routing build refused: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(1)
