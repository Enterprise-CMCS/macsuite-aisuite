import importlib
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_RAG_ROOT = Path(__file__).resolve().parents[1]
if str(_RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(_RAG_ROOT))

from common.utils.contract_config import (  # noqa: E402
    load_config,
    validate_embeddings_table_name,
)


class ContractRegistryTests(unittest.TestCase):
    _CONTRACTS = (
        ("me_0002", "embeddings_me_0002_nemt"),
        ("tn_6756", "embeddings_tn_6756_tenncare"),
        ("wa_6369", "embeddings_wa_6369_ifc"),
        ("wa_6472", "embeddings_wa_6472_ahimc"),
        ("wa_6473", "embeddings_wa_6473_ifc"),
    )

    def _write_ini(self, body: str) -> str:
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".ini",
            delete=False,
            encoding="utf-8",
        )
        handle.write(textwrap.dedent(body))
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))
        return handle.name

    def _five_contract_config(self, active_contract_ids=()):
        sections = ["[default]", "embeddings_table_name = embeddings", ""]
        active_contract_ids = set(active_contract_ids)
        for contract_id, table_name in self._CONTRACTS:
            sections.extend(
                [
                    f"[contract:{contract_id}]",
                    f"active = {'true' if contract_id in active_contract_ids else 'false'}",
                    f"embeddings_table_name = {table_name}",
                    "",
                ]
            )
        return load_config(self._write_ini("\n".join(sections)))

    @staticmethod
    def _registry():
        return importlib.import_module("common.utils.contract_registry")

    @staticmethod
    def _shipped_config():
        shipped = _RAG_ROOT / "common" / "utils" / "aws.properties.ini"
        return load_config(str(shipped))

    @staticmethod
    def _subprocess_env():
        env = os.environ.copy()
        for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            env.pop(name, None)
        return env

    def test_contract_registry_imports_in_fresh_process_without_aws_credentials(self):
        result = subprocess.run(
            ["python3", "-c", "import common.utils.contract_registry"],
            cwd=_RAG_ROOT,
            env=self._subprocess_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_contract_registry_import_does_not_load_side_effect_dependencies(self):
        script = textwrap.dedent(
            """
            import sys
            import common.utils.contract_registry

            forbidden = (
                "boto3",
                "common.utils.settings",
                "common.utils.logger",
                "structlog",
            )
            loaded = [name for name in forbidden if name in sys.modules]
            assert not loaded, f"unexpected imports: {loaded}"
            """
        )
        result = subprocess.run(
            ["python3", "-c", script],
            cwd=_RAG_ROOT,
            env=self._subprocess_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_list_contracts_on_shipped_ini_returns_five_refs_and_one_default(self):
        refs = self._registry().list_contracts(self._shipped_config())

        self.assertEqual(len(refs), 5)
        self.assertEqual(
            {ref.contract_id for ref in refs},
            {contract_id for contract_id, _ in self._CONTRACTS},
        )
        self.assertEqual(
            [ref.contract_id for ref in refs if ref.is_default],
            ["tn_6756"],
        )

    def test_list_contracts_does_not_require_resolvable_active_section(self):
        registry = self._registry()
        fixtures = (
            self._five_contract_config(("me_0002", "tn_6756")),
            self._five_contract_config(),
        )

        for config in fixtures:
            with self.subTest(active_defaults=sum(
                config.getboolean(section, "active")
                for section in config.sections()
                if section.startswith("contract:")
            )):
                refs = registry.list_contracts(config)
                self.assertEqual(len(refs), 5)
                self.assertLessEqual(sum(ref.is_default for ref in refs), 1)

    def test_resolve_contract_returns_requested_contract(self):
        ref = self._registry().resolve_contract(self._shipped_config(), "wa_6472")
        self.assertEqual(ref.embeddings_table_name, "embeddings_wa_6472_ahimc")

    def test_resolve_contract_rejects_non_contract_ids_as_unknown(self):
        registry = self._registry()
        self.assertFalse(issubclass(registry.UnknownContractError, KeyError))
        self.assertFalse(issubclass(registry.UnknownContractError, ValueError))

        for contract_id in ("nope", "contract:tn_6756", "embeddings"):
            with self.subTest(contract_id=contract_id):
                with self.assertRaises(registry.UnknownContractError):
                    registry.resolve_contract(self._shipped_config(), contract_id)

    def test_resolve_contract_rejects_injection_shaped_id_as_unknown(self):
        registry = self._registry()

        with self.assertRaises(registry.UnknownContractError):
            registry.resolve_contract(self._shipped_config(), "tn_6756;drop")

    def test_resolve_contract_none_returns_shipped_default(self):
        ref = self._registry().resolve_contract(self._shipped_config(), None)
        self.assertEqual(ref.contract_id, "tn_6756")
        self.assertTrue(ref.is_default)

    def test_resolve_contract_none_preserves_active_section_value_errors(self):
        registry = self._registry()
        fixtures = (
            self._five_contract_config(),
            self._five_contract_config(("me_0002", "tn_6756")),
        )

        for config in fixtures:
            with self.subTest():
                with self.assertRaises(ValueError) as ctx:
                    registry.resolve_contract(config, None)
                self.assertNotIsInstance(ctx.exception, registry.UnknownContractError)

    def test_listed_embeddings_table_names_are_validated(self):
        refs = self._registry().list_contracts(self._shipped_config())
        for ref in refs:
            with self.subTest(contract_id=ref.contract_id):
                self.assertEqual(
                    validate_embeddings_table_name(ref.embeddings_table_name),
                    ref.embeddings_table_name,
                )

    def test_contract_ref_exposes_required_fields(self):
        refs = self._registry().list_contracts(self._shipped_config())
        for ref in refs:
            with self.subTest(contract_id=ref.contract_id):
                self.assertTrue(hasattr(ref, "contract_id"))
                self.assertTrue(hasattr(ref, "embeddings_table_name"))
                self.assertTrue(hasattr(ref, "is_default"))


if __name__ == "__main__":
    unittest.main()
