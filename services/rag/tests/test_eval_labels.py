"""Tests for dependency-light evaluation labels and requirement identifiers."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))


class EvalImportIsolationTests(unittest.TestCase):
    def test_labels_and_dataset_import_without_aws_or_runtime_dependencies(self):
        forbidden = [
            "search.database_searching.agents",
            "search.requirements.verdicts",
            "pandas",
            "boto3",
            "asyncpg",
            "pydantic_ai",
        ]
        script = (
            "import json, sys\n"
            "import eval.labels, eval.dataset\n"
            f"print(json.dumps([name for name in {forbidden!r} if name in sys.modules]))\n"
        )
        env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=RAG_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])


class EvalLabelTests(unittest.TestCase):
    def test_normalize_label_accepts_canonical_and_spaced_values(self):
        from eval.labels import normalize_label

        self.assertEqual(normalize_label("not met"), "NOT_MET")
        self.assertEqual(normalize_label("  Not Met "), "NOT_MET")
        self.assertEqual(normalize_label("MET"), "MET")
        self.assertIsNone(normalize_label(""))

    def test_unknown_label_names_the_rejected_value(self):
        from eval.labels import UnknownLabelError, normalize_label

        with self.assertRaises(UnknownLabelError) as context:
            normalize_label("Deficient")
        self.assertIn("Deficient", str(context.exception))

    def test_requirement_id_is_normalized_lowercase_hex(self):
        from eval.dataset import requirement_id

        first = requirement_id("  Lorem   Requirement ALPHA. ")
        second = requirement_id("lorem requirement alpha.")

        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
