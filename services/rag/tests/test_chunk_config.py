import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("structlog", MagicMock())

_RAG_ROOT = Path(__file__).resolve().parents[1]
if str(_RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(_RAG_ROOT))

from common.utils.helper import Helper  # noqa: E402


class ChunkConfigTests(unittest.TestCase):
    def _write_ini(self, default_values: str) -> str:
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".ini",
            delete=False,
            encoding="utf-8",
        )
        handle.write(
            "[default]\n"
            f"{textwrap.dedent(default_values).strip()}\n\n"
            "[contract:x]\n"
            "active = true\n"
        )
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))
        return handle.name

    def _set_env(self, name: str, value: str | None) -> None:
        previous = os.environ.get(name)

        def restore() -> None:
            os.environ.pop(name, None)
            if previous is not None:
                os.environ[name] = previous

        self.addCleanup(restore)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def setUp(self):
        self._set_env("AIPropFile", None)

    def test_default_chunk_values_are_positive_integers(self):
        self._set_env("CHUNK_SIZE", None)
        self._set_env("CHUNK_OVERLAP", None)
        path = self._write_ini(
            """
            chunk_size = 1024
            chunk_overlap = 150
            """
        )

        self.assertEqual(
            Helper.get_positive_int_property("chunk_size", config_file=path),
            1024,
        )
        self.assertEqual(
            Helper.get_positive_int_property("chunk_overlap", config_file=path),
            150,
        )

    def test_environment_values_override_ini_values(self):
        path = self._write_ini(
            """
            chunk_size = 1024
            chunk_overlap = 150
            """
        )
        self._set_env("CHUNK_SIZE", "2048")
        self._set_env("CHUNK_OVERLAP", "200")

        self.assertEqual(
            Helper.get_positive_int_property("chunk_size", config_file=path),
            2048,
        )
        self.assertEqual(
            Helper.get_positive_int_property("chunk_overlap", config_file=path),
            200,
        )

    def test_invalid_chunk_values_raise(self):
        self._set_env("CHUNK_SIZE", None)
        self._set_env("CHUNK_OVERLAP", None)

        for value in ("not-a-number", "0", "-1"):
            with self.subTest(value=value):
                path = self._write_ini(f"chunk_size = {value}")
                with self.assertRaisesRegex(ValueError, "positive integer|greater than zero"):
                    Helper.get_positive_int_property("chunk_size", config_file=path)

    def test_missing_chunk_value_raises(self):
        self._set_env("CHUNK_SIZE", None)
        path = self._write_ini("other_property = value")

        with self.assertRaisesRegex(ValueError, "required"):
            Helper.get_positive_int_property("chunk_size", config_file=path)

    def test_chunk_environment_override_mapping(self):
        self.assertEqual(
            Helper.ENV_PROPERTY_OVERRIDES["chunk_size"],
            ("CHUNK_SIZE",),
        )
        self.assertEqual(
            Helper.ENV_PROPERTY_OVERRIDES["chunk_overlap"],
            ("CHUNK_OVERLAP",),
        )


if __name__ == "__main__":
    unittest.main()
