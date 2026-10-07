"""Tests for field contracts, batched export runs and the export ledger."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

from coldstore.audit import Auditor
from coldstore.console.seed import seed_defaults
from coldstore.console.server import Server
from coldstore.reporting import (
    ACTIVE,
    COMPLETED,
    DEPRECATED,
    FAILED,
    Contract,
    ContractRegistry,
    ExportLimits,
    ExportRunner,
    Field,
    diff_contracts,
    render_record,
)
from coldstore.store import Store

AUDIT_V2 = Contract(
    id="audit",
    version=2,
    note="rename detail to message",
    fields=(
        Field("time", "time"),
        Field("kind", "kind"),
        Field("message", "detail"),
        Field("id", "id"),
        Field("operator", "operator", null="-"),
    ),
)


class ContractCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.ledger = Auditor(self.store, key="export:ledger")
        self.registry = ContractRegistry(self.store, self.ledger)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def register_builtins(self) -> None:
        self.registry.register(
            Contract(
                id="audit",
                version=1,
                fields=(
                    Field("time", "time"),
                    Field("kind", "kind"),
                    Field("detail", "detail"),
                    Field("id", "id"),
                ),
            ),
            reason="built-in default",
        )

    def ledger_details(self) -> list[dict]:
        return [json.loads(entry.detail) for entry in self.ledger.entries()]

    def test_register_records_diff_and_reason_in_ledger(self) -> None:
        self.register_builtins()
        _, changes = self.registry.register(AUDIT_V2, reason="rename for analytics")
        kinds = [change.kind for change in changes]
        self.assertEqual(kinds, ["renamed", "added"])
        self.assertEqual(changes[0].detail, "detail -> message")

        entries = self.ledger_details()
        self.assertEqual([entry["action"] for entry in entries], ["registered", "registered"])
        self.assertEqual(entries[1]["contract"], "audit")
        self.assertEqual(entries[1]["version"], 2)
        self.assertEqual(entries[1]["reason"], "rename for analytics")
        self.assertEqual(entries[1]["columns"], ["time", "kind", "message", "id", "operator"])

    def test_diff_detects_removal_and_null_change(self) -> None:
        old = Contract(
            id="audit",
            version=1,
            fields=(Field("a", "a"), Field("b", "b"), Field("c", "c")),
        )
        new = Contract(
            id="audit",
            version=2,
            fields=(Field("a", "a"), Field("b", "b", null="0")),
        )
        changes = diff_contracts(old, new)
        self.assertEqual(
            [(change.kind, change.field) for change in changes],
            [("null", "b"), ("removed", "c")],
        )

    def test_versions_are_immutable_and_columns_unique(self) -> None:
        self.register_builtins()
        with self.assertRaises(ValueError):
            self.registry.register(
                Contract(id="audit", version=1, fields=(Field("x", "x"),))
            )
        with self.assertRaises(ValueError):
            self.registry.register(
                Contract(
                    id="audit",
                    version=2,
                    fields=(Field("x", "x"), Field("x", "y")),
                )
            )

    def test_deprecate_keeps_version_servable_until_sunset(self) -> None:
        self.register_builtins()
        updated = self.registry.deprecate("audit", 1, "2099-01-01", reason="migrate")
        self.assertEqual(updated.status, DEPRECATED)
        self.assertTrue(self.registry.serveable(updated, "2026-10-06"))
        expired = self.registry.deprecate("audit", 1, "2000-01-01")
        self.assertFalse(self.registry.serveable(expired, "2026-10-06"))
        entries = self.ledger_details()
        self.assertEqual(entries[-1]["action"], "deprecated")
        self.assertEqual(entries[-2]["reason"], "migrate")

    def test_latest_serveable_skips_expired_versions(self) -> None:
        self.register_builtins()
        self.registry.register(AUDIT_V2)
        self.assertEqual(self.registry.latest_serveable("audit", "2026-10-06").version, 2)
        self.registry.deprecate("audit", 2, "2000-01-01")
        self.assertEqual(self.registry.latest_serveable("audit", "2026-10-06").version, 1)


class RenderCase(unittest.TestCase):
    def test_missing_values_default_to_empty_cell(self) -> None:
        contract = Contract(
            id="audit", version=1, fields=(Field("a", "a"), Field("b", "b"))
        )
        self.assertEqual(render_record(contract, {"a": None}), ["", ""])

    def test_field_may_declare_a_placeholder(self) -> None:
        contract = Contract(
            id="audit",
            version=1,
            fields=(Field("a", "a"), Field("b", "b", null="0")),
        )
        self.assertEqual(render_record(contract, {"a": 1}), ["1", "0"])


class RunnerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.ledger = Auditor(self.store, key="export:ledger")
        self.contract = Contract(
            id="audit",
            version=1,
            fields=(Field("kind", "kind"), Field("amount", "amount", null="0")),
        )

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def runner(self, **limits: int) -> ExportRunner:
        return ExportRunner(
            self.store,
            self.ledger,
            f"{self._tmp.name}/exports",
            ExportLimits(**limits),
        )

    def records(self, count: int) -> list[dict[str, object]]:
        return [{"kind": "cycle", "amount": index} for index in range(count)]

    def test_run_publishes_one_whole_file_after_staged_batches(self) -> None:
        runner = self.runner(batch_size=2, max_rows=100)
        manifest = runner.run(self.contract, self.records(5))
        self.assertEqual(manifest.status, COMPLETED)
        self.assertEqual(manifest.rows, 5)
        self.assertEqual(manifest.batches, 3)

        names = os.listdir(runner.directory)
        self.assertEqual(names, [f"{manifest.id}.csv"])
        with open(manifest.path, "r", encoding="utf-8") as handle:
            body = handle.read()
        self.assertEqual(
            body,
            "kind,amount\ncycle,0\ncycle,1\ncycle,2\ncycle,3\ncycle,4\n",
        )
        self.assertEqual(manifest.sha256, hashlib.sha256(body.encode("utf-8")).hexdigest())
        self.assertEqual(manifest.bytes, len(body.encode("utf-8")))

        stored = runner.get(manifest.id)
        self.assertEqual(stored, manifest)
        self.assertEqual([item.id for item in runner.list()], [manifest.id])
        actions = [json.loads(entry.detail)["action"] for entry in self.ledger.entries()]
        self.assertEqual(actions, ["started", "completed"])

    def test_run_beyond_the_row_limit_fails_without_artifact(self) -> None:
        runner = self.runner(batch_size=2, max_rows=3)
        manifest = runner.run(self.contract, self.records(5))
        self.assertEqual(manifest.status, FAILED)
        self.assertIn("row limit 3 exceeded", manifest.reason)
        self.assertEqual(os.listdir(runner.directory), [])
        actions = [json.loads(entry.detail)["action"] for entry in self.ledger.entries()]
        self.assertEqual(actions, ["started", "failed"])

    def test_missing_values_use_the_contract_placeholder(self) -> None:
        runner = self.runner(batch_size=10, max_rows=100)
        manifest = runner.run(self.contract, [{"kind": "cycle"}])
        with open(manifest.path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "kind,amount\ncycle,0\n")


class ConsoleExportCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def cycle_once(self) -> None:
        status, _ = self.call(
            "POST",
            "/cycle",
            {"room_id": "", "comp_id": "", "temp": -5.0, "ppm": 1.0, "amount": 2.0},
        )
        self.assertEqual(status, 200)

    def register_v2(self) -> None:
        status, body = self.call(
            "POST",
            "/export/contracts",
            {
                "id": "audit",
                "version": 2,
                "reason": "rename detail to message",
                "fields": [
                    {"name": "time", "source": "time"},
                    {"name": "kind", "source": "kind"},
                    {"name": "message", "source": "detail"},
                    {"name": "id", "source": "id"},
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [change["kind"] for change in body["changes"]], ["renamed"]
        )

    def test_builtin_contracts_registered_once_at_boot(self) -> None:
        status, body = self.call("GET", "/export/contracts")
        self.assertEqual(status, 200)
        pairs = {(c["id"], c["version"], c["status"]) for c in body["contracts"]}
        self.assertIn(("audit", 1, ACTIVE), pairs)
        self.assertIn(("telemetry", 1, ACTIVE), pairs)
        self.assertTrue(all(c["serveable"] for c in body["contracts"]))
        # The control decision trail stays untouched by export bookkeeping.
        status, summary = self.call("GET", "/audit/summary")
        self.assertEqual(summary["count"], 0)

    def test_live_export_follows_the_latest_serveable_contract(self) -> None:
        status, body = self.call("GET", "/export/audit")
        self.assertIn("time,kind,detail,id", body)
        self.register_v2()
        status, body = self.call("GET", "/export/audit")
        self.assertTrue(body.startswith("time,kind,message,id\n"))

    def test_pinned_version_serves_during_and_not_after_compat_window(self) -> None:
        self.cycle_once()
        self.register_v2()
        # Compatibility window: v1 still serves when pinned.
        status, body = self.call("POST", "/export/audit/archive", {"version": 1})
        self.assertEqual(status, 200)
        self.assertEqual(body["run"]["version"], 1)
        status, csv_body = self.call("POST", "/export/file", {"id": body["run"]["id"]})
        self.assertTrue(csv_body.startswith("time,kind,detail,id\n"))
        # Sunset passes: v1 is refused, the reason is in the ledger.
        status, _ = self.call(
            "POST",
            "/export/contracts/deprecate",
            {"id": "audit", "version": 1, "sunset": "2000-01-01", "reason": "migrate to v2"},
        )
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/export/audit/archive", {"version": 1})
        self.assertEqual(status, 400)
        self.assertIn("sunset", body["error"])
        status, body = self.call("GET", "/export/ledger")
        deprecated = [
            json.loads(entry["detail"])
            for entry in body["entries"]
            if json.loads(entry["detail"])["action"] == "deprecated"
        ]
        self.assertEqual(deprecated[0]["reason"], "migrate to v2")

    def test_archive_run_manifest_file_and_page_agree(self) -> None:
        self.cycle_once()
        status, body = self.call("POST", "/export/audit/archive", {})
        self.assertEqual(status, 200)
        run = body["run"]
        self.assertEqual(run["status"], COMPLETED)
        self.assertEqual(run["rows"], 1)
        self.assertEqual(run["batches"], 1)

        status, page = self.call("GET", "/export/runs")
        self.assertEqual([item["id"] for item in page["runs"]], [run["id"]])
        self.assertEqual(page["runs"][0]["artifact"], "available")

        status, csv_body = self.call("POST", "/export/file", {"id": run["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(
            run["sha256"], hashlib.sha256(csv_body.encode("utf-8")).hexdigest()
        )
        self.assertEqual(len(csv_body.strip().split("\n")) - 1, run["rows"])

        status, fetched = self.call("POST", "/export/run", {"id": run["id"]})
        self.assertEqual(fetched["run"], run)
        status, body = self.call("POST", "/export/run", {"id": "nope"})
        self.assertEqual(status, 404)

    def test_export_matches_the_json_api_for_the_same_rows(self) -> None:
        self.cycle_once()
        _, audit = self.call("GET", "/audit")
        entry = audit["entries"][0]
        _, body = self.call("POST", "/export/audit/archive", {})
        _, csv_body = self.call("POST", "/export/file", {"id": body["run"]["id"]})
        header, row = csv_body.strip().split("\n")
        self.assertEqual(header, "time,kind,detail,id")
        self.assertEqual(
            row.split(","),
            [entry["time"], entry["kind"], entry["detail"], entry["id"]],
        )

    def test_ledger_records_contract_and_run_outcomes(self) -> None:
        self.cycle_once()
        self.register_v2()
        self.call("POST", "/export/audit/archive", {})
        status, body = self.call("GET", "/export/ledger")
        details = [json.loads(entry["detail"]) for entry in body["entries"]]
        kinds = {entry["kind"] for entry in body["entries"]}
        self.assertEqual(kinds, {"export.contract", "export.run"})
        registered = [d for d in details if d.get("action") == "registered"]
        self.assertEqual(registered[-1]["reason"], "rename detail to message")
        completed = [d for d in details if d.get("action") == "completed"]
        self.assertEqual(completed[0]["rows"], 1)
        self.assertEqual(completed[0]["contract"], "audit")
        self.assertEqual(completed[0]["version"], 2)


if __name__ == "__main__":
    unittest.main()
