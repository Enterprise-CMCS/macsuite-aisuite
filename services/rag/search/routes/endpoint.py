import os
import sys
import json
from pathlib import Path
from typing import Optional

import strawberry
import structlog
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from strawberry.fastapi import GraphQLRouter

# Add src directory to Python path
src_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(src_dir))

logger = structlog.get_logger(__name__)


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
    async def process_query(self, query: str) -> QueryResponse:
        from search.foundational_model.foundational_llm_model import process_query_with_foundational_model

        results = await process_query_with_foundational_model(query)

        return QueryResponse(
            query=results["query"],
            semantic=SearchResponse(
                search_type=results["semantic"]["search_type"],
                response=results["semantic"]["response"],
                strategy=results["semantic"]["strategy"]
            ),
        )


schema = strawberry.Schema(query=Query, mutation=Mutation)


# Pydantic models for REST API
class AgentRequest(BaseModel):
    """Request model for agent endpoint."""
    query: str = Field(..., min_length=1, max_length=2000, description="User query to process")


class AgentResponse(BaseModel):
    """Response model for agent endpoint."""
    query: str = Field(..., description="Original query")
    response: str = Field(..., description="Agent-generated response")
    success: bool = Field(default=True, description="Whether request was successful")


# FastAPI application
app = FastAPI(
    title="Agentic RAG API",
    description="Production RAG system with pydantic-ai agent, AWS Bedrock NOVA Pro, and multi-strategy search",
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

graphql_app = GraphQLRouter(schema)
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
            {"path": "/health", "methods": ["GET"], "description": "Health check"},
            {"path": "/query", "methods": ["GET", "POST"], "description": "GraphQL API"},
            {"path": "/docs", "methods": ["GET"], "description": "Interactive API docs"}
        ],
        "usage": {
            "agent_get": "GET /agent?query=your+question", #answer from question
            "agent_post": "POST /agent with body {\"query\": \"your question\"}" #input question from user
        }
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """Service health status."""
    return {"status": "healthy", "service": "agentic-rag-api", "version": "2.0.0"}


async def process_agent_query(query: str) -> AgentResponse:
    """Process agent query (shared by GET and POST endpoints)."""
    logger.info("agent_request", query=query[:100])

    try:
        from search.database_searching.agents import search_agent, ChatDeps
        from search.database_searching.search import SearchEngine

        deps = ChatDeps(acronyms={}, timing={}, search_engine=SearchEngine())
        result = await search_agent.run(query, deps=deps)

        logger.info("agent_success", query=query[:100], length=len(result.output))
        return AgentResponse(query=query, response=result.output, success=True)

    except ImportError as e:
        error_msg = f"Module import failed: {str(e)}"
        logger.error("import_error", error=error_msg, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Service configuration error: {error_msg}"
        )

    except Exception as e:
        logger.error("agent_error", error=str(e), query=query[:100], exc_info=True)
        return AgentResponse(
            query=query,
            response=f"Error: {str(e)}. Please try rephrasing or contact support.",
            success=False
        )


@app.post("/agent", response_model=AgentResponse, tags=["Agent"]) #where user asks Q, endpoints to hit
async def agent_post(request: AgentRequest):
    """AI agent endpoint (POST with JSON body)."""
    return await process_agent_query(request.query)


@app.get("/agent", response_model=AgentResponse, tags=["Agent"]) #where response lands, endpoints to hit
async def agent_get(query: str = ""):
    if not query.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Query required. Example: /agent?query=What is RAG?")
    if len(query) > 2000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Query too long (max 2000 characters)")
    return await process_agent_query(query)


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8001"))
    reload = os.environ.get("API_RELOAD", "false").lower() == "true"

    logger.info("starting_server", host=host, port=port, reload=reload)
    uvicorn.run(app, host=host, port=port, reload=reload, log_level="info")
