"""Git merge drivers for GSD files."""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True)


# ---------------------------------------------------------------------------
# STATE merge helpers (ported from scripts/merge-gsd-state.py)
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\n(.*?\n)---\n(.*)$", text, re.DOTALL)
    if not match:
        return {}, text
    fm_text, body = match.group(1), match.group(2)
    fm: dict = {}
    current_key = None
    indent_block: dict = {}
    for line in fm_text.splitlines():
        if re.match(r"^  \w+:", line):
            k, v = line.strip().split(":", 1)
            indent_block[k.strip()] = v.strip()
        elif re.match(r"^\w+:", line):
            if current_key and indent_block:
                fm[current_key] = indent_block
                indent_block = {}
            k, v = line.split(":", 1)
            current_key = k.strip()
            v = v.strip()
            if v:
                fm[current_key] = v
        else:
            continue
    if current_key and indent_block:
        fm[current_key] = indent_block
    return fm, body


def _serialize_frontmatter(fm: dict, original_text: str) -> str:
    match = re.match(r"^---\n(.*?\n)---\n", original_text, re.DOTALL)
    if not match:
        return ""
    lines = []
    for line in match.group(1).splitlines():
        key_match = re.match(r"^(\w+):\s*(.*)", line)
        indent_match = re.match(r"^(  )(\w+):\s*(.*)", line)
        if indent_match:
            parent = [k for k in fm if isinstance(fm[k], dict)]
            for p in parent:
                if indent_match.group(2) in fm[p]:
                    lines.append(f"  {indent_match.group(2)}: {fm[p][indent_match.group(2)]}")
                    break
            else:
                lines.append(line)
        elif key_match:
            k = key_match.group(1)
            if k in fm and not isinstance(fm[k], dict):
                lines.append(f"{k}: {fm[k]}")
            else:
                lines.append(line)
        else:
            lines.append(line)
    return "---\n" + "\n".join(lines) + "\n---\n"


def _parse_timestamp(ts: str) -> datetime:
    ts = ts.strip().strip('"')
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return datetime.min


def _merge_frontmatter(base: dict, ours: dict, theirs: dict) -> dict:
    merged = dict(base)
    ts_o = _parse_timestamp(ours.get("last_updated", ""))
    ts_t = _parse_timestamp(theirs.get("last_updated", ""))
    merged["last_updated"] = ours["last_updated"] if ts_o >= ts_t else theirs["last_updated"]
    if isinstance(base.get("progress"), dict):
        merged_progress = dict(base.get("progress", {}))
        o_prog = ours.get("progress", {})
        t_prog = theirs.get("progress", {})
        for k in set(list(o_prog.keys()) + list(t_prog.keys())):
            try:
                merged_progress[k] = str(max(int(o_prog.get(k, 0)), int(t_prog.get(k, 0))))
            except (ValueError, TypeError):
                merged_progress[k] = o_prog.get(k, t_prog.get(k, base.get("progress", {}).get(k, "")))
        merged["progress"] = merged_progress
    for k in set(list(ours.keys()) + list(theirs.keys())):
        if k in ("last_updated", "progress"):
            continue
        o_val = ours.get(k)
        t_val = theirs.get(k)
        b_val = base.get(k)
        if o_val != b_val and t_val == b_val:
            merged[k] = o_val
        elif t_val != b_val and o_val == b_val:
            merged[k] = t_val
        else:
            merged[k] = o_val
    return merged


def _split_sections(body: str) -> list[tuple[str, str]]:
    sections = []
    current_heading = ""
    current_lines: list[str] = []
    for line in body.splitlines(keepends=True):
        if line.startswith("## "):
            if current_heading or current_lines:
                sections.append((current_heading, "".join(current_lines)))
            current_heading = line.strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_heading or current_lines:
        sections.append((current_heading, "".join(current_lines)))
    return sections


def _extract_table_rows(text: str) -> list[str]:
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"^\|[-\s|]+\|$", line):
            rows.append(line)
    return rows


