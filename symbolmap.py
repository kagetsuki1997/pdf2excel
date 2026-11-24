"""Windows 'Symbol' font characters -> Unicode.

Word and other tools store characters from the Symbol font as private-use code points
U+F020..U+F0FF (the Symbol encoding byte + 0xF000). Left alone they show as blanks in Excel.
"""

_GREEK_UPPER = "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ"      # 0x41..0x5A
_GREEK_LOWER = "αβχδεφγηιϕκλμνοπθρστυϖωξψζ"      # 0x61..0x7A

_SYMBOL = {
    0x22: "∀", 0x24: "∃", 0x27: "∋", 0x2A: "∗", 0x2D: "−", 0x40: "≅",
    0x5C: "∴", 0x5E: "⊥", 0x60: "¯", 0x7E: "∼",
    0xA0: "€", 0xA1: "ϒ", 0xA2: "′", 0xA3: "≤", 0xA4: "⁄", 0xA5: "∞", 0xA6: "ƒ", 0xA7: "♣",
    0xA8: "♦", 0xA9: "♥", 0xAA: "♠", 0xAB: "↔", 0xAC: "←", 0xAD: "↑", 0xAE: "→", 0xAF: "↓",
    0xB0: "°", 0xB1: "±", 0xB2: "″", 0xB3: "≥", 0xB4: "×", 0xB5: "∝", 0xB6: "∂", 0xB7: "•",
    0xB8: "÷", 0xB9: "≠", 0xBA: "≡", 0xBB: "≈", 0xBC: "…", 0xBD: "⏐", 0xBE: "⎯", 0xBF: "↵",
    0xC0: "ℵ", 0xC1: "ℑ", 0xC2: "ℜ", 0xC3: "℘", 0xC4: "⊗", 0xC5: "⊕", 0xC6: "∅", 0xC7: "∩",
    0xC8: "∪", 0xC9: "⊃", 0xCA: "⊇", 0xCB: "⊄", 0xCC: "⊂", 0xCD: "⊆", 0xCE: "∈", 0xCF: "∉",
    0xD0: "∠", 0xD1: "∇", 0xD2: "®", 0xD3: "©", 0xD4: "™", 0xD5: "∏", 0xD6: "√", 0xD7: "⋅",
    0xD8: "¬", 0xD9: "∧", 0xDA: "∨", 0xDB: "⇔", 0xDC: "⇐", 0xDD: "⇑", 0xDE: "⇒", 0xDF: "⇓",
    0xE0: "◊", 0xE1: "〈", 0xE5: "∑", 0xF1: "〉", 0xF2: "∫",
}
for _i, _ch in enumerate(_GREEK_UPPER):
    _SYMBOL[0x41 + _i] = _ch
for _i, _ch in enumerate(_GREEK_LOWER):
    _SYMBOL[0x61 + _i] = _ch
# plain ASCII characters keep their own value (digits, + = < > ( ) [ ] { } | etc.)
for _code in list(range(0x20, 0x7F)):
    _SYMBOL.setdefault(_code, chr(_code))
# keep the choices used so far: micro sign for the Greek mu (units like uA), Greek capital omega for ohm
_SYMBOL[0x6D] = "µ"

SYMBOL_MAP = {0xF000 + code: ch for code, ch in _SYMBOL.items()}


def fix_char(text):
    """Map a single character from the Symbol font's private-use range to Unicode."""
    return SYMBOL_MAP.get(ord(text), text) if len(text) == 1 else text
