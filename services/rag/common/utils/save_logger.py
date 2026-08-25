import json
import logging
from pathlib import Path
from typing import Any, Dict, List
 
 
logger = logging.getLogger(__name__)
 
 
def save_retrieval_log(stage: str, query: str, results: List[Dict[str, Any]], log_file: Path, max_results: int = 10,) -> None:
    """
    Save retrieval results to the provided JSONL file.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True,)
 
    try:
        with log_file.open(mode="a", encoding="utf-8",) as file:

            for rank, result in enumerate(results[:max_results], start=1,):
                metadata = result.get("metadata", {})

                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        metadata = {}

                if not isinstance(metadata, dict):
                    metadata = {}
                debug = result.get("debug", {}) 

                if not isinstance(debug, dict):
                    debug = {}

                record = {
                    "stage": stage,
                    "query": query,
                    "rank": rank,
                    "id": result.get("id"),
                    "document": metadata.get( "doc_id", metadata.get("source"), ),
                    "page": metadata.get("page", metadata.get("page_index"), ),
                    "text": result.get("text", ""),
                    "bm25_score": result.get("bm25_score", debug.get("bm25_score"), ),
                    "semantic_similarity": result.get("similarity", debug.get("semantic_similarity"), ),
                }
 
                file.write(json.dumps(record, ensure_ascii=False, default=str, ) + "\n")
 
    except OSError:
        logger.exception("Could not write retrieval log to %s", log_file,)
