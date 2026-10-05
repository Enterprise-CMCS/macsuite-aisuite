import asyncio
import json

import numpy as np
from data_embeddings_storage.database.connection import get_connection, release_connection
from search.database_searching.aws_embedding_client import BedrockEmbeddingClient
from search.database_searching.reranker import CohereReranker
from common.utils.helper import Helper
from common.utils.logger import log

RRF_K = 60


MIN_EF_SEARCH = 64

# Offsets a source table's row ids apart from every other source's in a
# federated search, so two tables' unrelated rows sharing a serial id (both
# tables start counting from 1) don't collide as the same dict key in
# agents.record_chunks(). The id is never sent back to Postgres, so a
# synthetic value is fine.
_FEDERATED_ID_OFFSET = 1_000_000_000


def _parse_metadata(metadata):
    """asyncpg hands JSONB back as text unless a codec is registered."""
    if isinstance(metadata, str):
        try:
            return json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return {}
    return metadata if isinstance(metadata, dict) else {}


class SearchEngine:
    def __init__(self, table_name=None):
        self.bedrock_client = BedrockEmbeddingClient()
        self.reranker = CohereReranker()
        self.table_name = table_name or Helper.get_embeddings_table_name()

    async def embed_query(self, query_text):
        embedding = await self.bedrock_client.get_embedding(query_text, input_type="search_query")

        if isinstance(embedding, list):
            return np.array(embedding).flatten()
        return np.array(embedding.get('float', [])).flatten()

    async def fulltext_search(self, query: str, limit: int = 30):
        connection = await get_connection()
        try:
            results = await connection.fetch(f"""
                SELECT
                    id,
                    text,
                    metadata,
                    ts_rank_cd(search_tsv, query, 32) AS rank
                FROM {self.table_name},
                     to_tsquery('english',NULLIF(replace(plainto_tsquery('english', $1)::text,' & ',' | '),'')) AS query
                WHERE search_tsv @@ query
                ORDER BY rank DESC
                LIMIT $2
            """, query, limit)

            return [dict(row) for row in results] if results else []
        finally:
            await release_connection(connection)

    async def semantic_search(self, query_text, limit=100):
        embedding_flattened = await self.embed_query(query_text)

        connection = await get_connection()
        try:
            results = await connection.fetch(f"""
                SELECT
                    id,
                    text,
                    metadata,
                    embedding <=> $1::vector AS distance
                FROM {self.table_name}
                ORDER BY embedding <=> $1::vector
                LIMIT $2
            """, embedding_flattened, limit)

            return [dict(row) for row in results] if results else []
        finally:
            await release_connection(connection)

    async def hybrid_search(self, query_text, limit=12, dense_limit=60, lexical_limit=60):
        embedding_flattened = await self.embed_query(query_text)
        ef_search = max(MIN_EF_SEARCH, dense_limit)

        connection = await get_connection()
        try:
            async with connection.transaction():
                await connection.execute(f"SET LOCAL hnsw.ef_search = {int(ef_search)}")

                results = await connection.fetch(f"""
                    WITH dense AS (
                        SELECT
                            id,
                            text,
                            metadata,
                            embedding <=> $1::vector AS distance,
                            ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS position
                        FROM {self.table_name}
                        ORDER BY embedding <=> $1::vector
                        LIMIT $3
                    ),
                    lexical AS (
                        SELECT
                            id,
                            text,
                            metadata,
                            -- Scored here as well so a chunk the vector arm never
                            -- shortlisted still carries a cosine distance. Otherwise
                            -- its retrieval confidence lands in the workbook blank.
                            embedding <=> $1::vector AS distance,
                            ts_rank_cd(search_tsv, query, 32) AS lexical_rank,
                            ROW_NUMBER() OVER (ORDER BY ts_rank_cd(search_tsv, query, 32) DESC) AS position
                        FROM {self.table_name},
                             to_tsquery('english',NULLIF(replace(plainto_tsquery('english', $2)::text,' & ',' | '),'')) AS query
                        WHERE search_tsv @@ query
                        ORDER BY lexical_rank DESC
                        LIMIT $4
                    )
                    SELECT
                        COALESCE(d.id, l.id) AS id,
                        COALESCE(d.text, l.text) AS text,
                        COALESCE(d.metadata, l.metadata) AS metadata,
                        COALESCE(d.distance, l.distance) AS distance,
                        CASE WHEN COALESCE(d.distance, l.distance) IS NOT NULL
                             THEN ROUND(GREATEST(0, LEAST(1, 1 - COALESCE(d.distance, l.distance)))::numeric, 4)::float8
                        END AS retrieval_confidence,
                        l.lexical_rank,
                        d.position AS dense_position,
                        l.position AS lexical_position,
                        (COALESCE(1.0 / ($5 + d.position), 0)
                            + COALESCE(1.0 / ($5 + l.position), 0))::float8 AS fused_score
                    FROM dense d
                    FULL OUTER JOIN lexical l ON d.id = l.id
                    ORDER BY fused_score DESC
                    LIMIT $6
                """, embedding_flattened, query_text, dense_limit, lexical_limit, RRF_K, limit)

            return [dict(row, metadata=_parse_metadata(row["metadata"])) for row in results] if results else []
        finally:
            await release_connection(connection)

    async def reranked_search(self, query_text, limit=8, candidate_limit=40):
        candidates = await self.hybrid_search(query_text, limit=candidate_limit)
        if not candidates:
            return []
        try:
            reranked = await self.reranker.rerank_results(query_text, candidates, top_k=limit)
        except Exception as lclEx:
            Helper.print_exception("SearchEngine.reranked_search", lclEx,
                                   "Cohere rerank failed, falling back to the fused hybrid order.")
            return candidates[:limit]

        log.debug(f"reranked_search() reranked {len(candidates)} candidates down to {len(reranked)}")
        return reranked


