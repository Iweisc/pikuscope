"""Unified diff parsing with line-number annotation for LLM prompts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str  # the @@ line (may carry section heading)
    lines: list[str] = field(default_factory=list)  # raw lines incl. +/-/space prefix


@dataclass
class FileDiff:
    path: str
    old_path: str
    status: str  # added|modified|removed|renamed
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def additions(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.startswith("+"))

    @property
    def deletions(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.startswith("-"))

    def new_line_numbers(self) -> set[int]:
        """Line numbers (new file side) that are added or context — commentable RIGHT lines."""
        out: set[int] = set()
        for h in self.hunks:
            n = h.new_start
            for l in h.lines:
                if l.startswith("+") or l.startswith(" "):
                    out.add(n)
                    n += 1
        return out

    def added_line_numbers(self) -> set[int]:
        out: set[int] = set()
        for h in self.hunks:
            n = h.new_start
            for l in h.lines:
                if l.startswith("+"):
                    out.add(n)
                    n += 1
                elif l.startswith(" "):
                    n += 1
        return out

    def annotated(self, max_chars: int = 60_000) -> str:
        """Render hunks with explicit new/old line numbers for precise LLM anchoring."""
        parts: list[str] = []
        for h in self.hunks:
            parts.append(h.header)
            o, n = h.old_start, h.new_start
            for l in h.lines:
                if l.startswith("+"):
                    parts.append(f"{n:>5} + {l[1:]}")
                    n += 1
                elif l.startswith("-"):
                    parts.append(f"{o:>5} - {l[1:]}")
                    o += 1
                elif l.startswith("\\"):
                    parts.append(f"        {l}")
                else:
                    parts.append(f"{n:>5}   {l[1:] if l else ''}")
                    o += 1
                    n += 1
        text = "\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (diff truncated)"
        return text


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_unified_diff(diff_text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    cur_hunk: Hunk | None = None
    old_path = new_path = ""
    status = "modified"
    is_binary = False

    def flush() -> None:
        nonlocal cur, cur_hunk
        if cur is not None:
            files.append(cur)
        cur = None
        cur_hunk = None

    lines = diff_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            flush()
            m = re.match(r'diff --git (?:"?a/(.*?)"?) (?:"?b/(.*?)"?)$', line)
            old_path = m.group(1) if m else ""
            new_path = m.group(2) if m else ""
            status = "modified"
            is_binary = False
            cur = None
        elif line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "removed"
        elif line.startswith("rename from "):
            status = "renamed"
            old_path = line[len("rename from "):]
        elif line.startswith("rename to "):
            new_path = line[len("rename to "):]
        elif line.startswith("Binary files"):
            is_binary = True
            if cur is None:
                cur = FileDiff(path=new_path or old_path, old_path=old_path,
                               status=status, is_binary=True)
        elif line.startswith("--- "):
            pass
        elif line.startswith("+++ "):
            p = line[4:]
            if p.startswith("b/"):
                new_path = p[2:]
            elif p == "/dev/null":
                new_path = old_path
        elif line.startswith("@@"):
            m = HUNK_RE.match(line)
            if m:
                if cur is None:
                    cur = FileDiff(path=new_path or old_path, old_path=old_path, status=status)
                cur_hunk = Hunk(
                    old_start=int(m.group(1)),
                    old_count=int(m.group(2) or 1),
                    new_start=int(m.group(3)),
                    new_count=int(m.group(4) or 1),
                    header=line,
                )
                cur.hunks.append(cur_hunk)
        elif cur_hunk is not None and (line[:1] in ("+", "-", " ", "\\") or line == ""):
            cur_hunk.lines.append(line if line else " ")
        i += 1
    flush()
    return [f for f in files if f.path]