def _union_table_rows(base_text: str, ours_text: str, theirs_text: str) -> str:
    ours_rows = _extract_table_rows(ours_text)
    theirs_rows = _extract_table_rows(theirs_text)
    if not ours_rows:
        return ours_text
    header = ours_rows[0] if ours_rows else ""
    seen: set[str] = set()
    merged_data = []
    for row in ours_rows[1:] + theirs_rows[1:]:
        normalised = re.sub(r"\s+", " ", row.strip())
        if normalised not in seen:
            seen.add(normalised)
            merged_data.append(row)
    sep_match = re.search(r"^\|[-\s|]+\|$", ours_text, re.MULTILINE)
    sep = sep_match.group(0) if sep_match else "|---------|------|----------|-----------|"
    lines = ours_text.splitlines(keepends=True)
    before: list[str] = []
    after: list[str] = []
    in_table = False
    past_table = False
    for line in lines:
        if line.startswith("|"):
            in_table = True
        elif in_table and not line.startswith("|"):
            past_table = True
            in_table = False
        if not in_table and not past_table:
            before.append(line)
        elif past_table:
            after.append(line)
    table_lines = [header, sep] + merged_data
    return "".join(before) + "\n".join(table_lines) + "\n" + "".join(after)


def _union_list_items(base_text: str, ours_text: str, theirs_text: str) -> str:
    def extract_items(text: str) -> list[str]:
        items = []
        for line in text.splitlines():
            if re.match(r"^\d+\.\s|^[-*]\s", line.strip()):
                items.append(line)
        return items

    ours_items = extract_items(ours_text)
    theirs_items = extract_items(theirs_text)
    seen: set[str] = set()
    merged = []
    for item in ours_items + theirs_items:
        key = re.sub(r"^\d+\.\s+|^[-*]\s+", "", item.strip())
        if key not in seen:
            seen.add(key)
            merged.append(item)
    non_list = [line for line in ours_text.splitlines() if not re.match(r"^\d+\.\s|^[-*]\s", line.strip())]
    return "\n".join(non_list + merged) + "\n"


def _merge_body(base_body: str, ours_body: str, theirs_body: str, ours_newer: bool) -> str:
    base_sections = {h: c for h, c in _split_sections(base_body)}
    ours_sections = _split_sections(ours_body)
    theirs_sections = {h: c for h, c in _split_sections(theirs_body)}
    ours_headings = {h for h, _ in ours_sections}
    theirs_only = [(h, c) for h, c in _split_sections(theirs_body) if h not in ours_headings and h]
    merged = []
    for heading, ours_content in ours_sections:
        base_content = base_sections.get(heading, "")
        theirs_content = theirs_sections.get(heading, ours_content)
        if ours_content == theirs_content:
            merged.append((heading, ours_content))
            continue
        heading_lower = heading.lower()
        if "current position" in heading_lower or "session continuity" in heading_lower:
            merged.append((heading, ours_content if ours_newer else theirs_content))
        elif "active phases" in heading_lower:
            merged.append((heading, _union_list_items(base_content, ours_content, theirs_content)))
        elif "recent decisions" in heading_lower:
            merged.append((heading, _union_table_rows(base_content, ours_content, theirs_content)))
        elif "accumulated context" in heading_lower:
            merged.append((heading, _union_list_items(base_content, ours_content, theirs_content)))
        else:
            merged.append((heading, ours_content if ours_newer else theirs_content))
    merged.extend(theirs_only)
    result = ""
    for heading, content in merged:
        if heading:
            result += heading + "\n"
        result += content
    return result


def _merge_state(base_path: Path, ours_path: Path, theirs_path: Path) -> None:
    base_text = base_path.read_text()
    ours_text = ours_path.read_text()
    theirs_text = theirs_path.read_text()
    if ours_text == theirs_text:
        return
    base_fm, base_body = _parse_frontmatter(base_text)
    ours_fm, ours_body = _parse_frontmatter(ours_text)
    theirs_fm, theirs_body = _parse_frontmatter(theirs_text)
    merged_fm = _merge_frontmatter(base_fm, ours_fm, theirs_fm)
    ours_newer = _parse_timestamp(ours_fm.get("last_updated", "")) >= _parse_timestamp(
        theirs_fm.get("last_updated", "")
    )
    merged_body = _merge_body(base_body, ours_body, theirs_body, ours_newer)
    result = _serialize_frontmatter(merged_fm, ours_text) + merged_body
    ours_path.write_text(result)


