"""Shared requirement verdict parsing and grading."""

from importlib import import_module

__all__ = [
    "format_source_with_page",
    "grade_requirement",
    "grade_requirements",
    "parse_agent_verdict",
]


def __getattr__(name):
    """Load grading helpers lazily so submodules import without the agent stack."""
    if name in __all__:
        return getattr(import_module(".verdicts", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted([*globals(), *__all__])
