"""Append-only traceability receipts for generated exports."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from coldstore.store import Store, append_command, load_commands

LEDGER_KEY = "export:ledger"


@dataclass(frozen=True)
class ExportReceipt:
    """Contract, output and reconciliation details for one export run."""

    id: str
    export_name: str
    contract_version: str
    contract_fingerprint: str
    status: str
    generated_at: str
    generation: str
    missing_policy: str
    batch_size: int
    rows: int
    batches: int
    file_path: str
    checksum: str
    contract: dict[str, object]
    missing_count: int = 0
    type_mismatch_count: int = 0
    api_mismatch_count: int = 0
    differences: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "export_name": self.export_name,
            "contract_version": self.contract_version,
            "contract_fingerprint": self.contract_fingerprint,
            "status": self.generation_status(),
            "generated_at": self.generated_at,
            "generation": self.generation,
            "missing_policy": self.missing_policy,
            "batch_size": self.batch_size,
            "rows": self.rows,
            "batches": self.batches,
            "file_path": self.file_path,
            "checksum": self.checksum,
            "contract": self.contract,
            "missing_count": self.missing_count,
            "type_mismatch_count": self.type_mismatch_count,
            "api_mismatch_count": self.api_mismatch_count,
            "differences": list(self.differences),
        }

    def generation_status(self) -> str:
        if self.missing_count or self.type_mismatch_count or self.api_mismatch_count:
            return "completed_with_differences"
        return self.status


def append_receipt(store: Store, receipt: ExportReceipt) -> None:
    append_command(store, LEDGER_KEY, json.dumps(receipt.as_dict(), ensure_ascii=False))


def list_receipts(store: Store, export_name: str | None = None) -> list[ExportReceipt]:
    receipts: list[ExportReceipt] = []
    for item in load_commands(store, LEDGER_KEY):
        try:
            payload = json.loads(item)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        if export_name is not None and payload.get("export_name") != export_name:
            continue
        receipts.append(
            ExportReceipt(
                id=str(payload.get("id", "")),
                export_name=str(payload.get("export_name", "")),
                contract_version=str(payload.get("contract_version", "")),
                contract_fingerprint=str(payload.get("contract_fingerprint", "")),
                status=str(payload.get("status", "")),
                generated_at=str(payload.get("generated_at", "")),
                generation=str(payload.get("generation", "complete_atomic")),
                missing_policy=str(payload.get("missing_policy", "blank")),
                batch_size=int(payload.get("batch_size", 0)),
                rows=int(payload.get("rows", 0)),
                batches=int(payload.get("batches", 0)),
                file_path=str(payload.get("file_path", "")),
                checksum=str(payload.get("checksum", "")),
                contract=_contract_payload(payload.get("contract")),
                missing_count=int(payload.get("missing_count", 0)),
                type_mismatch_count=int(payload.get("type_mismatch_count", 0)),
                api_mismatch_count=int(payload.get("api_mismatch_count", 0)),
                differences=tuple(_difference_items(payload.get("differences"))),
            )
        )
    return receipts


def new_receipt(
    export_name: str,
    contract_version: str,
    contract_fingerprint: str,
    batch_size: int,
    batches: int,
    file_path: str,
    checksum: str,
    contract: dict[str, object],
    summary: dict[str, object],
) -> ExportReceipt:
    """Build a receipt from a materialized export reconciliation summary."""

    return ExportReceipt(
        id=uuid.uuid4().hex,
        export_name=export_name,
        contract_version=contract_version,
        contract_fingerprint=contract_fingerprint,
        status="completed",
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        generation="complete_atomic",
        missing_policy="blank",
        batch_size=batch_size,
        batches=batches,
        rows=int(summary.get("rows", 0)),
        file_path=file_path,
        checksum=checksum,
        contract=contract,
        missing_count=int(summary.get("missing_count", 0)),
        type_mismatch_count=int(summary.get("type_mismatch_count", 0)),
        api_mismatch_count=int(summary.get("api_mismatch_count", 0)),
        differences=tuple(_difference_items(summary.get("differences"))),
    )


def _contract_payload(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _difference_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    differences: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            differences.append(dict(item))
    return differences
