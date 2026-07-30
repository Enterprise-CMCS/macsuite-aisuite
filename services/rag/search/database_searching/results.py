from search.database_searching.search import SearchEngine


async def get_search_results(query: str):

    search = SearchEngine()

    semantic_results = await search.semantic_search(query)

    def extract_text(results):

        texts = [result.get('text', '') for result in results if result.get('text')]
        return "\n\n".join(texts)

    return {
        "semantic": extract_text(semantic_results),
    }
