"""Drift tests keeping scripts/dag_manager.py aligned with references/dag-schema.json.

Standard library only; run with::

    python3 -m unittest tests.test_dag_schema_sync -v
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = SKILL_DIR / "scripts" / "dag_manager.py"
SCHEMA_PATH = SKILL_DIR / "references" / "dag-schema.json"


def load_cli_module():
    """Import scripts/dag_manager.py so schema constants can be inspected directly."""
    spec = importlib.util.spec_from_file_location("dag_manager", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_schema() -> dict:
    """Parse references/dag-schema.json."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class DagSchemaSyncTest(unittest.TestCase):
    """Assert exact alignment between dag_manager.py constants and dag-schema.json."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dm = load_cli_module()
        cls.schema = load_schema()
        cls.defs = cls.schema["$defs"]

    def _verification_branches(self) -> dict:
        branches = {}
        for item in self.defs["verification"]["allOf"]:
            vtype = item["if"]["properties"]["type"]["const"]
            branches[vtype] = item["then"]
        return branches

    def test_schema_version_matches_pattern(self) -> None:
        pattern = self.schema["properties"]["schema_version"]["pattern"]
        self.assertRegex(self.dm.SCHEMA_VERSION, pattern)

    def test_root_enums_and_required(self) -> None:
        props = self.schema["properties"]
        self.assertEqual(self.dm.MODES, tuple(props["mode"]["enum"]))
        self.assertEqual(self.dm.PERSISTENCE_MODES, tuple(props["persistence"]["enum"]))
        self.assertEqual(self.dm.ROOT_REQUIRED, tuple(self.schema["required"]))

    def test_node_enums_and_required(self) -> None:
        node = self.defs["node"]
        props = node["properties"]
        self.assertEqual(self.dm.NODE_REQUIRED, tuple(node["required"]))
        self.assertEqual(self.dm.ACTION_TYPES, tuple(props["action_type"]["enum"]))
        self.assertEqual(self.dm.STATUSES, tuple(props["status"]["enum"]))
        self.assertEqual(
            self.dm.SIDE_EFFECT_SCOPES,
            tuple(props["side_effects"]["items"]["enum"]),
        )
        self.assertEqual(self.dm.RISK_TIERS, tuple(props["risk"]["enum"]))
        self.assertEqual(
            self.dm.RISK_TIERS,
            tuple(props["approved"]["properties"]["tier"]["enum"]),
        )
        self.assertEqual(self.dm.ID_RE.pattern, self.defs["nodeId"]["pattern"])

    def test_residue_enums(self) -> None:
        residue_props = self.defs["residueEntry"]["properties"]
        self.assertEqual(
            self.dm.SIDE_EFFECT_SCOPES,
            tuple(residue_props["scopes"]["items"]["enum"]),
        )
        self.assertEqual(self.dm.RESIDUE_REASONS, tuple(residue_props["reason"]["enum"]))

    def test_preflight_and_variants(self) -> None:
        requires_props = self.schema["properties"]["requires"]["properties"]
        self.assertEqual(set(self.dm.REQUIRES_FIELDS), set(requires_props.keys()))
        self.assertEqual(self.dm.PREFLIGHT_OS, tuple(requires_props["os"]["items"]["enum"]))

        tool_obj_schema = requires_props["tools"]["additionalProperties"]["oneOf"][1]
        self.assertEqual(
            set(self.dm.TOOL_SPEC_FIELDS),
            set(tool_obj_schema["properties"].keys()),
        )

        variants_props = self.defs["node"]["properties"]["variants"]["properties"]
        self.assertEqual(set(self.dm.PREFLIGHT_OS), set(variants_props.keys()))
        self.assertEqual(
            set(self.dm.VARIANT_FIELDS),
            set(self.defs["nodeVariant"]["properties"].keys()),
        )

    def test_service_and_interactive_fields(self) -> None:
        node_props = self.defs["node"]["properties"]
        service_props = node_props["service"]["properties"]
        self.assertEqual(set(self.dm.SERVICE_FIELDS), set(service_props.keys()))
        self.assertEqual(
            set(self.dm.SERVICE_SIGNALS.keys()),
            set(service_props["stop_signal"]["enum"]),
        )
        self.assertEqual(
            set(self.dm.INTERACTIVE_FIELDS),
            set(node_props["interactive"]["properties"].keys()),
        )

    def test_verification_types_required_and_allowed(self) -> None:
        verification = self.defs["verification"]
        self.assertEqual(
            self.dm.VERIFICATION_TYPES,
            tuple(verification["properties"]["type"]["enum"]),
        )
        branches = self._verification_branches()
        self.assertEqual(set(self.dm.VERIFICATION_TYPES), set(branches.keys()))
        self.assertEqual(set(self.dm.VERIFICATION_REQUIRED.keys()), set(branches.keys()))
        self.assertEqual(set(self.dm.VERIFICATION_ALLOWED.keys()), set(branches.keys()))
        for vtype, then_schema in branches.items():
            with self.subTest(verification_type=vtype):
                self.assertEqual(
                    set(self.dm.VERIFICATION_REQUIRED[vtype]),
                    set(then_schema["required"]),
                )
                self.assertEqual(
                    set(self.dm.VERIFICATION_ALLOWED[vtype]),
                    set(then_schema["properties"].keys()),
                )
