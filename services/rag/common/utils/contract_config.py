"""Per-contract aws.properties.ini resolution without AWS/logging deps."""

import configparser
import os
import re

_CONTRACT_SECTION_PREFIX = "contract:"
_EMBEDDINGS_TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def resolve_config_path(config_file="aws.properties.ini", *, ai_prop_file=None, utils_dir=None):
    if ai_prop_file is None:
        ai_prop_file = os.getenv("AIPropFile")
    if ai_prop_file:
        config_file = ai_prop_file.strip()
    if os.path.isabs(config_file):
        return config_file
    base = utils_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, config_file)


def load_config(config_path):
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file '{config_path}' not found.")
    config = configparser.ConfigParser()
    config.read(config_path)
    return config


def contract_sections(config):
    return [
        section
        for section in config.sections()
        if section.startswith(_CONTRACT_SECTION_PREFIX)
    ]


def resolve_active_contract_section(config):
    sections = contract_sections(config)
    if not sections:
        return None

    active_sections = [
        section
        for section in sections
        if config.getboolean(section, "active", fallback=False)
    ]
    if len(active_sections) == 0:
        raise ValueError(
            "No active contract section: set active = true on exactly one "
            f"[contract:…] section (found: {sections})."
        )
    if len(active_sections) > 1:
        raise ValueError(
            "Multiple active contract sections "
            f"{active_sections}; only one may have active = true."
        )
    return active_sections[0]


def validate_embeddings_table_name(table_name):
    if not table_name or not _EMBEDDINGS_TABLE_NAME_RE.fullmatch(table_name):
        raise ValueError(
            f"Invalid embeddings_table_name '{table_name}': "
            "must match ^[a-z][a-z0-9_]*$."
        )
    return table_name


def get_config_property(config, str_property_name, *, default=None, raise_if_missing=True):
    active_section = resolve_active_contract_section(config)
    if active_section is not None and config.has_option(active_section, str_property_name):
        return config.get(active_section, str_property_name)
    if config.has_option("default", str_property_name):
        return config.get("default", str_property_name)
    if not raise_if_missing:
        return default
    raise KeyError(str_property_name)


def list_embeddings_table_names_from_config(config):
    names = []
    for section in contract_sections(config):
        if not config.has_option(section, "embeddings_table_name"):
            continue
        name = validate_embeddings_table_name(
            config.get(section, "embeddings_table_name").strip()
        )
        if name not in names:
            names.append(name)
    if not names:
        default_name = get_config_property(
            config,
            "embeddings_table_name",
            default="embeddings",
            raise_if_missing=False,
        ) or "embeddings"
        names.append(validate_embeddings_table_name(default_name.strip()))
    return names
