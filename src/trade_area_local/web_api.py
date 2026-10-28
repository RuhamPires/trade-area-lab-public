"""Stateless web adapter for the shared local engines and the public catalog."""

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import math
import os
from numbers import Real
from pathlib import Path
import re

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .contracts import ModelConfig, NOTICE, SimulationConfig
from .web_catalog import AVAILABLE_STATUSES, PUBLIC_FIELDS, WebCatalog, get_default_catalog, normalize
from .web_rules import EXPLORATORY_DEFAULTS, LIMITS, REFERENCE_DEFAULTS, method_description

MAX_RESPONSE_BYTES = 4_000_000
MAX_REQUEST_BYTES = 256_000
_RUN_FIELDS = {"catalog_version", "uf", "municipality", "station_ids", "mode", "base_date", "seed", "vehicles", "max_events", "days", "reference", "exploratory"}
_RESULT_FIELDS = ("trade_area", "mode", "assignment_reason", "has_transactions", "is_dispersed", "cr", "area_profile", "scenario_segment", "segment_source", "transaction_count", "liters", "authorized_amount", "eligible_vehicle_count")
_MOVE_FIELDS = ("pass", "station_id", "from_area", "to_area", "cr_before", "cr_after", "stage")
_METRIC_FIELDS = {"stations_total", "stations_with_transactions", "stations_without_transactions", "trade_areas", "isolated_areas", "dispersed_stations", "fallback_partitions", "moves_total", "moves_per_pass", "mean_cr", "area_sizes", "mean_distance_to_center_km", "max_distance_to_center_km", "partition_modes", "reference"}
_REFERENCE_METRIC_FIELDS = {"period", "config", "eligibility", "iterations", "outliers", "corrections", "low_volume", "profiles", "allocation", "initial_areas", "kernel_station_ids", "candidate_policy", "no_kernel"}


class WebRequestError(ValueError):
    def __init__(self, detail, status_code=422):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _fail(detail, status_code=422):
    raise WebRequestError(detail, status_code)


def _object(value, name, allowed):
    if not isinstance(value, dict):
        _fail(f"{name} deve ser um objeto JSON.")
    unknown = set(value) - set(allowed)
    if unknown:
        _fail(f"Campos desconhecidos em {name}: {', '.join(sorted(str(item) for item in unknown))}.")
    return value


def _finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        _fail("JSON não permite NaN ou Infinity; informe números finitos.")
    if isinstance(value, dict):
        for item in value.values():
            _finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            _finite_tree(item)


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail(f"{name} deve ser inteiro entre {low} e {high}.")
    return value


def _number(value, name, low=0, high=20000, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, Real):
        _fail(f"{name} deve ser um número finito.")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        _fail(f"{name} está fora do intervalo permitido.")
    if not math.isfinite(number) or not low <= number <= high or positive and number <= 0:
        _fail(f"{name} deve estar entre {low} e {high}" + (", maior que zero." if positive else "."))
    return number


def _text(value, name, *, optional=False, max_length=200):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        _fail(f"{name} deve ser texto preenchido com até {max_length} caracteres.")
    return value.strip()


def _configuration(raw, defaults, name):
    raw = _object(raw, name, defaults)
    config = {**defaults, **raw}
    integer_fields = {"max_passes", "urban_ratio", "road_ratio", "candidates_road", "candidates_urban", "candidates_combined", "target_size"}
    for key, value in config.items():
        if key in integer_fields:
            minimum = 2 if key == "target_size" else 1
            _integer(value, key, minimum, 8 if key == "max_passes" else 1000)
        elif key in {"exclude_full_overlap", "cross_uf"}:
            if type(value) is not bool:
                _fail(f"{key} deve ser booleano verdadeiro ou falso.")
        elif key == "ranking":
            if not isinstance(value, str) or value not in {"cc", "cc_per_km"}:
                _fail("ranking deve ser cc ou cc_per_km.")
        elif "percentile" in key or key == "profile_urban_threshold":
            _number(value, key, 0, 1)
        else:
            _number(value, key, positive=True)
    return config


