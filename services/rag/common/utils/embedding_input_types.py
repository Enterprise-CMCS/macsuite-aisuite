"""Shared request construction for embedding model inputs."""

INPUT_TYPE_QUERY = "search_query"
INPUT_TYPE_DOCUMENT = "search_document"


def build_embedding_request(texts, input_type):
    if isinstance(texts, str):
        texts = [texts]

    return {"texts": texts, "input_type": input_type}
