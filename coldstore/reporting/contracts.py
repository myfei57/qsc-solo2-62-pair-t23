"""Versioned field contracts shared by CSV exports and HTTP projections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    """One output column and its canonical source field."""

    name: str
    api_name: str
    value_type: str
    required: bool = True
    missing_policy: str = "blank"
    status: str = "active"
    description: str = ""
    aliases: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "api_name": self.api_name,
            "type": self.value_type,
            "required": self.required,
            "missing_policy": self.missing_policy,
            "status": self.status,
            "description": self.description,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class ExportContract:
    """Frozen columns, types and compatibility rules for one export version."""

    name: str
    version: str
    title: str
    fields: tuple[FieldSpec, ...]
    status: str = "active"
    compatibility: str = "removal requires a new major version"

    def __post_init__(self) -> None:
        if not self.fields:
            raise ValueError("export contract must declare fields")
        columns = [field.name for field in self.fields]
        if len(columns) != len(set(columns)):
            raise ValueError("export contract columns must be unique")
        api_names = [field.api_name for field in self.fields]
        if len(api_names) != len(set(api_names)):
            raise ValueError("export contract source fields must be unique")

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    @property
    def fingerprint(self) -> str:
        payload = {
            "name": self.name,
            "version": self.version,
            "fields": [
                [
                    field.name,
                    field.api_name,
                    field.value_type,
                    field.required,
                    field.missing_policy,
                    list(field.aliases),
                ]
                for field in self.fields
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "title": self.title,
            "status": self.status,
            "compatibility": self.compatibility,
            "columns": list(self.columns),
            "fingerprint": self.fingerprint,
            "missing_policy": "blank",
            "fields": [field.as_dict() for field in self.fields],
        }


def _telemetry_fields() -> tuple[FieldSpec, ...]:
    # Keep this source order aligned with Telemetry.as_dict so the v2 export and
    # JSON interface are projections of the same canonical field sequence.
    types = {
        "store_keys": "integer",
        "rooms": "integer",
        "defrosting": "integer",
        "average_temp": "number",
        "compressors": "integer",
        "compressors_running": "integer",
        "total_stage": "integer",
        "max_stage": "integer",
        "load_ratio": "number",
        "supply_open": "integer",
        "temp_value": "number",
        "temp_rate": "number",
        "temp_within": "boolean",
        "ammonia_last": "number",
        "ammonia_latched": "boolean",
        "interlock_tripped": "boolean",
        "interlock_trips": "integer",
        "cold_primary": "string",
        "cold_running": "integer",
        "evap_temperature": "number",
        "evap_above": "boolean",
        "defrost_warmups": "integer",
        "cooling_load": "number",
        "quota_value": "number",
        "audit_entries": "integer",
        "schedule_due": "integer",
    }
    return tuple(
        FieldSpec(name=name, api_name=name, value_type=value_type)
        for name, value_type in types.items()
    )


AUDIT_V1 = ExportContract(
    name="audit",
    version="v1",
    title="Runtime audit trail with the legacy column order",
    status="legacy",
    fields=(
        FieldSpec("time", "time", "string"),
        FieldSpec("kind", "kind", "string"),
        FieldSpec("detail", "detail", "string"),
        FieldSpec("id", "id", "string"),
    ),
)

AUDIT_V2 = ExportContract(
    name="audit",
    version="v2",
    title="Runtime audit trail aligned to the JSON field order",
    fields=(
        FieldSpec("id", "id", "string"),
        FieldSpec("time", "time", "string"),
        FieldSpec("kind", "kind", "string"),
        FieldSpec("detail", "detail", "string"),
    ),
)

TELEMETRY_V1 = ExportContract(
    name="telemetry",
    version="v1",
    title="Telemetry counters in the legacy long format",
    status="legacy",
    fields=(
        FieldSpec(
            "metric",
            "name",
            "string",
            description="Legacy column for the telemetry metric name",
            aliases=("name",),
        ),
        FieldSpec(
            "value",
            "value",
            "scalar",
            description="Legacy column for the telemetry metric value",
        ),
    ),
)

TELEMETRY_V2 = ExportContract(
    name="telemetry",
    version="v2",
    title="Telemetry counters aligned to the JSON object",
    fields=_telemetry_fields(),
)


_CONTRACTS: dict[tuple[str, str], ExportContract] = {
    (contract.name, contract.version): contract
    for contract in (AUDIT_V1, AUDIT_V2, TELEMETRY_V1, TELEMETRY_V2)
}

DEFAULT_VERSIONS = {"audit": "v1", "telemetry": "v1"}


def resolve_contract(name: str, version: str | None = None) -> ExportContract:
    """Return a registered contract, defaulting to the frozen legacy version."""

    selected_version = version or DEFAULT_VERSIONS[name]
    try:
        return _CONTRACTS[(name, selected_version)]
    except KeyError as exc:
        raise ValueError(f"unknown export contract {name}:{selected_version}") from exc


def contract_catalog() -> list[ExportContract]:
    """Return all supported contracts in stable catalog order."""

    return [AUDIT_V1, AUDIT_V2, TELEMETRY_V1, TELEMETRY_V2]


def value_matches_type(value: object, value_type: str) -> bool:
    """Validate the scalar types allowed in an export contract."""

    if value is None:
        return False
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "scalar":
        return isinstance(value, (str, int, float, bool))
    return False
