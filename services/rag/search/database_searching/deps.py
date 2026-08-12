from common.utils import contract_config
from common.utils.contract_registry import resolve_contract
from common.utils.helper import Helper
from search.database_searching.agents import ChatDeps
from search.database_searching.search import SearchEngine


_ENGINE_CACHE = {}


def _resolve_table_name(contract_id: str | None) -> str:
    if contract_id is None:
        return Helper.get_embeddings_table_name()

    config = contract_config.load_config(contract_config.resolve_config_path())
    return resolve_contract(config, contract_id).embeddings_table_name


def get_search_engine(contract_id: str | None = None) -> SearchEngine:
    table_name = _resolve_table_name(contract_id)
    if table_name not in _ENGINE_CACHE:
        _ENGINE_CACHE[table_name] = SearchEngine(table_name=table_name)
    return _ENGINE_CACHE[table_name]


def build_chat_deps(
    contract_id: str | None = None,
    *,
    acronyms=None,
    timing=None,
) -> ChatDeps:
    return ChatDeps(
        acronyms=acronyms or {},
        timing=timing or {},
        search_engine=get_search_engine(contract_id),
    )
