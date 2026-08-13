import os
import sys
from collections import Counter
from pathlib import Path
from typing import Optional, Union

import strawberry
import structlog
from cross_web import HTTPException as GraphQLHTTPException
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from strawberry.fastapi import GraphQLRouter

# Add src directory to Python path
src_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(src_dir))

from common.utils import contract_config  # noqa: E402
from common.utils.contract_registry import (  # noqa: E402
    UnknownContractError,
    list_contracts,
    resolve_contract,
)
from search.database_searching.agents import search_agent  # noqa: E402
from search.database_searching.deps import build_chat_deps  # noqa: E402
from search.requirements import verdicts as requirements_verdicts  # noqa: E402
from search.routes import security  # noqa: E402

logger = structlog.get_logger(__name__)

DEFAULT_MAX_BATCH_SIZE = 25
try:
    MAX_BATCH_SIZE = int(
        os.environ.get("REQUIREMENTS_MAX_BATCH_SIZE", DEFAULT_MAX_BATCH_SIZE)
    )
    if MAX_BATCH_SIZE < 1:
        MAX_BATCH_SIZE = DEFAULT_MAX_BATCH_SIZE
except (TypeError, ValueError):
    MAX_BATCH_SIZE = DEFAULT_MAX_BATCH_SIZE


def _load_contract_config():
    """Load the active aws.properties.ini contract definitions."""
    return contract_config.load_config(contract_config.resolve_config_path())


# GraphQL schema types
@strawberry.type
class SearchResponse:
    """Search response for GraphQL API."""
    search_type: str
    response: str
    strategy: str


@strawberry.type
class QueryResponse:
    """Query response containing all search strategies."""
    query: str
    contract_id: str
    semantic: SearchResponse
    hybrid: Optional[SearchResponse] = None
    reranked: Optional[SearchResponse] = None


@strawberry.type
class Query:
    """GraphQL query root."""
    @strawberry.field
    async def hello(self) -> str:
        return "Hello from RAG GraphQL API!"


@strawberry.type
class Mutation:
    """GraphQL mutation root."""
    @strawberry.mutation
    async def process_query(self, query: str, contract_id: Optional[str] = None) -> QueryResponse:
        agent_response = await process_agent_query(query, contract_id)
        return QueryResponse(
            query=agent_response.query,
            contract_id=agent_response.contract_id,
            semantic=SearchResponse(
                search_type="agent",
                response=agent_response.response,
                strategy="agent",
            ),
        )


schema = strawberry.Schema(query=Query, mutation=Mutation)


# Pydantic models for REST API
class AgentRequest(BaseModel):
    """Request model for agent endpoint."""
    query: str = Field(..., min_length=1, max_length=2000, description="User query to process")
    contract_id: Optional[str] = Field(
        default=None,
        description="Contract to scope the search to (defaults to the active contract)"
    )


class AgentResponse(BaseModel):
    """Response model for agent endpoint."""
    query: str = Field(..., description="Original query")
    response: str = Field(..., description="Agent-generated response")
    success: bool = Field(default=True, description="Whether request was successful")
    contract_id: str = Field(..., description="Contract the query was scoped to")


class RequirementItem(BaseModel):
    """One requirement to grade."""
    text: str = Field(..., min_length=1, max_length=2000)
    id: Optional[str] = None


class RequirementsRequest(BaseModel):
    """Batch requirement grading request."""
    requirements: list[RequirementItem] = Field(..., min_length=1)
    retry_unclear: bool = True


class RequirementVerdict(BaseModel):
    """One graded requirement result."""
    id: Union[str, int]
    success: bool
    error: Optional[str] = None
    Requirement: str
    Recommendation: str
    Response: str
    Source: str
    Page: Union[str, int]


class RequirementsSummary(BaseModel):
    """Aggregate counts for a requirements batch."""
    total: int
    succeeded: int
    failed: int
    by_recommendation: dict[str, int]


class RequirementsResponse(BaseModel):
    """Batch requirement grading response."""
    results: list[RequirementVerdict]
    summary: RequirementsSummary


