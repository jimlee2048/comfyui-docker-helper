"""Control-safe text helpers shared by CLI presentation boundaries."""


def control_safe_text(
    value: str,
    *,
    escape_backslashes: bool = True,
) -> str:
    """Escape non-printing characters and, by default, printable backslashes."""
    escaped: list[str] = []
    for character in value:
        if character == "\\" and escape_backslashes:
            escaped.append("\\\\")
            continue
        if character.isprintable():
            escaped.append(character)
            continue
        codepoint = ord(character)
        if character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif codepoint <= 0xFF:
            escaped.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(f"\\U{codepoint:08x}")
    return "".join(escaped)
