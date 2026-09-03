import os
import structlog
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from search.database_searching.agents import answer_question, build_deps

logger = structlog.get_logger(__name__)
_deps = None

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
            {"path": "/health", "methods": ["GET"], "description": "Health check"},
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

    global _deps
    try:
        if _deps is None:
            _deps = build_deps()

        answer = await answer_question(query, deps=_deps)

        logger.info("agent_success", query=query[:100], length=len(answer))
        return AgentResponse(query=query, response=answer, success=True)

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