# ---------------------------------------------------------------------------
# ROADMAP merge helpers (ported from scripts/merge-gsd-roadmap.py)
# ---------------------------------------------------------------------------


def _split_into_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    sections = []
    preamble_lines: list[str] = []
    current_heading = None
    current_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("### "):
            if current_heading is not None:
                sections.append((current_heading, "".join(current_lines)))
            current_heading = line.rstrip("\n")
            current_lines = []
        elif current_heading is None:
            preamble_lines.append(line)
        else:
            current_lines.append(line)
    if current_heading is not None:
        sections.append((current_heading, "".join(current_lines)))
    return "".join(preamble_lines), sections


def _extract_phase_number(heading: str) -> int | None:
    match = re.search(r"Phase\s+(\d+)", heading)
    return int(match.group(1)) if match else None


def _extract_roadmap_table_rows(text: str) -> list[str]:
    rows = []
    seen_header = False
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        if re.match(r"^\|[-\s|]+\|$", line):
            seen_header = True
            continue
        if not seen_header:
            seen_header = True
            continue
        rows.append(line)
    return rows


def _get_table_header_and_sep(text: str) -> tuple[str | None, str | None]:
    header = None
    sep = None
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        if header is None:
            header = line
        elif re.match(r"^\|[-\s|]+\|$", line):
            sep = line
            break
    return header, sep


def _phase_key_from_row(row: str) -> str:
    cells = [c.strip() for c in row.split("|") if c.strip()]
    if cells:
        return re.sub(r"~+", "", cells[0]).strip()
    return row


def _union_roadmap_table(base_text: str, ours_text: str, theirs_text: str) -> str:
    ours_rows = _extract_roadmap_table_rows(ours_text)
    theirs_rows = _extract_roadmap_table_rows(theirs_text)
    seen_keys: set[str] = set()
    merged = []
    for row in ours_rows:
        key = _phase_key_from_row(row)
        seen_keys.add(key)
        merged.append(row)
    for row in theirs_rows:
        key = _phase_key_from_row(row)
        if key not in seen_keys:
            seen_keys.add(key)
            merged.append(row)
    header, sep = _get_table_header_and_sep(ours_text)
    if header and sep:
        return "\n".join([header, sep] + merged) + "\n"
    return "\n".join(merged) + "\n"


def _merge_preamble(base_pre: str, ours_pre: str, theirs_pre: str) -> str:
    if ours_pre == theirs_pre:
        return ours_pre

    def find_table_block(text: str, table_name: str):
        pattern = rf"(## [^\n]*{table_name}[^\n]*\n\n)((?:\|[^\n]+\n)+)"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.start(), match.end(), match.group(1), match.group(2)
        return None

    result = ours_pre
    for table_name in ("Active Phases", "Completed Phases"):
        base_match = find_table_block(base_pre, table_name)
        ours_match = find_table_block(ours_pre, table_name)
        theirs_match = find_table_block(theirs_pre, table_name)
        if ours_match and theirs_match:
            _, _, ours_heading, ours_table = ours_match
            _, _, _, base_table = base_match if base_match else (0, 0, "", "")
            _, _, _, theirs_table = theirs_match
            merged_table = _union_roadmap_table(base_table, ours_table, theirs_table)
            result = result.replace(ours_heading + ours_table, ours_heading + merged_table)
    return result


