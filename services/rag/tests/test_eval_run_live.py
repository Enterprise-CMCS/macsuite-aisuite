"""Tests for the opt-in HTTP evaluation runner."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

FIXTURES = RAG_ROOT / "tests" / "fixtures" / "eval"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _RecordingHttp:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse({"results": self.results})

    def client_type(self):
        recorder = self

        class RecordingClient:
            def __init__(self, **kwargs):
                self.default_headers = kwargs.get("headers", {})

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def close(self):
                return None

            def post(self, url, **kwargs):
                headers = dict(self.default_headers)
                headers.update(kwargs.pop("headers", {}))
                if headers:
                    kwargs["headers"] = headers
                return recorder.post(url, **kwargs)

        return RecordingClient


class EvalRunLiveTests(unittest.TestCase):
    def _args(self, output):
        return [
            "--ground-truth",
            str(FIXTURES / "ground_truth.jsonl"),
            "--output",
            str(output),
            "--api-url",
            "https://api.example/base",
            "--model-id",
            "us.amazon.nova-pro-v1:0",
            "--prompt-version",
            "hybrid-search-v1",
            "--contract-id",
            "tn_6756",
            "--run-id",
            "stub-run",
        ]

    def _results(self):
        return [
            {
                "id": json.loads(line)["requirement_id"],
                "success": True,
                "error": None,
                "Requirement": json.loads(line)["requirement"],
                "Recommendation": "NOT MET" if index == 1 else "MET",
                "Response": "Lorem response.",
                "Source": "Lorem: 1",
                "Page": "1",
            }
            for index, line in enumerate(
                (FIXTURES / "ground_truth.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )
        ]

    def test_live_gate_fails_without_network_when_environment_is_unset(self):
        from eval import run_live

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "must-not-be-written.jsonl"
            messages = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(run_live.httpx, "Client", create=True) as client,
                patch.object(run_live.httpx, "post", create=True) as post,
                redirect_stdout(messages),
                redirect_stderr(messages),
            ):
                result = run_live.main(self._args(output))

            self.assertNotEqual(result, 0)
            self.assertIn("AISUITE_EVAL_LIVE", messages.getvalue())
            client.assert_not_called()
            post.assert_not_called()
            self.assertFalse(output.exists())

    def test_import_is_offline_and_does_not_load_agent_or_verdict_modules(self):
        script = (
            "import json, sys\n"
            "import eval.run_live\n"
            "names = ['search.database_searching.agents', "
            "'search.requirements.verdicts']\n"
            "print(json.dumps([name for name in names if name in sys.modules]))\n"
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

    def test_stubbed_http_writes_prediction_shape_and_api_key_header(self):
        from eval import run_live
        from eval.dataset import read_jsonl

        recorder = _RecordingHttp(self._results())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "predictions.jsonl"
            with (
                patch.dict(
                    os.environ,
                    {
                        "AISUITE_EVAL_LIVE": "1",
                        "AISUITE_EVAL_API_KEY": "test-api-key",
                    },
                    clear=True,
                ),
                patch.object(
                    run_live.httpx,
                    "Client",
                    recorder.client_type(),
                    create=True,
                ),
                patch.object(run_live.httpx, "post", recorder.post, create=True),
            ):
                result = run_live.main(self._args(output))

            self.assertEqual(result, 0)
            records = read_jsonl(output)
            self.assertEqual(len(records), 10)
            expected_keys = {
                "requirement_id",
                "tool_label",
                "model_id",
                "retry_unclear",
                "prompt_version",
                "contract_id",
                "run_id",
                "timestamp",
            }
            self.assertEqual(set(records[0]), expected_keys)
            self.assertEqual(records[1]["tool_label"], "NOT_MET")
            self.assertEqual(records[0]["model_id"], "us.amazon.nova-pro-v1:0")
            self.assertIs(records[0]["retry_unclear"], True)
            self.assertEqual(records[0]["prompt_version"], "hybrid-search-v1")
            self.assertEqual(records[0]["contract_id"], "tn_6756")
            self.assertEqual(records[0]["run_id"], "stub-run")
            self.assertTrue(records[0]["timestamp"].endswith("Z"))
            parsed = datetime.fromisoformat(records[0]["timestamp"].replace("Z", "+00:00"))
            self.assertEqual(parsed.tzinfo, timezone.utc)

            self.assertEqual(len(recorder.calls), 1)
            url, kwargs = recorder.calls[0]
            self.assertEqual(url, "https://api.example/base/requirements")
            self.assertEqual(kwargs["headers"]["x-api-key"], "test-api-key")

    def test_api_key_header_is_omitted_when_environment_is_unset(self):
        from eval import run_live

        recorder = _RecordingHttp(self._results())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "predictions.jsonl"
            with (
                patch.dict(os.environ, {"AISUITE_EVAL_LIVE": "1"}, clear=True),
                patch.object(
                    run_live.httpx,
                    "Client",
                    recorder.client_type(),
                    create=True,
                ),
                patch.object(run_live.httpx, "post", recorder.post, create=True),
            ):
                result = run_live.main(self._args(output))

            self.assertEqual(result, 0)
            headers = recorder.calls[0][1].get("headers", {})
            self.assertNotIn("x-api-key", {key.lower() for key in headers})


if __name__ == "__main__":
    unittest.main()
