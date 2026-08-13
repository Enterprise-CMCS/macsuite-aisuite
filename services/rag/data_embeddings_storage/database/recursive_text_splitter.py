import re

DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", "; ", " ", ""]


class RecursiveCharacterTextSplitter:
    """Split text on a cascade of separators, coarsest first.

    Separators stay attached to the piece they terminate, so joining the
    pieces of any level reproduces the input exactly. Pieces are merged
    greedily up to chunk_size, and each new chunk keeps a tail of the
    previous one so consecutive chunks share a real substring.
    """

    def __init__(
        self,
        chunk_size=1024,
        chunk_overlap=150,
        length_function=len,
        is_separator_regex=False,
        separators=None,
    ):
        if chunk_overlap >= chunk_size:
            raise ValueError(
                f"chunk_overlap ({chunk_overlap}) must be smaller than chunk_size ({chunk_size})"
            )
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.length_function = length_function
        self.is_separator_regex = is_separator_regex
        self.separators = list(separators) if separators else list(DEFAULT_SEPARATORS)

    def split_text(self, text):
        """Return the chunks of text, in order, without dropping characters."""
        if not text or not text.strip():
            return []
        if self.length_function(text) <= self.chunk_size:
            return [text]
        return self._split(text, self.separators)

    def _split(self, text, separators):
        return self._merge(self._explode(text, separators))

    def _explode(self, text, separators):
        """Return pieces that each fit in chunk_size and cover text in order."""
        separator, finer_separators = self._select_separator(text, separators)
        if separator == "":
            return self._hard_split(text)

        pieces = []
        for piece in self._split_keeping_separator(text, separator):
            if self.length_function(piece) <= self.chunk_size:
                pieces.append(piece)
            else:
                pieces.extend(self._explode(piece, finer_separators))
        return pieces

    def _select_separator(self, text, separators):
        """Return the coarsest separator present in text and the finer rest."""
        for index, separator in enumerate(separators):
            if separator == "":
                break
            if re.search(self._pattern(separator), text):
                return separator, separators[index + 1:]
        return "", []

    def _pattern(self, separator):
        return separator if self.is_separator_regex else re.escape(separator)

    def _split_keeping_separator(self, text, separator):
        """Cut text after every separator, so the pieces still spell out text."""
        pieces = []
        start = 0
        for match in re.finditer(self._pattern(separator), text):
            if match.end() > start:
                pieces.append(text[start:match.end()])
                start = match.end()
        if start < len(text):
            pieces.append(text[start:])
        return pieces

    def _merge(self, pieces):
        """Greedily pack pieces into chunks, carrying an overlap tail forward."""
        chunks = []
        window = []
        total = 0
        for piece in pieces:
            length = self.length_function(piece)
            if window and total + length > self.chunk_size:
                chunk = "".join(window)
                chunks.append(chunk)
                window, total = self._carry_overlap(chunk, window, total, length)
            window.append(piece)
            total += length
        if window:
            chunks.append("".join(window))
        return chunks

    def _carry_overlap(self, chunk, window, total, next_length):
        """Reduce an emitted window to the overlap the next chunk starts from.

        Whole pieces are kept when they fit, so overlaps land on separator
        boundaries; otherwise the tail characters of the emitted chunk are
        carried instead, which still leaves a shared substring.
        """
        while window and total - self.length_function(window[0]) >= self.chunk_overlap:
            total -= self.length_function(window.pop(0))
        if total + next_length <= self.chunk_size:
            return window, total

        carry = min(self.chunk_overlap, self.chunk_size - next_length)
        if carry <= 0:
            return [], 0
        tail = chunk[-carry:]
        return [tail], self.length_function(tail)

    def _hard_split(self, text):
        """Slice a run with no usable separator into pieces _merge can overlap."""
        step = self.chunk_size - self.chunk_overlap
        return [text[start:start + step] for start in range(0, len(text), step)]