def _merge_plan_checkboxes(ours_body: str, theirs_body: str) -> str:
    ours_lines = ours_body.splitlines(keepends=True)
    theirs_lines = theirs_body.splitlines(keepends=True)
    theirs_checked: set[str] = set()
    for line in theirs_lines:
        match = re.match(r"^- \[x\]\s+(.*)", line)
        if match:
            theirs_checked.add(match.group(1).strip())
    merged = []
    for line in ours_lines:
        unchecked = re.match(r"^(- \[ \]\s+)(.*)", line)
        if unchecked and unchecked.group(2).strip() in theirs_checked:
            merged.append(f"- [x] {unchecked.group(2)}")
            if not line.endswith("\n"):
                pass
            else:
                merged[-1] += "\n"
        else:
            merged.append(line)
    ours_plan_names: set[str] = set()
    for line in ours_lines:
        match = re.match(r"^- \[[ x]\]\s+(.*)", line)
        if match:
            ours_plan_names.add(match.group(1).strip())
    for line in theirs_lines:
        match = re.match(r"^- \[[ x]\]\s+(.*)", line)
        if match and match.group(1).strip() not in ours_plan_names:
            merged.append(line)
    return "".join(merged)


def _merge_sections(
    base_sections: list[tuple[str, str]],
    ours_sections: list[tuple[str, str]],
    theirs_sections: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    ours_map = {_extract_phase_number(h): (h, b) for h, b in ours_sections}
    theirs_map = {_extract_phase_number(h): (h, b) for h, b in theirs_sections}
    all_phases: list[int | None] = []
    seen: set = set()
    for phase_num in [_extract_phase_number(h) for h, _ in ours_sections]:
        if phase_num is not None and phase_num not in seen:
            all_phases.append(phase_num)
            seen.add(phase_num)
    for phase_num in [_extract_phase_number(h) for h, _ in theirs_sections]:
        if phase_num is not None and phase_num not in seen:
            all_phases.append(phase_num)
            seen.add(phase_num)
    merged = []
    for phase_num in all_phases:
        if phase_num in ours_map and phase_num not in theirs_map:
            merged.append(ours_map[phase_num])
        elif phase_num in theirs_map and phase_num not in ours_map:
            merged.append(theirs_map[phase_num])
        elif phase_num in ours_map and phase_num in theirs_map:
            ours_heading, ours_body = ours_map[phase_num]
            _, theirs_body = theirs_map[phase_num]
            if ours_body == theirs_body:
                merged.append((ours_heading, ours_body))
            else:
                merged_body = _merge_plan_checkboxes(ours_body, theirs_body)
                merged.append((ours_heading, merged_body))
    return merged


def _merge_roadmap(base_path: Path, ours_path: Path, theirs_path: Path) -> None:
    base_text = base_path.read_text()
    ours_text = ours_path.read_text()
    theirs_text = theirs_path.read_text()
    if ours_text == theirs_text:
        return
    base_pre, base_sections = _split_into_sections(base_text)
    ours_pre, ours_sections = _split_into_sections(ours_text)
    theirs_pre, theirs_sections = _split_into_sections(theirs_text)
    merged_pre = _merge_preamble(base_pre, ours_pre, theirs_pre)
    merged_sections = _merge_sections(base_sections, ours_sections, theirs_sections)
    result = merged_pre
    for heading, body in merged_sections:
        result += heading + "\n" + body
    ours_path.write_text(result)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@app.command()
def state(
    base: Path = typer.Argument(..., help="Base version of the file (%O)."),
    ours: Path = typer.Argument(..., help="Our version of the file (%A)."),
    theirs: Path = typer.Argument(..., help="Their version of the file (%B)."),
) -> None:
    """Git merge driver for GSD STATE.md files."""
    try:
        _merge_state(base, ours, theirs)
    except Exception:
        raise typer.Exit(1)


@app.command()
def roadmap(
    base: Path = typer.Argument(..., help="Base version of the file (%O)."),
    ours: Path = typer.Argument(..., help="Our version of the file (%A)."),
    theirs: Path = typer.Argument(..., help="Their version of the file (%B)."),
) -> None:
    """Git merge driver for GSD ROADMAP.md files."""
    try:
        _merge_roadmap(base, ours, theirs)
    except Exception:
        raise typer.Exit(1)
