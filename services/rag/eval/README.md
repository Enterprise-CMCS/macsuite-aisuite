# RAG evaluation

Run these commands from `services/rag`. Evaluation artifacts belong in the
gitignored `eval/data/` directory.

First, extract human verdicts from the completed CRT:

```sh
python -m eval.extract_crt \
  --input /path/to/completed-crt.xlsx \
  --output eval/data/ground-truth.jsonl
```

Do this before running `search.excel_process.process_excel_with_rag`.
`ExcelRAGProcessor.load_excel` blanks the `Recommendation`, `RAG Response`, and
`Source` columns (`process_excel_with_rag.py` lines 72–94), which removes the
human verdicts from its in-memory workbook data.

Next, explicitly enable a live run and call the requirements API:

```sh
export AISUITE_EVAL_LIVE=1
# Set only when the API requires it:
export AISUITE_EVAL_API_KEY=your-api-key

python -m eval.run_live \
  --ground-truth eval/data/ground-truth.jsonl \
  --output eval/data/predictions.jsonl \
  --api-url http://127.0.0.1:8001 \
  --model-id us.amazon.nova-pro-v1:0 \
  --prompt-version hybrid-search-v1 \
  --contract-id tn_6756 \
  --run-id local-001
```

`retry_unclear` defaults to `true` and is recorded in every prediction. Pass
`--no-retry-unclear` to disable the API's second attempt for `UNCLEAR` results.

Finally, create machine-readable and short human-readable reports:

```sh
python -m eval.score \
  --ground-truth eval/data/ground-truth.jsonl \
  --predictions eval/data/predictions.jsonl \
  --json-out eval/data/report.json \
  --md-out eval/data/report.md
```

Add `--fail-under-agreement 0.9` to return exit code 1 when a defined agreement
rate is below 90%.
