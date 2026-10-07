"""CSV rendering and atomic, batch-bounded export materialization."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence

from coldstore.store import Store

from .contracts import ExportContract, FieldSpec, resolve_contract, value_matches_type
from .ledger import append_receipt, new_receipt

QUOTE_TRIGGERS = (",", '"', "\n")
MAX_EXPORT_BATCH_SIZE = 1000
MAX_DIFFERENCE_SAMPLES = 100


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


def _csv_line(values: Iterable[object]) -> str:
    return ",".join(_escape(value) for value in values) + "\n"


def _difference(sample: list[dict[str, object]], row_index: int, field: FieldSpec, reason: str) -> None:
    if len(sample) < MAX_DIFFERENCE_SAMPLES:
        sample.append(
            {
                "row": row_index,
                "column": field.name,
                "api_name": field.api_name,
                "reason": reason,
            }
        )


def project_rows(
    contract: ExportContract,
    records: Iterable[dict[str, object]],
    api_records: Iterable[dict[str, object]] | None = None,
) -> tuple[list[list[str]], dict[str, object]]:
    """Project canonical records onto contract columns and reconcile values."""

    rows: list[list[str]] = []
    summary = _empty_summary()
    api_values = list(api_records) if api_records is not None else None

    for row_index, record in enumerate(records):
        if api_values is not None and row_index >= len(api_values):
            _add_count(summary, "api_mismatch_count", 1)
            _difference(summary["differences"], row_index, contract.fields[0], "api_mismatch")
        row, row_summary = project_row(contract, record, row_index, api_values)
        _add_count(row_summary, "rows", 1)
        _merge_summary(summary, row_summary)
        rows.append(row)

    if api_values is not None and len(api_values) > int(summary["rows"]):
        extra_rows = len(api_values) - int(summary["rows"])
        _add_count(summary, "api_mismatch_count", extra_rows)
        if len(summary["differences"]) < MAX_DIFFERENCE_SAMPLES:
            summary["differences"].append(
                {"row": int(summary["rows"]), "column": "", "api_name": "", "reason": "row_count_mismatch"}
            )

    return rows, summary


def project_row(
    contract: ExportContract,
    record: dict[str, object],
    row_index: int,
    api_records: list[dict[str, object]] | None = None,
) -> tuple[list[str], dict[str, object]]:
    """Project one record and report its missing, type and API differences."""

    row: list[str] = []
    summary = _empty_summary()
    api_record = api_records[row_index] if api_records is not None and row_index < len(api_records) else None

    for field in contract.fields:
        value = _field_value(record, field)
        if value is None:
            _add_count(summary, "missing_count", 1)
            _difference(summary["differences"], row_index, field, "missing")
            row.append("")
            continue
        if not value_matches_type(value, field.value_type):
            _add_count(summary, "type_mismatch_count", 1)
            _difference(summary["differences"], row_index, field, "type_mismatch")
        if api_record is not None and api_record.get(field.api_name) != value:
            _add_count(summary, "api_mismatch_count", 1)
            _difference(summary["differences"], row_index, field, "api_mismatch")
        row.append(str(value))

    return row, summary


def _field_value(record: dict[str, object], field: FieldSpec) -> object:
    value = record.get(field.api_name)
    if value is not None:
        return value
    for alias in field.aliases:
        if alias in record:
            return record[alias]
    return None


def _empty_summary() -> dict[str, object]:
    return {
        "rows": 0,
        "missing_count": 0,
        "type_mismatch_count": 0,
        "api_mismatch_count": 0,
        "differences": [],
    }


def _add_count(summary: dict[str, object], key: str, amount: int) -> None:
    summary[key] = int(summary[key]) + amount


def _merge_summary(target: dict[str, object], source: dict[str, object]) -> None:
    _add_count(target, "rows", int(source["rows"]))
    for key in ("missing_count", "type_mismatch_count", "api_mismatch_count"):
        _add_count(target, key, int(source[key]))
    remaining = MAX_DIFFERENCE_SAMPLES - len(target["differences"])
    if remaining > 0:
        target["differences"].extend(list(source["differences"])[:remaining])


def _batched(records: Iterable[dict[str, object]], batch_size: int) -> Iterator[list[dict[str, object]]]:
    batch: list[dict[str, object]] = []
    for record in records:
        batch.append(record)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def materialize_export(
    export_name: str,
    records: Iterable[dict[str, object]],
    output_dir: str,
    *,
    store: Store,
    version: str | None = None,
    batch_size: int = MAX_EXPORT_BATCH_SIZE,
    api_records: Iterable[dict[str, object]] | None = None,
    file_path: str | None = None,
) -> tuple[str, dict[str, object]]:
    """Build one complete CSV atomically, processing records in bounded batches."""

    contract = resolve_contract(export_name, version)
    if batch_size < 1 or batch_size > MAX_EXPORT_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_EXPORT_BATCH_SIZE}")

    api_values = list(api_records) if api_records is not None else None
    target_path = file_path or _default_file_path(output_dir, export_name, contract.version)
    temporary_path = f"{target_path}.tmp"
    os.makedirs(os.path.dirname(os.path.abspath(target_path)) or ".", exist_ok=True)

    digest = hashlib.sha256()
    summary = _empty_summary()
    batches = 0
    row_index = 0
    with open(temporary_path, "w", encoding="utf-8", newline="") as handle:
        header = _csv_line(contract.columns)
        handle.write(header)
        digest.update(header.encode("utf-8"))
        for record_batch in _batched(records, batch_size):
            lines: list[str] = []
            for record in record_batch:
                if api_values is not None and row_index >= len(api_values):
                    _add_count(summary, "api_mismatch_count", 1)
                    _difference(summary["differences"], row_index, contract.fields[0], "api_mismatch")
                row, row_summary = project_row(contract, record, row_index, api_values)
                row_index += 1
                _add_count(row_summary, "rows", 1)
                lines.append(_csv_line(row))
                _merge_summary(summary, row_summary)
            text = "".join(lines)
            handle.write(text)
            digest.update(text.encode("utf-8"))
            batches += 1

    if api_values is not None and len(api_values) > row_index:
        extra_rows = len(api_values) - row_index
        _add_count(summary, "api_mismatch_count", extra_rows)
        if len(summary["differences"]) < MAX_DIFFERENCE_SAMPLES:
            summary["differences"].append(
                {"row": row_index, "column": "", "api_name": "", "reason": "row_count_mismatch"}
            )

    os.replace(temporary_path, target_path)
    receipt = new_receipt(
        export_name=export_name,
        contract_version=contract.version,
        contract_fingerprint=contract.fingerprint,
        batch_size=batch_size,
        batches=batches,
        file_path=target_path,
        checksum=digest.hexdigest(),
        contract=contract.as_dict(),
        summary=summary,
    )
    append_receipt(store, receipt)
    return target_path, {"receipt": receipt.as_dict(), "summary": summary, "contract": contract.as_dict()}


def _default_file_path(output_dir: str, export_name: str, version: str) -> str:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return os.path.join(output_dir, f"{export_name}-{version}-{timestamp}-{uuid.uuid4().hex[:8]}.csv")
