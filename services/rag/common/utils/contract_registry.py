"""Import-safe registry for configured RAG contracts."""

from dataclasses import dataclass

from common.utils import contract_config


class UnknownContractError(Exception):
    """Raised when a requested contract ID is not configured."""


@dataclass(frozen=True)
class ContractRef:
    contract_id: str
    embeddings_table_name: str
    is_default: bool


def _contract_ref(config, section, active_section):
    if not config.has_option(section, "embeddings_table_name"):
        raise ValueError(
            f"Contract section [{section}] is missing embeddings_table_name."
        )
    table_name = contract_config.validate_embeddings_table_name(
        config.get(section, "embeddings_table_name").strip()
    )
    return ContractRef(
        contract_id=section.removeprefix("contract:"),
        embeddings_table_name=table_name,
        is_default=section == active_section,
    )


def list_contracts(config):
    try:
        active_section = contract_config.resolve_active_contract_section(config)
    except ValueError:
        active_section = None

    return [
        _contract_ref(config, section, active_section)
        for section in contract_config.contract_sections(config)
    ]


def resolve_contract(config, contract_id):
    if contract_id is None:
        active_section = contract_config.resolve_active_contract_section(config)
        if active_section is None:
            raise ValueError("No contract sections are configured.")
        return _contract_ref(config, active_section, active_section)

    contracts = list_contracts(config)
    for contract in contracts:
        if contract.contract_id == contract_id:
            return contract

    valid_ids = ", ".join(contract.contract_id for contract in contracts)
    raise UnknownContractError(
        f"Unknown contract_id '{contract_id}'. Valid contract ids: {valid_ids}."
    )
