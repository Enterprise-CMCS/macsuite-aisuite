"""Utilities for combining ranked search results."""


def reciprocal_rank_fusion(*ranked_lists, k=60):
    """Fuse ranked document lists using reciprocal rank fusion."""
    scores = {}
    documents = {}
    first_seen = {}

    for ranked_list in ranked_lists:
        for rank, document in enumerate(ranked_list, start=1):
            document_id = document["id"]
            if document_id not in documents:
                documents[document_id] = document
                first_seen[document_id] = len(first_seen)
                scores[document_id] = 0.0
            scores[document_id] += 1 / (k + rank)

    ordered_ids = sorted(
        documents,
        key=lambda document_id: (-scores[document_id], first_seen[document_id]),
    )
    return [documents[document_id] for document_id in ordered_ids]
