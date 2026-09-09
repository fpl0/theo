"""Split literal message text within Telegram UTF-16 limits.

Preserves every character, favors paragraph and word boundaries, and never splits
a Unicode code point. Rendering and outbox state live outside this module.
"""


def split_text(text: str, limit: int = 4096) -> list[str]:
    # Plain text deliberately uses no parse_mode: literal HTML cannot break markup or inject links.
    if not text:
        raise ValueError("Text cannot be empty")
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for character in text:
        width = len(character.encode("utf-16-le")) // 2
        if width > limit:
            raise ValueError("Text limit cannot fit a single character")
        if units + width > limit:
            buffered = "".join(current)
            # Keep normal paragraphs/words intact when a useful boundary exists.
            # Retain every separator so concatenating chunks reproduces the input.
            boundary = buffered.rfind("\n") + 1
            if boundary < max(1, len(buffered) // 2):
                boundary = buffered.rfind(" ") + 1
            if boundary < max(1, len(buffered) // 2):
                boundary = len(buffered)
            chunks.append(buffered[:boundary])
            current = list(buffered[boundary:])
            units = len(buffered[boundary:].encode("utf-16-le")) // 2
        current.append(character)
        units += width
    if current:
        chunks.append("".join(current))
    return chunks
