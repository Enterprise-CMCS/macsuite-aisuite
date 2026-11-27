class RecursiveCharacterTextSplitter:
    def __init__(self, chunk_size=1024, chunk_overlap=30, length_function=len, is_separator_regex=False):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.length_function = length_function
        self.is_separator_regex = is_separator_regex

    def split_text(self, text):
        """
        Recursively split text into chunks of chunk_size with chunk_overlap.
        """
        if not text:
            return []
        chunks = []
        start = 0
        text_length = self.length_function(text)
        while start < text_length:
            end = min(start + self.chunk_size, text_length)
            chunk = text[start:end]
            chunks.append(chunk)
            if end == text_length:
                break
            start = end - self.chunk_overlap if self.chunk_overlap > 0 else end
            if start < 0 or start >= text_length:
                break
        return chunks
