"""Pure scoring functions for labelled requirement evaluations."""

import math
from collections.abc import Iterable, Mapping

from eval.labels import HUMAN_LABELS, TOOL_LABELS, MET, NOT_MET, UNCLEAR, ERROR


def wilson_interval(
    successes: int, total: int, z: float = 1.959964
) -> tuple[float, float] | None:
    """Return a Wilson score interval, or None when there is no sample."""
    if total == 0:
        return None

    proportion = successes / total
    z_squared = z * z
    denominator = 1 + z_squared / total
    center = (proportion + z_squared / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / total
            + z_squared / (4 * total * total)
        )
        / denominator
    )
    return center - margin, center + margin


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def score(
    ground_truth: Iterable[Mapping], predictions: Iterable[Mapping]
) -> dict:
    """Score predictions joined to ground truth by requirement_id."""
    ground_truth = list(ground_truth)
    predictions = list(predictions)
    ground_truth_ids = {row.get("requirement_id") for row in ground_truth}
    predictions_by_id = {
        row.get("requirement_id"): row
        for row in predictions
        if row.get("requirement_id") is not None
    }

    confusion = {
        human_label: {tool_label: 0 for tool_label in TOOL_LABELS}
        for human_label in HUMAN_LABELS
    }
    scored_pairs: list[tuple[str, str]] = []

    for row in ground_truth:
        human_label = row.get("human_label")
        prediction = predictions_by_id.get(row.get("requirement_id"))
        if human_label not in HUMAN_LABELS or prediction is None:
            continue
        tool_label = prediction.get("tool_label")
        if tool_label not in TOOL_LABELS:
            raise ValueError(f"Invalid tool label: {tool_label}")
        scored_pairs.append((human_label, tool_label))
        confusion[human_label][tool_label] += 1

    decided_pairs = [
        pair for pair in scored_pairs if pair[0] in (MET, NOT_MET)
    ]
    not_met_pairs = [pair for pair in scored_pairs if pair[0] == NOT_MET]
    agreements = sum(human == tool for human, tool in scored_pairs)
    decided_agreements = sum(human == tool for human, tool in decided_pairs)

    n_ground_truth = len(ground_truth)
    n_scored = len(scored_pairs)
    n_decided = len(decided_pairs)
    return {
        "n_ground_truth": n_ground_truth,
        "n_scored": n_scored,
        "n_decided": n_decided,
        "n_unmatched_predictions": sum(
            row.get("requirement_id") not in ground_truth_ids for row in predictions
        ),
        "coverage": _rate(n_scored, n_ground_truth),
        "agreement_rate": _rate(agreements, n_scored),
        "agreement_ci95": wilson_interval(agreements, n_scored),
        "decided_agreement_rate": _rate(decided_agreements, n_decided),
        "unclear_on_decided_rate": _rate(
            sum(tool == UNCLEAR for _, tool in decided_pairs), n_decided
        ),
        "false_met_rate": _rate(
            sum(tool == MET for _, tool in not_met_pairs), len(not_met_pairs)
        ),
        "error_rate": _rate(
            sum(tool == ERROR for _, tool in scored_pairs), n_scored
        ),
        "confusion": confusion,
    }
