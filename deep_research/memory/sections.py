"""Lossless, deterministic section windows for full-report memory indexing.

A section is an exact slice of the source string; joining the windows recovers the
original report byte-for-byte after UTF-8 encoding. Splits favor Markdown headers,
then paragraph boundaries, then whitespace, with a bounded hard fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_SECTION_CHARS = 2400


@dataclass(frozen=True)
class ReportSection:
    index: int
    text: str
    title: str
    start: int
    end: int


def split_report_sections(text: str, max_chars: int = MAX_SECTION_CHARS) -> list[ReportSection]:
    if not text:
        return []
    if max_chars < 128:
        raise ValueError("max_chars must be at least 128")
    sections: list[ReportSection] = []
    cursor = 0
    last_title = "正文"
    length = len(text)
    while cursor < length:
        ceiling = min(cursor + max_chars, length)
        end = ceiling
        if ceiling < length:
            segment = text[cursor:ceiling]
            minimum = max(1, max_chars // 2)
            # Prefer not to break immediately before/inside a section heading.
            boundaries = [m.start() + 1 for m in re.finditer(r"\n(?=#{1,6}\s)", segment)]
            boundaries += [m.end() for m in re.finditer(r"\n\s*\n", segment)]
            eligible = [b for b in boundaries if minimum <= b <= len(segment)]
            if eligible:
                end = cursor + max(eligible)
            else:
                # If the paragraph is too long, avoid splitting through URLs/words.
                spaces = [m.end() for m in re.finditer(r"\s", segment)]
                eligible = [b for b in spaces if minimum <= b <= len(segment)]
                if eligible:
                    end = cursor + max(eligible)
        if end <= cursor:
            end = ceiling
        piece = text[cursor:end]
        headings = re.findall(r"(?m)^#{1,6}\s+([^\n]+)", piece)
        if headings:
            last_title = headings[-1].strip()[:120] or last_title
        sections.append(ReportSection(len(sections), piece, last_title, cursor, end))
        cursor = end
    assert "".join(section.text for section in sections) == text
    return sections
