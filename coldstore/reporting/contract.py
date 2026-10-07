"""Versioned field contracts that pin every CSV export to a declared schema."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from coldstore.store.commands import load_commands
from coldstore.store.store import Store

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from coldstore.audit import Auditor

CONTRACT_KEY = "export:contracts"

ACTIVE = "active"
DEPRECATED = "deprecated"
SUNSET = "sunset"
STATUSES = (ACTIVE, DEPRECATED, SUNSET)


@dataclass(frozen=True)
class Field:
    """One exported column bound to a key of the shared row projection.

    ``null`` is the text written when the projection has no value for the
    column.  The convention is an empty cell; a field may declare a
    placeholder (for example ``"0"``) when downstream expects one.
    """

    name: str
    source: str
    null: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "source": self.source, "null": self.null}

    @staticmethod
    def from_dict(payload: dict[str, object]) -> "Field":
        name = str(payload.get("name", ""))
        return Field(
            name=name,
            source=str(payload.get("source", name)),
            null=str(payload.get("null", "")),
        )


@dataclass(frozen=True)
class Change:
    """One schema difference between two contract versions."""

    kind: str
    field: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "field": self.field, "detail": self.detail}


@dataclass(frozen=True)
class Contract:
    """One immutable version of an export schema.

    ``sunset`` is the last day (YYYY-MM-DD) a deprecated version may still
    serve exports; after it the version is refused.
    """

    id: str
    version: int
    fields: tuple[Field, ...]
    status: str = ACTIVE
    sunset: str = ""
    note: str = ""

    def columns(self) -> list[str]:
        return [field.name for field in self.fields]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "version": self.version,
            "status": self.status,
            "sunset": self.sunset,
            "note": self.note,
            "fields": [field.as_dict() for field in self.fields],
        }

    @staticmethod
    def from_dict(payload: dict[str, object]) -> "Contract":
        fields = payload.get("fields", [])
        return Contract(
            id=str(payload.get("id", "")),
            version=int(payload.get("version", 0)),
            status=str(payload.get("status", ACTIVE)),
            sunset=str(payload.get("sunset", "")),
            note=str(payload.get("note", "")),
            fields=tuple(
                Field.from_dict(item) for item in fields if isinstance(item, dict)
            ),
        )

    def with_status(self, status: str, sunset: str = "") -> "Contract":
        return Contract(
            id=self.id,
            version=self.version,
            fields=self.fields,
            status=status,
            sunset=sunset or self.sunset,
            note=self.note,
        )


def diff_contracts(old: Contract | None, new: Contract) -> list[Change]:
    """Schema changes from ``old`` to ``new``; a creation entry without history."""

    if old is None:
        return [Change("created", new.id, f"contract {new.id} v{new.version} registered")]
    changes: list[Change] = []
    old_by_name = {field.name: field for field in old.fields}
    new_by_name = {field.name: field for field in new.fields}
    old_by_source = {field.source: field for field in old.fields}
    new_sources = {field.source for field in new.fields}
    for field in new.fields:
        previous = old_by_name.get(field.name)
        if previous is None:
            renamed = old_by_source.get(field.source)
            if renamed is not None and renamed.name not in new_by_name:
                changes.append(
                    Change("renamed", field.name, f"{renamed.name} -> {field.name}")
                )
            else:
                changes.append(Change("added", field.name, f"column {field.name} added"))
            continue
        if previous.source != field.source:
            changes.append(
                Change("rebound", field.name, f"source {previous.source} -> {field.source}")
            )
        if previous.null != field.null:
            changes.append(
                Change("null", field.name, f"missing value {previous.null!r} -> {field.null!r}")
            )
    for field in old.fields:
        if field.name not in new_by_name and field.source not in new_sources:
            changes.append(Change("removed", field.name, f"column {field.name} removed"))
    return changes


class ContractRegistry:
    """Persists contract versions and records every change in the ledger."""

    def __init__(self, store: Store, ledger: "Auditor | None" = None) -> None:
        self._store = store
        self._ledger = ledger

    def register(self, contract: Contract, reason: str = "") -> tuple[Contract, list[Change]]:
        """Add a new immutable version; the diff and reason go to the ledger."""

        if not contract.id:
            raise ValueError("contract id is required")
        if contract.version < 1:
            raise ValueError("contract version must be a positive integer")
        if not contract.fields:
            raise ValueError("contract must declare at least one field")
        if any(not field.name for field in contract.fields):
            raise ValueError("every contract field needs a column name")
        names = contract.columns()
        if len(set(names)) != len(names):
            raise ValueError("contract columns must be unique")
        if self.get(contract.id, contract.version) is not None:
            raise ValueError(f"contract {contract.id} v{contract.version} already exists")
        changes = diff_contracts(self.latest(contract.id), contract)
        contracts = self._load()
        contracts.append(contract)
        self._save(contracts)
        self._record("registered", contract, changes, reason)
        return contract, changes

    def deprecate(
        self, contract_id: str, version: int, sunset: str, reason: str = ""
    ) -> Contract:
        """Mark a version deprecated; it keeps serving until ``sunset`` passes."""

        contracts = self._load()
        for index, contract in enumerate(contracts):
            if contract.id == contract_id and contract.version == version:
                updated = contract.with_status(DEPRECATED, sunset)
                contracts[index] = updated
                self._save(contracts)
                self._record("deprecated", updated, [], reason)
                return updated
        raise ValueError(f"no contract {contract_id} v{version}")

    def get(self, contract_id: str, version: int | None = None) -> Contract | None:
        if version is None:
            return self.latest(contract_id)
        for contract in self._load():
            if contract.id == contract_id and contract.version == version:
                return contract
        return None

    def latest(self, contract_id: str) -> Contract | None:
        versions = [contract for contract in self._load() if contract.id == contract_id]
        if not versions:
            return None
        return max(versions, key=lambda contract: contract.version)

    def latest_serveable(self, contract_id: str, today: str) -> Contract | None:
        """Highest version still allowed to serve on ``today``."""

        versions = [
            contract
            for contract in self._load()
            if contract.id == contract_id and self.serveable(contract, today)
        ]
        if not versions:
            return None
        return max(versions, key=lambda contract: contract.version)

    def list(self) -> list[Contract]:
        return sorted(self._load(), key=lambda contract: (contract.id, contract.version))

    def serveable(self, contract: Contract, today: str) -> bool:
        """A version serves until its sunset day has passed."""

        if contract.status == SUNSET:
            return False
        if contract.sunset and contract.sunset < today:
            return False
        return True

    def _load(self) -> list[Contract]:
        contracts: list[Contract] = []
        for item in load_commands(self._store, CONTRACT_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if isinstance(payload, dict):
                contracts.append(Contract.from_dict(payload))
        return contracts

    def _save(self, contracts: list[Contract]) -> None:
        items = [json.dumps(contract.as_dict(), ensure_ascii=False) for contract in contracts]
        self._store.put(CONTRACT_KEY, json.dumps(items, ensure_ascii=False))

    def _record(
        self, action: str, contract: Contract, changes: list[Change], reason: str
    ) -> None:
        if self._ledger is None:
            return
        detail = {
            "action": action,
            "contract": contract.id,
            "version": contract.version,
            "status": contract.status,
            "sunset": contract.sunset,
            "columns": contract.columns(),
            "changes": [change.as_dict() for change in changes],
            "reason": reason,
        }
        self._ledger.record("export.contract", json.dumps(detail, ensure_ascii=False))
