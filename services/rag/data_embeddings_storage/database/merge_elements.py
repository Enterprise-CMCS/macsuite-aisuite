def merge_lead_in_lists(docs):
    """Join a paragraph that ends with ':' to the list element that follows it.
    BDA returns a lead-in sentence and its list as two separate elements.
    """
    out = []
    for doc in docs:
        meta = doc.get("metadata", {})
        if out and meta.get("element_type") == "TEXT" and meta.get("subtype") == "list":
            prev = out[-1]
            prev_meta = prev.get("metadata", {})
            if (prev_meta.get("element_type") == "TEXT"
                    and prev_meta.get("subtype") == "paragraph"
                    and prev.get("text", "").rstrip().rstrip("*_ ").endswith(":")
                    and prev_meta.get("doc_id") == meta.get("doc_id")
                    and prev_meta.get("page") == meta.get("page")):
                text = doc.get("text", "")
                prev_lines = prev["text"].rstrip().split("\n")
                # BDA repeats the section header on every element
                header = prev_lines[0] if len(prev_lines) > 1 else None
                if header and text.startswith(header + "\n"):
                    text = text[len(header) + 1:]
                # sometimes repeats the lead-in sentence at the top of the list
                lead_in = prev_lines[-1].replace("*", "").strip()
                first_line, _, rest = text.partition("\n")
                if first_line.replace("*", "").strip() == lead_in:
                    text = rest
                out[-1] = {"text": prev["text"].rstrip() + "\n" + text,
                           "metadata": {**prev_meta, "subtype": "paragraph+list"}}
                continue
        out.append(doc)
    return out