# FastAPI application
app = FastAPI(
    title="Agentic RAG API",
    description="Production RAG system with pydantic-ai agent, AWS Bedrock NOVA Pro, and multi-strategy search",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Require an API key on every route except the health check."""
    status_code, message = security.check_request(
        request.url.path,
        request.headers.get("x-api-key"),
    )
    if status_code is not None:
        return JSONResponse(status_code=status_code, content={"detail": message})
    return await call_next(request)


# Added last so CORS wraps the auth middleware and answers preflight OPTIONS.
app.add_middleware(CORSMiddleware, **security.build_cors_kwargs())


class _AgentGraphQLRouter(GraphQLRouter):
    async def process_result(self, request, result):
        for error in result.errors or []:
            original = getattr(error, "original_error", None)
            if isinstance(original, HTTPException):
                raise GraphQLHTTPException(
                    original.status_code, str(original.detail)
                ) from original
        return await super().process_result(request, result)


graphql_app = _AgentGraphQLRouter(schema)
app.include_router(graphql_app, prefix="/query")


@app.get("/", tags=["Info"])
async def root():
    """API information and available endpoints."""
    return {
        "service": "Agentic RAG API",
        "version": "2.0.0",
        "status": "operational",
        "model": os.environ.get('BEDROCK_MODEL_ID', 'us.amazon.nova-pro-v1:0'),
        "endpoints": [
            {"path": "/agent", "methods": ["GET", "POST"], "description": "AI agent endpoint"},
            {"path": "/requirements", "methods": ["POST"], "description": "Batch requirement grading endpoint"},
            {"path": "/contracts", "methods": ["GET"], "description": "Available contract ids"},
            {"path": "/health", "methods": ["GET"], "description": "Health check"},
            {"path": "/query", "methods": ["GET", "POST"], "description": "GraphQL API"},
            {"path": "/docs", "methods": ["GET"], "description": "Interactive API docs"}
        ],
        "usage": {
            "agent_get": "GET /agent?query=your+question&contract_id=tn_6756", #answer from question
            "agent_post": "POST /agent with body {\"query\": \"your question\", \"contract_id\": \"tn_6756\"}" #input question from user
        }
    }


@app.get("/contracts", tags=["Contracts"])
async def list_available_contracts():
    """Contract ids clients may scope a query to."""
    contracts = list_contracts(_load_contract_config())
    return {
        "contracts": [
            {"contract_id": contract.contract_id, "is_default": contract.is_default}
            for contract in contracts
        ]
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """Service health status."""
    return {"status": "healthy", "service": "agentic-rag-api", "version": "2.0.0"}


@app.post(
    "/requirements",
    response_model=RequirementsResponse,
    tags=["Requirements"],
)
async def requirements_post(request: RequirementsRequest):
    """Grade a batch of requirements."""
    if len(request.requirements) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many requirements (max {MAX_BATCH_SIZE})",
        )

    items = [
        {
            "text": item.text,
            **({"id": item.id} if item.id is not None else {}),
        }
        for item in request.requirements
    ]
    results = await requirements_verdicts.grade_requirements(
        items,
        retry_unclear=request.retry_unclear,
    )
    succeeded = sum(result.get("success") is True for result in results)

    return {
        "results": results,
        "summary": {
            "total": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "by_recommendation": dict(
                Counter(result["Recommendation"] for result in results)
            ),
        },
    }


async def process_agent_query(query: str, contract_id: Optional[str] = None) -> AgentResponse:
    """Process agent query (shared by GET and POST endpoints)."""
    try:
        resolved_contract_id = resolve_contract(
            _load_contract_config(), contract_id
        ).contract_id
    except UnknownContractError as exc:
        logger.warning("unknown_contract", contract_id=(contract_id or "")[:100])
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    logger.info("agent_request", query=query[:100], contract_id=resolved_contract_id)

    try:
        deps = build_chat_deps(resolved_contract_id)
        result = await search_agent.run(query, deps=deps)

        logger.info(
            "agent_success",
            query=query[:100],
            contract_id=resolved_contract_id,
            length=len(result.output),
        )
        return AgentResponse(
            query=query,
            response=result.output,
            success=True,
            contract_id=resolved_contract_id,
        )

    except Exception as e:
        logger.error(
            "agent_error",
            error=str(e),
            query=query[:100],
            contract_id=resolved_contract_id,
            exc_info=True,
        )
        return AgentResponse(
            query=query,
            response=f"Error: {str(e)}. Please try rephrasing or contact support.",
            success=False,
            contract_id=resolved_contract_id,
        )


@app.post("/agent", response_model=AgentResponse, tags=["Agent"]) #where user asks Q, endpoints to hit
async def agent_post(request: AgentRequest):
    """AI agent endpoint (POST with JSON body)."""
    return await process_agent_query(request.query, request.contract_id)


@app.get("/agent", response_model=AgentResponse, tags=["Agent"]) #where response lands, endpoints to hit
async def agent_get(query: str = "", contract_id: Optional[str] = None):
    if not query.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Query required. Example: /agent?query=What is RAG?")
    if len(query) > 2000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Query too long (max 2000 characters)")
    return await process_agent_query(query, contract_id)


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8001"))
    reload = os.environ.get("API_RELOAD", "false").lower() == "true"

    logger.info("starting_server", host=host, port=port, reload=reload)
    uvicorn.run(app, host=host, port=port, reload=reload, log_level="info")
