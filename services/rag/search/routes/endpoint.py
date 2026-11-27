import os

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from search.routes import manifest_api

logger = structlog.get_logger(__name__)


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
manifest_api.install(app)


@app.get("/", tags=["Info"])
async def root():
    """API information and available endpoints."""
    return {
        "service": "Agentic RAG API",
        "version": "2.0.0",
        "status": "operational",
        "model": os.environ.get('BEDROCK_MODEL_ID', 'us.amazon.nova-2-lite-v1:0'),
        "endpoints": [
            {"path": "/manifest", "methods": ["POST"], "description": "Validate a MuleSoft manifest and start the review workflow"},
            {"path": "/manifest/{requestKey}", "methods": ["GET"], "description": "Manifest workflow status"},
            {"path": "/manifest/{requestKey}/results", "methods": ["GET"], "description": "Reviewed legal requirements"},
            {"path": "/health", "methods": ["GET"], "description": "Health check"},
            {"path": "/docs", "methods": ["GET"], "description": "Interactive API docs"}
        ]
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """Service health status."""
    return {"status": "healthy", "service": "agentic-rag-api", "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8001"))
    reload = os.environ.get("API_RELOAD", "false").lower() == "true"

    logger.info("starting_server", host=host, port=port, reload=reload)
    uvicorn.run(app, host=host, port=port, reload=reload, log_level="info")
