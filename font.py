#!/usr/bin/env python3
"""
font.py - a tiny 3x5 bitmap font for the 6x21 key matrix.

Glyphs are five rows of three pixels, "#" lit and "." dark; a few marks
(colon, period) are one pixel wide and space is two blank columns.
Shared by clock.py (digits on the board) and push.py (scrolling text).
"""

GLYPHS = {
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", ".##", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", ".#.", ".#.", ".#."),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
    "A": (".#.", "#.#", "###", "#.#", "#.#"),
    "B": ("##.", "#.#", "##.", "#.#", "##."),
    "C": (".##", "#..", "#..", "#..", ".##"),
    "D": ("##.", "#.#", "#.#", "#.#", "##."),
    "E": ("###", "#..", "##.", "#..", "###"),
    "F": ("###", "#..", "##.", "#..", "#.."),
    "G": (".##", "#..", "#.#", "#.#", ".##"),
    "H": ("#.#", "#.#", "###", "#.#", "#.#"),
    "I": ("###", ".#.", ".#.", ".#.", "###"),
    "J": ("..#", "..#", "..#", "#.#", ".#."),
    "K": ("#.#", "#.#", "##.", "#.#", "#.#"),
    "L": ("#..", "#..", "#..", "#..", "###"),
    "M": ("#.#", "###", "#.#", "#.#", "#.#"),
    "N": ("##.", "#.#", "#.#", "#.#", "#.#"),
    "O": (".#.", "#.#", "#.#", "#.#", ".#."),
    "P": ("##.", "#.#", "##.", "#..", "#.."),
    "Q": (".#.", "#.#", "#.#", ".#.", "..#"),
    "R": ("##.", "#.#", "##.", "#.#", "#.#"),
    "S": (".##", "#..", ".#.", "..#", "##."),
    "T": ("###", ".#.", ".#.", ".#.", ".#."),
    "U": ("#.#", "#.#", "#.#", "#.#", "###"),
    "V": ("#.#", "#.#", "#.#", "#.#", ".#."),
    "W": ("#.#", "#.#", "#.#", "###", "#.#"),
    "X": ("#.#", "#.#", ".#.", "#.#", "#.#"),
    "Y": ("#.#", "#.#", ".#.", ".#.", ".#."),
    "Z": ("###", "..#", ".#.", "#..", "###"),
    ":": (".", "#", ".", "#", "."),
    ".": (".", ".", ".", ".", "#"),
    "-": ("...", "...", "###", "...", "..."),
    " ": ("..", "..", "..", "..", ".."),
}

HEIGHT = 5
assert all(len(rows) == HEIGHT for rows in GLYPHS.values())


def columns_of(text):
    """Render text as a list of 5-bit columns (top row first), one blank
    column between glyphs. Unknown characters become spaces."""
    columns = []
    for index, char in enumerate(text.upper()):
        glyph = GLYPHS.get(char, GLYPHS[" "])
        if index:
            columns.append((0, 0, 0, 0, 0))
        for x in range(len(glyph[0])):
            columns.append(tuple(1 if glyph[y][x] == "#" else 0
                                 for y in range(HEIGHT)))
    return columns


def width_of(text):
    """Width of the rendered text in columns, without building it."""
    return sum(len(GLYPHS.get(c, GLYPHS[" "])[0]) for c in text.upper()) \
        + max(0, len(text) - 1)
