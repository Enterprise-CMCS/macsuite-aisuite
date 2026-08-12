from search.database_searching.deps import get_search_engine


async def get_search_results(query: str, contract_id: str | None = None):

    search = get_search_engine(contract_id)

    semantic_results = await search.semantic_search(query)

    def extract_text(results):

        texts = [result.get('text', '') for result in results if result.get('text')]
        return "\n\n".join(texts)

    return {
        "semantic": extract_text(semantic_results),
    }