def _validated_run(payload, catalog):
    _object(payload, "requisição", _RUN_FIELDS)
    _finite_tree(payload)
    version = _text(payload.get("catalog_version"), "catalog_version")
    if version != catalog.version:
        _fail("A versão do catálogo mudou. Atualize o cadastro e recalcule o cenário.", 409)
    mode = payload.get("mode", "reference")
    if not isinstance(mode, str) or mode not in {"reference", "exploratory"}:
        _fail("Modo deve ser reference ou exploratory.")
    base = _text(payload.get("base_date"), "base_date", max_length=10)
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", base):
            raise ValueError
        base_date = date.fromisoformat(base)
    except ValueError:
        _fail("Data-base deve ser uma data válida no formato AAAA-MM-DD.")
    if not date(2000, 5, 1) <= base_date <= date(2100, 12, 31):
        _fail("Data-base deve estar entre 2000-05-01 e 2100-12-31.")
    reference = _configuration(payload.get("reference", {}), REFERENCE_DEFAULTS, "reference")
    exploratory = _configuration(payload.get("exploratory", {}), EXPLORATORY_DEFAULTS, "exploratory")
    if mode == "reference" and exploratory != EXPLORATORY_DEFAULTS:
        _fail("Parâmetros exploratórios alterados exigem modo exploratory.")
    if mode == "exploratory" and reference != REFERENCE_DEFAULTS:
        _fail("Parâmetros de referência alterados exigem modo reference.")
    parameters = {
        "catalog_version": version, "mode": mode, "base_date": base_date.isoformat(),
        "seed": _integer(payload.get("seed", 42), "Semente", 0, 2**32 - 1),
        "vehicles": _integer(payload.get("vehicles", 1200), "Veículos", 0, LIMITS["vehicles"]),
        "max_events": _integer(payload.get("max_events", 30), "Eventos por veículo", 1, LIMITS["max_events"]),
        "days": _integer(payload.get("days", 90), "Janela em dias", 1, 365),
        "reference": reference, "exploratory": exploratory,
    }
    if parameters["vehicles"] * parameters["max_events"] > LIMITS["total_events"]:
        _fail("O cenário excede 60.000 eventos; reduza veículos ou eventos por veículo.")
    uf = _text(payload.get("uf"), "UF", optional=True, max_length=2)
    municipality = _text(payload.get("municipality"), "Município", optional=True)
    if uf is not None and not re.fullmatch("[A-Za-z]{2}", uf):
        _fail("UF deve conter duas letras.")
    ids = payload.get("station_ids")
    if ids is not None:
        if not isinstance(ids, list) or not ids:
            _fail("station_ids deve ser uma lista não vazia de códigos em texto.")
        ids = [_text(item, "station_id", max_length=100) for item in ids]
        if len(set(ids)) != len(ids):
            _fail("station_ids não pode conter códigos repetidos.")
        selected = []
        for sid in sorted(ids):
            row = catalog.station(sid)
            if row is None:
                _fail(f"Código de posto desconhecido: {sid}.")
            if uf and normalize(row["uf"]) != normalize(uf) or municipality and normalize(row["municipality"]) != normalize(municipality):
                _fail("A seleção contém posto fora da restrição de UF/município.")
            selected.append(row)
    else:
        if not uf and not municipality:
            _fail("Selecione códigos de postos ou recorte por UF/município antes de calcular.")
        selected = [row for row in catalog.rows() if (not uf or normalize(row["uf"]) == normalize(uf)) and (not municipality or normalize(row["municipality"]) == normalize(municipality))]
    usable = [row for row in selected if row["coordinate_status"] in AVAILABLE_STATUSES]
    if len(usable) > LIMITS["stations"]:
        _fail("O recorte excede 1.000 postos utilizáveis. Reduza a seleção; nenhum posto foi truncado.")
    if not usable:
        _fail("O recorte não contém postos com coordenadas utilizáveis. Escolha outro recorte.")
    parameters.update(uf=uf.upper() if uf else None, municipality=municipality.upper() if municipality else None, station_ids=sorted(row["station_id"] for row in selected))
    return parameters, base_date, usable, [{"station_id": row["station_id"], "reason": "coordinate_review_required" if row["coordinate_status"] == "review_required" else "coordinates_missing"} for row in selected if row["coordinate_status"] not in AVAILABLE_STATUSES]


