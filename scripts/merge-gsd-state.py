#!/usr/bin/env python3
"""Git merge driver for GSD STATE.md files.

Resolves common conflicts: timestamps, progress counters, decision tables,
and session continuity sections. Falls back to git's default merge on error.

Usage (registered via git config):
    python3 mnemosyne_scripts/merge-gsd-state.py %O %A %B
"""

import re
import sys
from datetime import datetime


def parse_frontmatter(text):
    """Split into (frontmatter_dict, body). Frontmatter is between --- markers."""
    match = re.match(r"^---\n(.*?\n)---\n(.*)$", text, re.DOTALL)
    if not match:
        return {}, text
    fm_text, body = match.group(1), match.group(2)
    fm = {}
    current_key = None
    indent_block = {}
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


def serialize_frontmatter(fm, original_text):
    """Rebuild frontmatter preserving original key order and formatting."""
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


def parse_timestamp(ts):
    ts = ts.strip().strip('"')
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return datetime.min


def merge_frontmatter(base, ours, theirs):
    merged = dict(base)
    ts_o = parse_timestamp(ours.get("last_updated", ""))
    ts_t = parse_timestamp(theirs.get("last_updated", ""))
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
            merged[k] = o_val  # both changed → take ours

    return merged


def split_sections(body):
    """Split markdown body into list of (heading, content) tuples."""
    sections = []
    current_heading = ""
    current_lines = []
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


def extract_table_rows(text):
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"^\|[-\s|]+\|$", line):
            rows.append(line)
    return rows


def union_table_rows(base_text, ours_text, theirs_text):
    """Union table rows, preserving header from ours."""
    base_rows = extract_table_rows(base_text)
    ours_rows = extract_table_rows(ours_text)
    theirs_rows = extract_table_rows(theirs_text)

    if not ours_rows:
        return ours_text

    header = ours_rows[0] if ours_rows else ""

    # Data rows (skip header)
    seen = set()
    merged_data = []
    for row in ours_rows[1:] + theirs_rows[1:]:
        normalised = re.sub(r"\s+", " ", row.strip())
        if normalised not in seen:
            seen.add(normalised)
            merged_data.append(row)

    # Reconstruct: find separator line
    sep_match = re.search(r"^\|[-\s|]+\|$", ours_text, re.MULTILINE)
    sep = sep_match.group(0) if sep_match else "|---------|------|----------|-----------|"

    # Find any text before and after the table
    lines = ours_text.splitlines(keepends=True)
    before = []
    after = []
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


def union_list_items(base_text, ours_text, theirs_text):
    """Union numbered/bulleted list items by content."""
    def extract_items(text):
        items = []
        for line in text.splitlines():
            if re.match(r"^\d+\.\s|^[-*]\s", line.strip()):
                items.append(line)
        return items

    ours_items = extract_items(ours_text)
    theirs_items = extract_items(theirs_text)

    seen = set()
    merged = []
    for item in ours_items + theirs_items:
        key = re.sub(r"^\d+\.\s+|^[-*]\s+", "", item.strip())
        if key not in seen:
            seen.add(key)
            merged.append(item)

    non_list = [line for line in ours_text.splitlines() if not re.match(r"^\d+\.\s|^[-*]\s", line.strip())]
    return "\n".join(non_list + merged) + "\n"


def merge_body(base_body, ours_body, theirs_body, ours_newer):
    base_sections = {h: c for h, c in split_sections(base_body)}
    ours_sections = split_sections(ours_body)
    theirs_sections = {h: c for h, c in split_sections(theirs_body)}

    # Track which headings theirs has that ours doesn't
    ours_headings = {h for h, _ in ours_sections}
    theirs_only = [(h, c) for h, c in split_sections(theirs_body) if h not in ours_headings and h]

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
            merged.append((heading, union_list_items(base_content, ours_content, theirs_content)))
        elif "recent decisions" in heading_lower:
            merged.append((heading, union_table_rows(base_content, ours_content, theirs_content)))
        elif "accumulated context" in heading_lower:
            merged.append((heading, union_list_items(base_content, ours_content, theirs_content)))
        else:
            merged.append((heading, ours_content if ours_newer else theirs_content))

    # Append sections only in theirs
    merged.extend(theirs_only)

    result = ""
    for heading, content in merged:
        if heading:
            result += heading + "\n"
        result += content
    return result


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

    base_fm, base_body = parse_frontmatter(base_text)
    ours_fm, ours_body = parse_frontmatter(ours_text)
    theirs_fm, theirs_body = parse_frontmatter(theirs_text)

    merged_fm = merge_frontmatter(base_fm, ours_fm, theirs_fm)
    ours_newer = parse_timestamp(ours_fm.get("last_updated", "")) >= parse_timestamp(
        theirs_fm.get("last_updated", "")
    )

    merged_body = merge_body(base_body, ours_body, theirs_body, ours_newer)
    result = serialize_frontmatter(merged_fm, ours_text) + merged_body

    with open(ours_path, "w") as f:
        f.write(result)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
