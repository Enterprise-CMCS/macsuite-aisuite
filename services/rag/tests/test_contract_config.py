import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_RAG_ROOT = Path(__file__).resolve().parents[1]
if str(_RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(_RAG_ROOT))

from common.utils.contract_config import (  # noqa: E402
    get_config_property,
    list_embeddings_table_names_from_config,
    load_config,
    resolve_active_contract_section,
    resolve_config_path,
    validate_embeddings_table_name,
)
from data_embeddings_storage.database.embeddings_schema import (  # noqa: E402
    create_embeddings_table_sql,
    embeddings_index_statements,
    expected_index_names,
)


class ActiveContractResolutionTests(unittest.TestCase):
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

    def test_active_contract_overlays_prefixes_and_table(self):
        path = self._write_ini(
            """
            [default]
            input_bucket_name = aisuite-dev-contract-rag
            input_prefix =
            embeddings_table_name = embeddings
            code_bucket_name = aisuite-dev-llm-pipeline-code

            [contract:me_0002]
            active = false
            input_prefix = state_of_ME/MCR-ME-0002-NEMT/
            embeddings_table_name = embeddings_me_0002_nemt

            [contract:tn_6756]
            active = true
            input_prefix = state_of_TN/MCCRS-TN-6756-TennCare/
            embeddings_table_name = embeddings_tn_6756_tenncare
            """
        )
        config = load_config(path)
        self.assertEqual(
            get_config_property(config, "input_prefix"),
            "state_of_TN/MCCRS-TN-6756-TennCare/",
        )
        self.assertEqual(
            get_config_property(config, "embeddings_table_name"),
            "embeddings_tn_6756_tenncare",
        )
        self.assertEqual(
            get_config_property(config, "code_bucket_name"),
            "aisuite-dev-llm-pipeline-code",
        )
        self.assertEqual(
            list_embeddings_table_names_from_config(config),
            ["embeddings_me_0002_nemt", "embeddings_tn_6756_tenncare"],
        )

    def test_zero_active_contracts_raises(self):
        path = self._write_ini(
            """
            [default]
            input_prefix =

            [contract:me_0002]
            active = false
            input_prefix = state_of_ME/MCR-ME-0002-NEMT/
            embeddings_table_name = embeddings_me_0002_nemt
            """
        )
        with self.assertRaises(ValueError) as ctx:
            get_config_property(load_config(path), "input_prefix")
        self.assertIn("No active contract", str(ctx.exception))

    def test_multiple_active_contracts_raises(self):
        path = self._write_ini(
            """
            [default]
            input_prefix =

            [contract:me_0002]
            active = true
            input_prefix = state_of_ME/MCR-ME-0002-NEMT/
            embeddings_table_name = embeddings_me_0002_nemt

            [contract:tn_6756]
            active = true
            input_prefix = state_of_TN/MCCRS-TN-6756-TennCare/
            embeddings_table_name = embeddings_tn_6756_tenncare
            """
        )
        with self.assertRaises(ValueError) as ctx:
            get_config_property(load_config(path), "input_prefix")
        self.assertIn("Multiple active", str(ctx.exception))

    def test_no_contract_sections_uses_default(self):
        path = self._write_ini(
            """
            [default]
            input_prefix = legacy/
            embeddings_table_name = embeddings
            """
        )
        config = load_config(path)
        self.assertEqual(get_config_property(config, "input_prefix"), "legacy/")
        self.assertEqual(get_config_property(config, "embeddings_table_name"), "embeddings")
        self.assertIsNone(resolve_active_contract_section(config))

    def test_shipped_dev_ini_defaults_to_tn_6756(self):
        shipped = _RAG_ROOT / "common" / "utils" / "aws.properties.ini"
        config = load_config(str(shipped))
        self.assertEqual(
            get_config_property(config, "input_prefix"),
            "state_of_TN/MCCRS-TN-6756-TennCare/",
        )
        self.assertEqual(
            get_config_property(config, "embeddings_table_name"),
            "embeddings_tn_6756_tenncare",
        )
        self.assertEqual(len(list_embeddings_table_names_from_config(config)), 5)
        self.assertEqual(resolve_active_contract_section(config), "contract:tn_6756")

    def test_absolute_config_path_passthrough(self):
        path = self._write_ini(
            """
            [default]
            input_prefix = x/
            """
        )
        self.assertEqual(resolve_config_path(path), path)

    def test_invalid_table_name_rejected(self):
        with self.assertRaises(ValueError):
            validate_embeddings_table_name("embeddings;drop table")
        with self.assertRaises(ValueError):
            validate_embeddings_table_name("EmbeddingsBad")

    def test_create_sql_uses_validated_table_name(self):
        sql = create_embeddings_table_sql("embeddings_tn_6756_tenncare")
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS aisuite_schema.embeddings_tn_6756_tenncare",
            sql,
        )
        self.assertIn("VECTOR(1536)", sql)

    def test_index_sql_is_schema_qualified(self):
        statements = embeddings_index_statements("embeddings_tn_6756_tenncare")
        self.assertTrue(statements)
        for statement in statements:
            self.assertIn("ON aisuite_schema.embeddings_tn_6756_tenncare", statement)

    def test_index_names_match_statements(self):
        table = "embeddings_tn_6756_tenncare"
        names = expected_index_names(table)
        statements = embeddings_index_statements(table)
        self.assertEqual(len(names), len(statements))
        for name, statement in zip(names, statements):
            self.assertIn(name, statement)


if __name__ == "__main__":
    unittest.main()
