from data_embeddings_storage.database.connection import initialization_db, close_db, get_connection, release_connection
import asyncio
from common.utils.settings import EMBEDDING_DIMENSION


async def create_embedding_table():
    
    await initialization_db()
    connect = await get_connection()

    try:

        await connect.execute(f"""
        CREATE TABLE IF NOT EXISTS embeddings (
            id SERIAL PRIMARY KEY,
            text TEXT NOT NULL,
            metadata JSONB DEFAULT NULL,
            embedding VECTOR({EMBEDDING_DIMENSION}) NOT NULL,
            search_tsv tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('english', coalesce(text, '')), 'A') || 
                setweight(to_tsvector('english', coalesce(metadata::text, '')), 'B')
            ) STORED,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        print("Embeddings table created successfully.")

        await connect.execute("""
                CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw 
                ON embeddings
                USING hnsw (embedding vector_cosine_ops) 
                WITH (m=16, ef_construction= 128);
        """)    
        print("HNSW index created successfully.")

        await connect.execute("""
            CREATE INDEX IF NOT EXISTS idx_embeddings_metadata
            ON embeddings USING GIN (metadata);
        """)
        print("Metadata index created successfully.")

        await connect.execute("""
            CREATE INDEX IF NOT EXISTS idx_search_text_trgm
            ON embeddings USING GIN (text gin_trgm_ops);
        """)
        print("Fuzzy index created successfully.")

        await connect.execute("""
            CREATE INDEX IF NOT EXISTS idx_search_tsv
            ON embeddings USING GIN (search_tsv);
        """)
        print("Full-text search index created successfully.")

    finally:
        await release_connection(connect)
        await close_db()


   

if __name__ == "__main__":
    asyncio.run(create_embedding_table())