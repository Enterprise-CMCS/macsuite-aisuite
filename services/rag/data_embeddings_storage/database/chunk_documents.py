CHUNKING_VERSION = "v2-recursive-cascade"


def split_documents(all_json_data, text_splitter, chunking_version=CHUNKING_VERSION):
    split_docs = []
    for doc in all_json_data:
        metadata = doc.get("metadata", {})
        element_type = metadata.get("element_type")
        if element_type == "TEXT":
            text_chunks = text_splitter.split_text(doc.get("text", ""))
            chunk_count = len(text_chunks)
            for idx, chunk in enumerate(text_chunks):
                split_docs.append(
                    {
                        "text": chunk,
                        "metadata": {
                            **metadata,
                            "chunk_index": idx,
                            "chunk_count": chunk_count,
                            "chunking_version": chunking_version,
                        },
                    }
                )
        else:
            split_docs.append(doc)

    return split_docs
