"""Export projections for runtime audit and telemetry data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from .contracts import resolve_contract
from .csv_export import project_rows

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from coldstore.audit import Auditor
    from coldstore.console.telemetry import Telemetry


def audit_records(auditor: Auditor) -> list[dict[str, str]]:
    """Canonical audit records shared by JSON and both CSV contract versions."""

    return [entry.as_dict() for entry in auditor.entries()]


def iter_audit_records(auditor: Auditor) -> Iterator[dict[str, str]]:
    """Stream canonical audit records for large exports."""

    for entry in auditor.iter_entries():
        yield entry.as_dict()


def audit_export(auditor: Auditor) -> tuple[list[str], list[list[str]]]:
    """Columns and rows for the legacy v1 runtime audit trail."""

    records = audit_records(auditor)
    return ["time", "kind", "detail", "id"], [
        [record["time"], record["kind"], record["detail"], record["id"]] for record in records
    ]


def audit_contract_export(
    auditor: Auditor, version: str | None = None
) -> tuple[list[str], list[list[str]]]:
    contract = resolve_contract("audit", version)
    rows, _ = project_rows(contract, audit_records(auditor))
    return list(contract.columns), rows


def telemetry_records(telemetry: Telemetry) -> list[dict[str, object]]:
    """Canonical telemetry record aligned to the JSON response."""

    return [telemetry.as_dict()]


def telemetry_legacy_records(telemetry: Telemetry) -> list[dict[str, object]]:
    """Legacy long-form records for the original CSV columns."""

    return [{"name": key, "value": value} for key, value in telemetry.as_dict().items()]


def telemetry_export(telemetry: Telemetry) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the legacy v1 telemetry counters."""

    return ["metric", "value"], [[key, value] for key, value in telemetry.as_dict().items()]


def telemetry_contract_export(
    telemetry: Telemetry, version: str | None = None
) -> tuple[list[str], list[list[str]]]:
    contract = resolve_contract("telemetry", version)
    records = telemetry_legacy_records(telemetry) if contract.version == "v1" else telemetry_records(telemetry)
    rows, _ = project_rows(contract, records)
    return list(contract.columns), rows
