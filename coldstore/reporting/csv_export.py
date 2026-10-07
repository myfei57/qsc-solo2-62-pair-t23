"""CSV rendering for operator exports, driven by field contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from .contract import Contract, Field

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from coldstore.audit import Auditor
    from coldstore.console.telemetry import Telemetry

QUOTE_TRIGGERS = (",", '"', "\n")

AUDIT_CONTRACT = Contract(
    id="audit",
    version=1,
    note="runtime audit trail",
    fields=(
        Field("time", "time"),
        Field("kind", "kind"),
        Field("detail", "detail"),
        Field("id", "id"),
    ),
)

TELEMETRY_CONTRACT = Contract(
    id="telemetry",
    version=1,
    note="runtime telemetry counters",
    fields=(
        Field("metric", "metric"),
        Field("value", "value"),
    ),
)

BUILTIN_CONTRACTS = (AUDIT_CONTRACT, TELEMETRY_CONTRACT)


def _escape(value: object) -> str:
    text = "" if value is None else str(value)
    if any(trigger in text for trigger in QUOTE_TRIGGERS):
        return '"' + text.replace('"', '""') + '"'
    return text


def to_csv(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Render a header row and body rows as comma separated text."""

    lines = [",".join(_escape(column) for column in columns)]
    for row in rows:
        lines.append(",".join(_escape(value) for value in row))
    return "\n".join(lines) + "\n"


def render_cell(field: Field, record: Mapping[str, object]) -> str:
    """One cell; a missing value takes the field's declared ``null`` text."""

    value = record.get(field.source)
    if value is None:
        return field.null
    return str(value)


def render_record(contract: Contract, record: Mapping[str, object]) -> list[str]:
    """One body row in contract column order."""

    return [render_cell(field, record) for field in contract.fields]


def render_header(contract: Contract) -> str:
    """The header line for a contract, including the trailing newline."""

    return ",".join(_escape(column) for column in contract.columns()) + "\n"


def render_batch(contract: Contract, records: Sequence[Mapping[str, object]]) -> str:
    """Body lines for one batch; no header, newline terminated when non-empty."""

    lines = [
        ",".join(_escape(cell) for cell in render_record(contract, record))
        for record in records
    ]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def audit_export(auditor: Auditor) -> tuple[list[str], list[list[str]]]:
    """Columns and rows for the runtime audit trail."""

    rows = [render_record(AUDIT_CONTRACT, entry.as_dict()) for entry in auditor.entries()]
    return AUDIT_CONTRACT.columns(), rows


def telemetry_export(telemetry: Telemetry) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the telemetry counters."""

    records = [
        {"metric": key, "value": value} for key, value in telemetry.as_dict().items()
    ]
    rows = [render_record(TELEMETRY_CONTRACT, record) for record in records]
    return TELEMETRY_CONTRACT.columns(), rows