def _compute_model(parameters, base_date, selected):
    """Compute shared engines without persisting files or exposing raw inputs."""
    import numpy as np
    import pandas as pd
    from .model import run_model
    from .simulation import simulate_events

    frame = pd.DataFrame(selected)
    frame["source"] = frame["coordinate_source"].fillna("public_catalog")
    frame["coordinate_quality"] = frame["coordinate_status"]
    simulation = SimulationConfig(seed=parameters["seed"], vehicles=parameters["vehicles"], max_events_per_vehicle=parameters["max_events"], days=parameters["days"], end_date=base_date - timedelta(days=1))
    if parameters["mode"] == "reference":
        from .reference_contracts import ReferenceConfig
        from .reference_model import run_reference_model
        from .reference_simulation import reference_period, simulate_reference_inputs
        config = ReferenceConfig(**parameters["reference"])
        prepared, events = simulate_reference_inputs(frame, simulation, base_date)
        result = run_reference_model(prepared, events, config, base_date=base_date)
        period = reference_period(base_date)
        eligibility = result.metrics.get("reference", {}).get("eligibility", {})
    else:
        # Operational segment is scenario data, independent of addresses/brand/coordinates.
        frame["public_segment"] = frame["segment"]
        rng = np.random.default_rng(parameters["seed"])
        frame["scenario_segment"] = rng.choice(["urbano", "rodoviario"], size=len(frame))
        frame["segment"] = frame["scenario_segment"]
        frame["segment_source"] = "synthetic"
        events = simulate_events(frame, simulation)
        result = run_model(frame, events, ModelConfig(**parameters["exploratory"]))
        aggregate = events.groupby("station_id").agg(transaction_count=("event_id", "size"), liters=("liters", "sum"), authorized_amount=("amount", "sum"), eligible_vehicle_count=("vehicle_id", "nunique"))
        for column in aggregate:
            result.stations[column] = result.stations.station_id.map(aggregate[column]).fillna(0)
        urban_share = result.stations.groupby("trade_area").scenario_segment.apply(lambda group: float(group.eq("urbano").mean()))
        profiles = urban_share.map(lambda share: "Urbana" if share > .8 else "Rodoviária" if share == 0 else "Mista")
        result.stations["area_profile"] = result.stations.trade_area.map(profiles)
        period = (simulation.end_date - timedelta(days=simulation.days - 1), simulation.end_date)
        eligibility = {"applicable": False, "reason": "exploratory_synthetic_events", "total": len(events), "accepted": len(events), "rejected": 0, "unknown": 0}
    return result, period, eligibility


