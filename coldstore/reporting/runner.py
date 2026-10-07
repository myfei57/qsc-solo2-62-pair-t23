"""Batched export runs: staged in parts, published as one immutable whole."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from coldstore.store.store import Store

from .contract import Contract
from .csv_export import render_batch, render_header

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from coldstore.audit import Auditor

RUN_KEY_PREFIX = "export:run:"
COMPLETED = "completed"
FAILED = "failed"


class ExportError(Exception):
    """Raised when a run breaches its performance limits."""


@dataclass(frozen=True)
class ExportLimits:
    """Performance caps every export run must stay within."""

    batch_size: int = 1000
    max_rows: int = 100000


@dataclass(frozen=True)
class Manifest:
    """The stored record of one export run; the export page reads these."""

    id: str
    contract: str
    version: int
    status: str
    rows: int
    bytes: int
    sha256: str
    batches: int
    started: str
    finished: str
    path: str
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "contract": self.contract,
            "version": self.version,
            "status": self.status,
            "rows": self.rows,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "batches": self.batches,
            "started": self.started,
            "finished": self.finished,
            "path": self.path,
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(payload: dict[str, object]) -> "Manifest":
        return Manifest(
            id=str(payload.get("id", "")),
            contract=str(payload.get("contract", "")),
            version=int(payload.get("version", 0)),
            status=str(payload.get("status", "")),
            rows=int(payload.get("rows", 0)),
            bytes=int(payload.get("bytes", 0)),
            sha256=str(payload.get("sha256", "")),
            batches=int(payload.get("batches", 0)),
            started=str(payload.get("started", "")),
            finished=str(payload.get("finished", "")),
            path=str(payload.get("path", "")),
            reason=str(payload.get("reason", "")),
        )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ExportRunner:
    """Builds an export in staged batches and publishes it atomically.

    Rows stream through fixed size batches into a ``.part`` staging file, so
    memory stays bounded by ``batch_size`` regardless of export volume.  Only
    when every batch is staged does the file move to its final ``.csv`` name;
    downstream and the export page only ever see complete, immutable files.
    """

    def __init__(
        self,
        store: Store,
        ledger: "Auditor | None",
        directory: str,
        limits: ExportLimits | None = None,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._directory = directory
        self._limits = limits or ExportLimits()

    @property
    def directory(self) -> str:
        return self._directory

    @property
    def limits(self) -> ExportLimits:
        return self._limits

    def run(self, contract: Contract, records: Iterable[Mapping[str, object]]) -> Manifest:
        """Export ``records`` under ``contract``; the manifest is the receipt."""

        run_id = uuid.uuid4().hex
        os.makedirs(self._directory, exist_ok=True)
        staging = os.path.join(self._directory, f"{run_id}.part")
        final = os.path.join(self._directory, f"{run_id}.csv")
        started = _now()
        self._record("started", run_id, contract, {})
        rows = 0
        batches = 0
        digest = hashlib.sha256()
        try:
            with open(staging, "w", encoding="utf-8", newline="") as handle:

                def emit(chunk: str) -> None:
                    digest.update(chunk.encode("utf-8"))
                    handle.write(chunk)

                emit(render_header(contract))
                batch: list[Mapping[str, object]] = []
                for record in records:
                    batch.append(record)
                    if len(batch) >= self._limits.batch_size:
                        rows += len(batch)
                        self._check_rows(rows)
                        emit(render_batch(contract, batch))
                        batches += 1
                        batch = []
                if batch:
                    rows += len(batch)
                    self._check_rows(rows)
                    emit(render_batch(contract, batch))
                    batches += 1
            size = os.path.getsize(staging)
            os.replace(staging, final)
        except Exception as exc:
            if os.path.exists(staging):
                os.remove(staging)
            manifest = Manifest(
                id=run_id,
                contract=contract.id,
                version=contract.version,
                status=FAILED,
                rows=rows,
                bytes=0,
                sha256="",
                batches=batches,
                started=started,
                finished=_now(),
                path="",
                reason=str(exc),
            )
            self._save(manifest)
            self._record("failed", run_id, contract, {"reason": str(exc), "rows": rows})
            return manifest
        manifest = Manifest(
            id=run_id,
            contract=contract.id,
            version=contract.version,
            status=COMPLETED,
            rows=rows,
            bytes=size,
            sha256=digest.hexdigest(),
            batches=batches,
            started=started,
            finished=_now(),
            path=final,
        )
        self._save(manifest)
        self._record(
            "completed",
            run_id,
            contract,
            {"rows": rows, "bytes": size, "sha256": manifest.sha256, "batches": batches},
        )
        return manifest

    def get(self, run_id: str) -> Manifest | None:
        raw, present = self._store.get(f"{RUN_KEY_PREFIX}{run_id}")
        if not present:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return Manifest.from_dict(payload)

    def list(self) -> list[Manifest]:
        manifests: list[Manifest] = []
        for key in self._store.keys():
            if not key.startswith(RUN_KEY_PREFIX):
                continue
            manifest = self.get(key[len(RUN_KEY_PREFIX) :])
            if manifest is not None:
                manifests.append(manifest)
        return sorted(manifests, key=lambda manifest: (manifest.started, manifest.id))

    def file_path(self, manifest: Manifest) -> str:
        """Where the published artifact for a run lives on disk."""

        return os.path.join(self._directory, f"{manifest.id}.csv")

    def _check_rows(self, rows: int) -> None:
        if rows > self._limits.max_rows:
            raise ExportError(f"row limit {self._limits.max_rows} exceeded")

    def _save(self, manifest: Manifest) -> None:
        self._store.put(
            f"{RUN_KEY_PREFIX}{manifest.id}",
            json.dumps(manifest.as_dict(), ensure_ascii=False),
        )

    def _record(
        self, action: str, run_id: str, contract: Contract, extra: dict[str, object]
    ) -> None:
        if self._ledger is None:
            return
        detail = {
            "action": action,
            "run": run_id,
            "contract": contract.id,
            "version": contract.version,
            **extra,
        }
        self._ledger.record("export.run", json.dumps(detail, ensure_ascii=False))