class FederatedSearchEngine:
    """Fans hybrid_search out across several embeddings tables and merges the results.

    Used to consult the shared regulatory knowledge base (42 CFR Part 438, the CMS
    State Guide) alongside whichever contract is active, since each contract's
    table only ever holds that contract's own document. Only hybrid_search is
    federated - it's the only method agents.retrieve() calls.

    The primary (first) source keeps its full `limit` - exactly what a plain
    SearchEngine would have returned - and every other source adds at most
    `reference_limit` rows on top. Merging into one shared `limit` instead would
    have had the reference table's passages crowd the contract's own out, since
    both tables' rank-based fused_scores land on the same scale; a requirement
    whose wording tracks the regulation closely could come back with most of its
    slots spent on the regulation and too little contract text to decide on.

    Every merged row is tagged with the label its source table was given, so a
    caller (agents.format_evidence, agents.check_citations) can tell a passage
    of federal regulation or CMS guidance apart from the contract itself - the
    two must never be treated as the same kind of evidence.

    The first source is the primary one and its failure is raised, the same as a
    plain SearchEngine. Any other source failing - most likely its table not
    existing yet in an environment bootstrap hasn't been re-run in - is logged and
    skipped, so a missing reference table degrades search to contract-only rather
    than failing every review.
    """

    def __init__(self, sources, reference_limit=4):
        """sources: an iterable of (table_name, label) pairs, primary first.
        reference_limit: the most rows each non-primary source adds per search."""
        self.sources = [(SearchEngine(table_name=name), label) for name, label in sources]
        self.reference_limit = reference_limit

    async def hybrid_search(self, query_text, limit=12, dense_limit=60, lexical_limit=60):
        per_table = await asyncio.gather(*(
            engine.hybrid_search(query_text, limit=limit if offset == 0 else self.reference_limit,
                                 dense_limit=dense_limit, lexical_limit=lexical_limit)
            for offset, (engine, _label) in enumerate(self.sources)
        ), return_exceptions=True)

        merged = []
        for offset, ((engine, label), rows) in enumerate(zip(self.sources, per_table)):
            if isinstance(rows, BaseException):
                if offset == 0 or not isinstance(rows, Exception):
                    raise rows
                Helper.print_exception("FederatedSearchEngine.hybrid_search", rows,
                                       f"Search of '{label}' table {engine.table_name} failed, "
                                       "continuing without it.")
                continue
            for row in rows:
                if row.get("id") is not None:
                    row["id"] += offset * _FEDERATED_ID_OFFSET
                row["source"] = label
                merged.append(row)

        # Not trimmed back to `limit` - see the class docstring. The order only
        # matters within a source, since agents.format_evidence groups by source.
        merged.sort(key=lambda row: row.get("fused_score") or 0, reverse=True)
        return merged