def _json_safe(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        return _json_safe(value.item())
    # pandas missing scalars are unavailable, never textual 'nan' or '<NA>'.
    return None


@lru_cache(maxsize=1)
def _code_digest():
    digest = hashlib.sha256()
    for name in ("web_api.py", "web_catalog.py", "web_rules.py", "contracts.py", "model.py", "geometry.py", "simulation.py", "reference_contracts.py", "reference_model.py", "reference_simulation.py", "eligibility.py", "road_routing.py"):
        path = Path(__file__).with_name(name)
        if path.exists():
            digest.update(name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _check_size(result):
    serialized = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(serialized) >= MAX_RESPONSE_BYTES:
        _fail("O resultado excede 4 MB. Reduza o recorte ou as iterações e recalcule.", 413)
    return result


def _public_eligibility(audit):
    fields = {"total", "accepted", "rejected", "unknown", "excluded", "reasons", "per_rule", "period", "notice", "applicable", "reason", "simulation"}
    result = {key: value for key, value in audit.items() if key in fields}
    if "per_rule" in result:
        result["per_rule"] = {rule: {status: value for status, value in counts.items() if status in {"accepted", "rejected", "unknown"}} for rule, counts in result["per_rule"].items()}
    if "simulation" in result:
        fields = {"source", "generator", "planned_proportions", "cycle_length", "calibration", "notice"}
        result["simulation"] = {key: value for key, value in result["simulation"].items() if key in fields}
    return result


def execute_web_run(payload: dict, catalog: WebCatalog) -> dict:
    """Pure request adapter: fixed inputs produce identical IDs and model outputs."""
    parameters, base_date, selected, excluded = _validated_run(payload, catalog)
    try:
        result, period, eligibility = _compute_model(parameters, base_date, selected)
    except WebRequestError:
        raise
    except Exception as exc:
        raise WebRequestError("Não foi possível concluir o cálculo. Revise o recorte e tente novamente.", 500) from exc
    expected = {row["station_id"] for row in selected}
    if len(result.stations) != len(selected) or set(result.stations.station_id) != expected or not result.stations.station_id.is_unique:
        _fail("O cálculo não conservou todos os postos selecionados. Nenhum resultado foi publicado.", 500)
    station_map = {row["station_id"]: row for row in selected}
    stations = []
    for row in result.stations.sort_values("station_id", kind="stable").to_dict(orient="records"):
        if not row.get("trade_area") or not row.get("assignment_reason"):
            _fail("O cálculo retornou associação incompleta. Recalcule o cenário.", 500)
        stations.append({**station_map[row["station_id"]], **{field: row.get(field) for field in _RESULT_FIELDS}})
    columns = [field for field in _MOVE_FIELDS if field in result.moves.columns]
    moves = {"columns": columns, "rows": result.moves[columns].values.tolist()}
    metrics = {field: result.metrics[field] for field in _METRIC_FIELDS if field in result.metrics}
    if "reference" in metrics:
        metrics["reference"] = {key: item for key, item in metrics["reference"].items() if key in _REFERENCE_METRIC_FIELDS}
        if "eligibility" in metrics["reference"]:
            metrics["reference"]["eligibility"] = _public_eligibility(metrics["reference"]["eligibility"])
    eligibility = _public_eligibility(eligibility)
    code_digest = _code_digest()
    canonical = json.dumps({"application_version": __version__, "catalog_sha256": catalog.sha256, "code_sha256": code_digest, "parameters": parameters}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    customized = parameters[parameters["mode"]] != (REFERENCE_DEFAULTS if parameters["mode"] == "reference" else EXPLORATORY_DEFAULTS)
    mode_label = "Referência customizada" if parameters["mode"] == "reference" and customized else "Referência documentada" if parameters["mode"] == "reference" else "Exploratório"
    response = {
        "run_id": "web-" + fingerprint[:24], "request_fingerprint": fingerprint,
        "catalog_version": catalog.version, "mode": parameters["mode"], "is_customized": customized,
        "notice": NOTICE + " " + mode_label + ". Segmentos operacionais e elegibilidade também são simulados.",
        "period": {"start": period[0].isoformat(), "end": period[1].isoformat()},
        "parameters": parameters, "metrics": metrics, "stations": stations, "moves": moves,
        "excluded_stations": excluded, "eligibility": eligibility,
        "manifest": {"schema_version": "1", "application_version": __version__, "code_sha256": code_digest, "catalog": catalog.provenance(), "mode_label": mode_label, "is_synthetic": True, "segment_source": "synthetic", "generated_at": datetime.now(timezone.utc).isoformat(), "accepted_stations": len(stations), "excluded_stations": len(excluded), "request_fingerprint": fingerprint, "notice": NOTICE},
    }
    return _check_size(_json_safe(response))


def _point(payload, name, catalog):
    identifier, coordinates = payload.get(name + "_id"), payload.get(name)
    if (identifier is None) == (coordinates is None):
        _fail(f"Informe {name}_id ou {name}, exclusivamente.")
    if identifier is not None:
        identifier = _text(identifier, name + "_id", max_length=100)
        row = catalog.station(identifier)
        if row is None:
            _fail("Código de posto desconhecido.")
        if row["coordinate_status"] not in AVAILABLE_STATUSES:
            _fail("O posto não possui coordenadas utilizáveis.")
        return [row["latitude"], row["longitude"]]
    if not isinstance(coordinates, list) or len(coordinates) != 2:
        _fail(f"{name} deve conter [latitude, longitude].")
    return [_number(coordinates[0], "Latitude", -90, 90), _number(coordinates[1], "Longitude", -180, 180)]


def _distance(payload, catalog):
    from .distances import distance_km
    _object(payload, "distância", {"origin_id", "destination_id", "origin", "destination"})
    _finite_tree(payload)
    origin, destination = _point(payload, "origin", catalog), _point(payload, "destination", catalog)
    return {"geodesic_km": distance_km(origin, destination, method="geodesic"), "haversine_km": distance_km(origin, destination, method="haversine"), "origin": origin, "destination": destination}


def _road_route(payload, catalog):
    from .road_routing import RoadRouteError, RoadRoutingUnavailable, graphhopper_route
    _object(payload, 'percurso', {'origin_id', 'destination_id', 'origin', 'destination'})
    _finite_tree(payload)
    origin, destination = _point(payload, 'origin', catalog), _point(payload, 'destination', catalog)
    endpoint = os.environ.get('TRADE_AREA_ROUTER_URL')
    if not endpoint:
        _fail('Roteamento por estrada não configurado neste ambiente. Use a instalação local com um roteador ativo.', 503)
    try:
        return _check_size(graphhopper_route(origin, destination, endpoint=endpoint))
    except RoadRoutingUnavailable:
        _fail('Roteador local indisponível. Verifique se o serviço de percurso está ativo.', 503)
    except RoadRouteError:
        _fail('Não foi possível obter um percurso válido para os pontos informados.', 502)
    except ValueError:
        _fail('Configuração do roteador local inválida. Verifique o serviço de percurso.', 503)


def _nearby(payload, catalog):
    import pandas as pd
    from .distances import nearby_stations
    _object(payload, "busca por raio", {"origin_id", "origin", "radius_km", "limit"})
    _finite_tree(payload)
    origin = _point(payload, "origin", catalog)
    radius = _number(payload.get("radius_km"), "Raio", 0, 20040)
    limit = _integer(payload.get("limit", 100), "Limite", 1, 1000)
    frame = pd.DataFrame(catalog.rows(available_only=True), columns=PUBLIC_FIELDS)
    frame["has_coordinates"] = True
    matches = nearby_stations(frame, origin, radius_km=radius, limit=max(1, len(frame)), exclude_station_id=payload.get("origin_id"))
    rows = matches.head(limit)[list(PUBLIC_FIELDS) + ["distance_km"]].to_dict(orient="records")
    return _json_safe({"total": len(matches), "returned": len(rows), "stations": rows})


async def _body(request):
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        _fail("Requisição muito grande. Envie apenas recorte, códigos e parâmetros.", 413)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        _fail("Corpo da requisição deve ser JSON válido.")
    return payload


def create_app(catalog: WebCatalog | None = None):
    app = FastAPI(title="Trade Area público", version=__version__, docs_url=None, redoc_url=None)

    def current_catalog():
        return catalog if catalog is not None else get_default_catalog()

    @app.exception_handler(WebRequestError)
    async def request_error(request, exc):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "Parâmetros inválidos. Confira os campos e os limites informados."}, status_code=422)

    @app.exception_handler(Exception)
    async def backend_error(request, exc):
        return JSONResponse({"detail": "Serviço temporariamente indisponível. Tente novamente."}, status_code=500)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "catalog_version": current_catalog().version}

    @app.get("/api/catalog/summary")
    def summary():
        return current_catalog().summary()

    @app.get("/api/stations")
    def stations(uf: str | None = None, municipality: str | None = None, q: str | None = None, status: str = "all", offset: int = 0, limit: int = 200):
        if any(value is not None and len(value) > 200 for value in (uf, municipality, q)):
            _fail("Filtros de busca devem ter até 200 caracteres.")
        try:
            return current_catalog().search(uf=uf, municipality=municipality, q=q, status=status, offset=offset, limit=limit)
        except ValueError as exc:
            _fail(str(exc))

    @app.get("/api/method")
    def method():
        return method_description()

    @app.post("/api/run")
    async def run(request: Request):
        payload = await _body(request)
        return await run_in_threadpool(execute_web_run, payload, current_catalog())

    @app.post("/api/distance")
    async def distance(request: Request):
        return await run_in_threadpool(_distance, await _body(request), current_catalog())

    @app.post("/api/nearby")
    async def nearby(request: Request):
        return await run_in_threadpool(_nearby, await _body(request), current_catalog())

    @app.post('/api/route')
    async def road_route(request: Request):
        return await run_in_threadpool(_road_route, await _body(request), current_catalog())

    return app
