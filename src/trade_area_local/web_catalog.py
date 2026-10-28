"""Read-only public catalog projection; no source files or network are queried."""

from collections import Counter
from copy import deepcopy
from functools import lru_cache
import gzip
import hashlib
import json
import math
from numbers import Real
from pathlib import Path
import re
from types import MappingProxyType
import unicodedata

PUBLIC_FIELDS = (
    "station_id", "cnpj", "name", "municipality", "uf", "address", "neighborhood",
    "postal_code", "latitude", "longitude", "coordinate_status", "coordinate_source",
    "coordinate_date", "coordinate_precision", "segment",
)
AVAILABLE_STATUSES = frozenset({"available_unverified", "available_verified"})
_STATUSES = AVAILABLE_STATUSES | {"missing", "review_required"}
CATALOG_NOTICE = "Cadastro público ANP e complementos públicos. Posições podem não estar verificadas; não comprovam funcionamento atual."


def normalize(value):
    return "".join(char for char in unicodedata.normalize("NFKD", str(value)) if not unicodedata.combining(char)).casefold().strip()


def _identifier_digits(value, length):
    """Recognize a complete CNPJ/CEP without extracting digits from addresses."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9./\s-]+", value):
        return None
    digits = re.sub(r"[./\s-]", "", value)
    return digits if len(digits) == length else None


def _project(row):
    if not isinstance(row, dict):
        raise ValueError("Registro inválido no catálogo público.")
    projected = {name: row.get(name) for name in PUBLIC_FIELDS}
    for name in ("station_id", "cnpj", "name", "municipality", "uf"):
        if not isinstance(projected[name], str) or (name != "cnpj" and not projected[name].strip()):
            raise ValueError(f"{name} deve ser texto no catálogo público.")
    status = projected["coordinate_status"]
    if status not in _STATUSES:
        raise ValueError("Situação de coordenada desconhecida no catálogo público.")
    if status in AVAILABLE_STATUSES:
        for name, bound in (("latitude", 90), ("longitude", 180)):
            number = projected[name]
            if isinstance(number, bool) or not isinstance(number, Real) or not math.isfinite(number) or abs(number) > bound:
                raise ValueError("Coordenadas utilizáveis inválidas no catálogo público.")
            projected[name] = float(number)
    else:
        projected["latitude"] = projected["longitude"] = None
    for name in ("address", "neighborhood", "postal_code", "coordinate_source", "coordinate_date", "coordinate_precision"):
        if projected[name] is not None and not isinstance(projected[name], str):
            raise ValueError(f"{name} deve ser texto ou nulo no catálogo público.")
    projected["segment"] = "nao_informado"
    return projected


class WebCatalog:
    """Immutable internal rows; each public method returns fresh JSON objects."""

    def __init__(self, path, manifest_path=None):
        path = Path(path)
        packed = path.read_bytes()
        raw = gzip.decompress(packed) if path.suffix == ".gz" else packed
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"version", "stations"}:
            raise ValueError("Estrutura inválida do catálogo público.")
        metadata = json.loads(Path(manifest_path).read_text(encoding="utf-8")) if manifest_path else {}
        if metadata.get("version", payload["version"]) != payload["version"]:
            raise ValueError("Versão do manifesto difere do catálogo público.")
        for name, content in (("sha256", packed), ("payload_sha256", raw)):
            if name in metadata and metadata[name] != hashlib.sha256(content).hexdigest():
                raise ValueError("Hash do catálogo público difere do manifesto.")
        self._initialize(payload["stations"], payload["version"], metadata, hashlib.sha256(raw).hexdigest())
        self.sha256 = hashlib.sha256(packed).hexdigest()
        if "counts" in metadata:
            expected = metadata["counts"]
            actual = self.summary()["totals"]
            if not isinstance(expected, dict) or set(expected) != set(actual) or any(type(expected[key]) is not int or expected[key] != value for key, value in actual.items()):
                raise ValueError("Contagens do catálogo público diferem do manifesto.")

    @classmethod
    def from_records(cls, stations, *, version="fixture", metadata=None):
        instance = cls.__new__(cls)
        instance._initialize(stations, version, metadata or {}, None)
        return instance

    def _initialize(self, stations, version, metadata, digest):
        if not isinstance(version, str) or not version.strip() or not isinstance(stations, list):
            raise ValueError("Versão ou lista de postos inválida no catálogo público.")
        rows = sorted((_project(row) for row in stations), key=lambda row: row["station_id"])
        if len({row["station_id"] for row in rows}) != len(rows):
            raise ValueError("Identificadores duplicados no catálogo público.")
        self.version = version
        self._rows = tuple(MappingProxyType(row) for row in rows)
        self._by_id = MappingProxyType({row["station_id"]: row for row in self._rows})
        self._search = tuple(normalize(" ".join(str(row.get(name) or "") for name in ("station_id", "cnpj", "name", "municipality", "uf", "address", "neighborhood", "postal_code"))) for row in self._rows)
        self._identifiers = tuple((_identifier_digits(row["cnpj"], 14), _identifier_digits(row["postal_code"], 8)) for row in self._rows)
        canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.sha256 = digest or hashlib.sha256(canonical).hexdigest()
        self.payload_sha256 = self.sha256
        # Only known provenance metadata may leave the service.
        self._metadata = {name: deepcopy(metadata[name]) for name in ("source", "source_version", "generated_at", "snapshot_version", "resolution_version", "attribution") if name in metadata}

    @property
    def count(self):
        return len(self._rows)

    def station(self, station_id):
        row = self._by_id.get(station_id)
        return dict(row) if row is not None else None

    def rows(self, *, available_only=False):
        return [dict(row) for row in self._rows if not available_only or row["coordinate_status"] in AVAILABLE_STATUSES]

    def provenance(self):
        return {"version": self.version, "sha256": self.sha256, "payload_sha256": self.payload_sha256, "counts": self.summary()["totals"], **deepcopy(self._metadata)}

    def summary(self):
        totals = Counter(row["coordinate_status"] for row in self._rows)
        municipalities = {}
        for row in self._rows:
            key = (row["uf"], row["municipality"])
            summary = municipalities.setdefault(key, {"uf": key[0], "municipality": key[1], "total": 0, "available": 0})
            summary["total"] += 1
            summary["available"] += row["coordinate_status"] in AVAILABLE_STATUSES
        available = sum(totals[status] for status in AVAILABLE_STATUSES)
        return {"version": self.version, "totals": {"total": self.count, "available": available, "missing": totals["missing"], "review": totals["review_required"], "pending": self.count - available}, "municipalities": [municipalities[key] for key in sorted(municipalities)], "notice": CATALOG_NOTICE}

    def _matches(self, *, uf=None, municipality=None, q=None, status="all"):
        if not isinstance(status, str) or status not in {"all", "available", "missing", "review"}:
            raise ValueError("Situação inválida; use all, available, missing ou review.")
        if any(value is not None and (not isinstance(value, str) or len(value) > 200) for value in (uf, municipality, q)):
            raise ValueError("Filtros de busca devem ser texto com até 200 caracteres.")
        uf, municipality, q = normalize(uf or ""), normalize(municipality or ""), normalize(q or "")
        numeric_q = _identifier_digits(q, 14) or _identifier_digits(q, 8)
        for row, text, identifiers in zip(self._rows, self._search, self._identifiers):
            if uf and normalize(row["uf"]) != uf or municipality and normalize(row["municipality"]) != municipality:
                continue
            if q and q not in text and (numeric_q is None or numeric_q not in identifiers):
                continue
            coordinate_status = row["coordinate_status"]
            if status == "available" and coordinate_status not in AVAILABLE_STATUSES or status == "missing" and coordinate_status != "missing" or status == "review" and coordinate_status != "review_required":
                continue
            yield row

    def search(self, *, uf=None, municipality=None, q=None, status="all", offset=0, limit=200):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Deslocamento deve ser inteiro não negativo.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Limite deve ser inteiro entre 1 e 1000.")
        selected = []
        total = 0
        for row in self._matches(uf=uf, municipality=municipality, q=q, status=status):
            if offset <= total < offset + limit:
                selected.append(dict(row))
            total += 1
        return {"version": self.version, "total": total, "offset": offset, "limit": limit, "stations": selected}

    def map_stations(self, *, uf=None, municipality=None, q=None, status="all", limit=1000):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Limite deve ser inteiro entre 1 e 1000.")
        selected = []
        total = available = 0
        for row in self._matches(uf=uf, municipality=municipality, q=q, status=status):
            total += 1
            if row["coordinate_status"] in AVAILABLE_STATUSES:
                available += 1
                if len(selected) < limit:
                    selected.append(dict(row))
        return {"version": self.version, "total": total, "available": available, "returned": len(selected), "limit": limit, "truncated": available > len(selected), "stations": selected}


@lru_cache(maxsize=1)
def get_default_catalog():
    root = Path(__file__).resolve().parents[2]
    return WebCatalog(root / "web/data/catalog.json.gz", root / "web/data/catalog-manifest.json")
