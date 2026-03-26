#!/usr/bin/env python3
"""Git merge driver for GSD ROADMAP.md files.

Resolves common conflicts: union of completed phases, active phases,
phase sections, and plan checkboxes (checked wins). Falls back to git's
default merge on error.

Usage (registered via git config):
    python3 mnemosyne_scripts/merge-gsd-roadmap.py %O %A %B
"""

import re
import sys


def split_into_sections(text):
    """Split markdown into sections by ### headings.

    Returns (preamble, [(heading_line, body), ...]).
    Preamble is everything before the first ### heading.
    """
    sections = []
    preamble_lines = []
    current_heading = None
    current_lines = []

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


def extract_phase_number(heading):
    match = re.search(r"Phase\s+(\d+)", heading)
    return int(match.group(1)) if match else None


def extract_table_rows(text):
    """Extract table data rows (not header or separator)."""
    rows = []
    seen_header = False
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        if re.match(r"^\|[-\s|]+\|$", line):
            seen_header = True
            continue
        if not seen_header:
            seen_header = True  # first row is header
            continue
        rows.append(line)
    return rows


def get_table_header_and_sep(text):
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


def phase_key_from_row(row):
    cells = [c.strip() for c in row.split("|") if c.strip()]
    if cells:
        return re.sub(r"~+", "", cells[0]).strip()
    return row


def union_table(base_text, ours_text, theirs_text):
    """Union table rows by first column (phase number), preserving ours' order."""
    ours_rows = extract_table_rows(ours_text)
    theirs_rows = extract_table_rows(theirs_text)

    seen_keys = set()
    merged = []
    for row in ours_rows:
        key = phase_key_from_row(row)
        seen_keys.add(key)
        merged.append(row)
    for row in theirs_rows:
        key = phase_key_from_row(row)
        if key not in seen_keys:
            seen_keys.add(key)
            merged.append(row)

    header, sep = get_table_header_and_sep(ours_text)
    if header and sep:
        return "\n".join([header, sep] + merged) + "\n"
    return "\n".join(merged) + "\n"


def merge_preamble(base_pre, ours_pre, theirs_pre):
    """Merge the preamble (everything before ### sections).

    Contains the Active Phases and Completed Phases tables.
    """
    if ours_pre == theirs_pre:
        return ours_pre

    def find_table_block(text, table_name):
        """Find a markdown table preceded by a ## heading containing table_name."""
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
            merged_table = union_table(base_table, ours_table, theirs_table)
            result = result.replace(ours_heading + ours_table, ours_heading + merged_table)

    return result


def merge_plan_checkboxes(ours_body, theirs_body):
    """Merge plan checkboxes: [x] wins over [ ]."""
    ours_lines = ours_body.splitlines(keepends=True)
    theirs_lines = theirs_body.splitlines(keepends=True)

    theirs_checked = set()
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

    # Add lines from theirs that don't exist in ours
    ours_plan_names = set()
    for line in ours_lines:
        match = re.match(r"^- \[[ x]\]\s+(.*)", line)
        if match:
            ours_plan_names.add(match.group(1).strip())

    for line in theirs_lines:
        match = re.match(r"^- \[[ x]\]\s+(.*)", line)
        if match and match.group(1).strip() not in ours_plan_names:
            merged.append(line)

    return "".join(merged)


def merge_sections(base_sections, ours_sections, theirs_sections):
    """Merge ### Phase sections."""
    base_map = {extract_phase_number(h): (h, b) for h, b in base_sections}
    ours_map = {extract_phase_number(h): (h, b) for h, b in ours_sections}
    theirs_map = {extract_phase_number(h): (h, b) for h, b in theirs_sections}

    all_phases = []
    seen = set()
    for phase_num in [extract_phase_number(h) for h, _ in ours_sections]:
        if phase_num is not None and phase_num not in seen:
            all_phases.append(phase_num)
            seen.add(phase_num)
    for phase_num in [extract_phase_number(h) for h, _ in theirs_sections]:
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
                merged_body = merge_plan_checkboxes(ours_body, theirs_body)
                merged.append((ours_heading, merged_body))

    return merged


def main():
    if len(sys.argv) != 4:
        sys.exit(1)

    base_path, ours_path, theirs_path = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(base_path) as f:
        base_text = f.read()
    with open(ours_path) as f:
        ours_text = f.read()
    with open(theirs_path) as f:
        theirs_text = f.read()

    if ours_text == theirs_text:
        sys.exit(0)

    base_pre, base_sections = split_into_sections(base_text)
    ours_pre, ours_sections = split_into_sections(ours_text)
    theirs_pre, theirs_sections = split_into_sections(theirs_text)

    merged_pre = merge_preamble(base_pre, ours_pre, theirs_pre)
    merged_sections = merge_sections(base_sections, ours_sections, theirs_sections)

    result = merged_pre
    for heading, body in merged_sections:
        result += heading + "\n" + body

    with open(ours_path, "w") as f:
        f.write(result)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
