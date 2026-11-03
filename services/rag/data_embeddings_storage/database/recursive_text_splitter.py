class RecursiveCharacterTextSplitter:
    """Split text into chunks of at most chunk_size, cutting at natural boundaries.
    splits at paragraph breaks, line breaks, sentence ends, semicolons,
    commas, spaces. Overlap is made of whole pieces (sentences/words) from the
    end of the previous chunk, up to chunk_overlap characters, so a chunk never
    starts in the middle of a word.
    """

    DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", "; ", ", ", " "]

    def __init__(self, chunk_size=1024, chunk_overlap=30, length_function=len,
                 is_separator_regex=False, separators=None):
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.length_function = length_function
        self.is_separator_regex = is_separator_regex   # kept for compatibility; not used
        self.separators = separators or self.DEFAULT_SEPARATORS

    def split_text(self, text):
        if not text:
            return []
        pieces = self._split(text, self.separators)
        return self._merge(pieces)

    # Break text into pieces no longer than chunk_size.
    def _split(self, text, separators):
        if self.length_function(text) <= self.chunk_size:
            return [text]
        for index, separator in enumerate(separators):
            if separator not in text:
                continue
            parts = text.split(separator)
            # keep the separator on the end of each part so no text is lost
            parts = [part + separator for part in parts[:-1]] + [parts[-1]]
            pieces = []
            for part in parts:
                if not part:
                    continue
                if self.length_function(part) <= self.chunk_size:
                    pieces.append(part)
                else:
                    pieces.extend(self._split(part, separators[index + 1:]))
            return pieces
        # Last resort: one "word" longer than chunk_size.
        return [text[i:i + self.chunk_size] for i in range(0, len(text), self.chunk_size)]

    # Join pieces back into chunks up to chunk_size, with whole-piece overlap.
    def _merge(self, pieces):
        chunks, current, size = [], [], 0
        for piece in pieces:
            length = self.length_function(piece)
            if current and size + length > self.chunk_size:
                chunks.append("".join(current))
                overlap, overlap_size = [], 0
                for previous in reversed(current):
                    previous_length = self.length_function(previous)
                    if overlap_size + previous_length > self.chunk_overlap:
                        break
                    overlap.insert(0, previous)
                    overlap_size += previous_length
                current, size = overlap, overlap_size
                if size + length > self.chunk_size:      # overlap would not fit
                    current, size = [], 0
            current.append(piece)
            size += length
        if current:
            chunks.append("".join(current))
        return [chunk.strip() for chunk in chunks if chunk.strip()]
