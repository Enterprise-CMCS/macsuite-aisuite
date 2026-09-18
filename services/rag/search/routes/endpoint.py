import os
import time
from typing import Optional

import structlog
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from common.utils.contract_config import validate_embeddings_table_name
from common.utils.helper import Helper
from data_embeddings_storage.database.connection import get_connection, release_connection
from search.database_searching.agents import answer_question_formatted, build_deps

logger = structlog.get_logger(__name__)
# Keyed by embeddings table name, so one running process can answer against
# several submissions' knowledge bases without a redeploy - see
# AgentRequest.contract. process_agent_query() always resolves to a concrete
# table name (never None) before this cache is touched.
_deps_by_table = {}
# Cached at first use. The INI only declares a handful of [contract:...]
# sections, but bootstrap pre-creates (and older runs have left behind) many
# more embeddings_* tables than that - the database itself, not the INI, is
# the authoritative set of contracts a request is allowed to select.
_known_table_names = None

class AgentRequest(BaseModel):
    """Request model for agent endpoint."""
    query: str = Field(..., min_length=1, max_length=2000, description="User query to process")
    contract: Optional[str] = Field(
        default=None,
        description="Embeddings table to search, e.g. 'embeddings_ne_1_1'. "
                    "Omit to use the INI's active contract.",
    )

class AgentResponse(BaseModel):
    """Response model for agent endpoint."""
    query: str = Field(..., description="Original query")
    response: str = Field(..., description="Agent-generated response")
    contract: str = Field(..., description="Embeddings table that actually answered this query.")
    processing_time: float = Field(..., description="Time taken to process the request, in seconds")
    success: bool = Field(default=True, description="Whether request was successful")


# FastAPI application
app = FastAPI(
    title="Agentic RAG API",
    description="Production RAG system with pydantic-ai agent, AWS Bedrock NOVA Lite, and multi-strategy search",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["Info"])
async def root():
    """API information and available endpoints."""
    return {
        "service": "Agentic RAG API",
        "version": "2.0.0",
        "status": "operational",
        "model": os.environ.get('BEDROCK_MODEL_ID', 'amazon.nova-pro-v1:0'),
        "endpoints": [
            {"path": "/agent", "methods": ["GET", "POST"], "description": "AI agent endpoint"},
            {"path": "/contracts", "methods": ["GET"], "description": "Configured embeddings tables and the default"},
            {"path": "/health", "methods": ["GET"], "description": "Health check"},
            {"path": "/docs", "methods": ["GET"], "description": "Interactive API docs"}
        ],
        "usage": {
            "agent_get": "GET /agent?query=your+question&contract=embeddings_table_name", #answer from question
            "agent_post": "POST /agent with body {\"query\": \"your question\", \"contract\": \"embeddings_table_name\"}", #input question from user
            "contract": "Recommended on every request - the embeddings table to search, e.g. 'embeddings_ne_1_1'. "
                        "See GET /contracts for the valid values. Omitting it falls back to the INI's active "
                        "contract, which can change out from under you on the next redeploy."
        }
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """Service health status."""
    return {"status": "healthy", "service": "agentic-rag-api", "version": "2.0.0"}


async def _valid_table_names():
    global _known_table_names
    if _known_table_names is None:
        connection = await get_connection()
        try:
            rows = await connection.fetch(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'aisuite_schema' AND tablename LIKE 'embeddings%';"
            )
        finally:
            await release_connection(connection)
        _known_table_names = {row["tablename"] for row in rows}
    return _known_table_names


@app.get("/contracts", tags=["Info"])
async def list_contracts():
    """Embeddings tables that actually exist in the database, and which one
    answers when a request omits `contract`. Callers should prefer passing
    `contract` explicitly rather than relying on this default - see
    AgentRequest.contract."""
    return {
        "contracts": sorted(await _valid_table_names()),
        "default": Helper.get_embeddings_table_name(),
    }


async def process_agent_query(query: str, contract: Optional[str] = None) -> AgentResponse:
    """Process agent query (shared by GET and POST endpoints)."""
    logger.info("agent_request", query=query[:100], contract=contract)

    table_name = None
    if contract:
        try:
            table_name = validate_embeddings_table_name(contract.strip())
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    start = time.perf_counter()
    try:
        if table_name is None:
            # No contract given - resolve now what SearchEngine would otherwise
            # resolve internally, so the response can say which table actually
            # answered instead of leaving that implicit.
            table_name = Helper.get_embeddings_table_name()
        elif table_name not in await _valid_table_names():
            # Inside try: a DB hiccup while checking membership (not "unknown
            # contract" itself, which still raises 400 below) should land in
            # the same graceful AgentResponse(success=False) path every other
            # failure in this function uses, not bypass it as a raw 500.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Unknown contract '{table_name}'. No embeddings table by that name exists.",
            )

        deps = _deps_by_table.get(table_name)
        if deps is None:
            deps = build_deps(table_name=table_name)
            _deps_by_table[table_name] = deps

        answer = await answer_question_formatted(query, deps=deps)
        elapsed = time.perf_counter() - start

        logger.info("agent_success", query=query[:100], contract=table_name,
                    length=len(answer), processing_time=elapsed)
        return AgentResponse(query=query, response=answer, contract=table_name,
                              processing_time=elapsed, success=True)

    except HTTPException:
        raise

    except ImportError as e:
        error_msg = f"Module import failed: {str(e)}"
        logger.error("import_error", error=error_msg, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Service configuration error: {error_msg}"
        )

    except Exception as e:
        elapsed = time.perf_counter() - start
        logger.error("agent_error", error=str(e), query=query[:100], exc_info=True)
        return AgentResponse(
            query=query,
            response=f"Error: {str(e)}. Please try rephrasing or contact support.",
            contract=table_name or contract or "unknown",
            processing_time=elapsed,
            success=False
        )


@app.post("/agent", response_model=AgentResponse, tags=["Agent"]) #where user asks Q, endpoints to hit
async def agent_post(request: AgentRequest):
    """AI agent endpoint (POST with JSON body)."""
    return await process_agent_query(request.query, contract=request.contract)


@app.get("/agent", response_model=AgentResponse, tags=["Agent"]) #where response lands, endpoints to hit
async def agent_get(query: str = "", contract: Optional[str] = None):
    if not query.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Query required. Example: /agent?query=What is RAG?")
    if len(query) > 2000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Query too long (max 2000 characters)")
    return await process_agent_query(query, contract=contract)


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8001"))
    reload = os.environ.get("API_RELOAD", "false").lower() == "true"

    logger.info("starting_server", host=host, port=port, reload=reload)
    uvicorn.run(app, host=host, port=port, reload=reload, log_level="info")
