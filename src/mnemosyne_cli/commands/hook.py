"""Git hook handlers and Claude Code hook subcommands."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import time

import typer

app = typer.Typer(no_args_is_help=True)

# Injection detection patterns (ported from gsd-prompt-guard.js)
# Advisory only -- does not block operations.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile('ignore\\s+(all\\s+)?previous\\s+instructions', re.IGNORECASE),
    re.compile('ignore\\s+(all\\s+)?above\\s+instructions', re.IGNORECASE),
    re.compile('disregard\\s+(all\\s+)?previous', re.IGNORECASE),
    re.compile('forget\\s+(all\\s+)?(your\\s+)?instructions', re.IGNORECASE),
    re.compile('override\\s+(system|previous)\\s+(prompt|instructions)', re.IGNORECASE),
    re.compile('you\\s+are\\s+now\\s+(?:a|an|the)\\s+', re.IGNORECASE),
    re.compile('act\\s+as\\s+(?:a|an|the)\\s+(?!plan|phase|wave)', re.IGNORECASE),
    re.compile("pretend\\s+(?:you(?:'re| are)\\s+|to\\s+be\\s+)", re.IGNORECASE),
    re.compile('from\\s+now\\s+on,?\\s+you\\s+(?:are|will|should|must)', re.IGNORECASE),
    re.compile('(?:print|output|reveal|show|display|repeat)\\s+(?:your\\s+)?(?:system\\s+)?(?:prompt|instructions)', re.IGNORECASE),
    re.compile('</?(?:system|assistant|human)>', re.IGNORECASE),
    re.compile('\\[SYSTEM\\]', re.IGNORECASE),
    re.compile('\\[INST\\]', re.IGNORECASE),
    re.compile('<<\\s*SYS\\s*>>', re.IGNORECASE),
]

_INVISIBLE_UNICODE = re.compile('[\\u200B-\\u200F\\u2028-\\u202F\\uFEFF\\u00AD]')


@app.command("post-change")
def post_change() -> None:
    """Detect container and vault content changes and print refresh suggestions."""
    result = subprocess.run(["git","diff-tree","--no-commit-id","--name-only","-r","HEAD"],capture_output=True,text=True)
    changed_files = result.stdout.strip()
    if not changed_files:
        orig = subprocess.run(["git","rev-parse","--git-dir"],capture_output=True,text=True)
        git_dir = orig.stdout.strip()
        if git_dir:
            oh = pathlib.Path(git_dir)/"ORIG_HEAD"
            if oh.exists():
                r2 = subprocess.run(["git","diff","--name-only","ORIG_HEAD","HEAD"],capture_output=True,text=True)
                changed_files = r2.stdout.strip()
    if not changed_files:
        return
    files = changed_files.splitlines()
    ni = any(re.match(r"^containers/",f) for f in files)
    nq = any(re.match(r"^(technologies/|agents/|docs/|projects/.*\.md)",f) for f in files)
    if ni and nq:
        typer.echo(""); typer.echo("  ⟳ Container files and vault content changed — run: mnemosyne refresh"); typer.echo("")
    elif ni:
        typer.echo(""); typer.echo("  ⟳ Container files changed — run: mnemosyne refresh --skip-qmd"); typer.echo("")
    elif nq:
        typer.echo(""); typer.echo("  ⟳ Vault content changed — run: mnemosyne refresh --skip-images"); typer.echo("")


@app.command("context-monitor")
def context_monitor() -> None:
    """PostToolUse hook: warn agent when context is running low.

    Returns empty output when context is fine to avoid blocking auto-compact.
    """
    WT,CT,SS,DC=35,25,60,5
    try:
        raw=sys.stdin.read(); data:dict=json.loads(raw) if raw and raw.strip() else {}
    except(json.JSONDecodeError,OSError):
        data={}
    sid:str=data.get("session_id") or ""
    if sid and ("/" in sid or "\\" in sid or ".." in sid): raise typer.Exit(0)
    if not sid: raise typer.Exit(0)
    cwd:str=data.get("cwd") or ""
    if cwd:
        cp=pathlib.Path(cwd)/".planning"/"config.json"
        if cp.exists():
            try:
                cfg=json.loads(cp.read_text())
                if(cfg.get("hooks") or {}).get("context_warnings") is False: raise typer.Exit(0)
            except(json.JSONDecodeError,OSError): pass
    tb=pathlib.Path(tempfile.gettempdir())/("claude-ctx-"+sid+".json")
    if not tb.exists(): raise typer.Exit(0)
    try: metrics:dict=json.loads(tb.read_text())
    except(json.JSONDecodeError,OSError): raise typer.Exit(0)
    now=int(time.time())
    if metrics.get("timestamp") and(now-metrics["timestamp"])>SS: raise typer.Exit(0)
    rem:float=metrics.get("remaining_percentage",100)
    used:int=metrics.get("used_pct",0)
    if rem>WT: raise typer.Exit(0)
    wp=pathlib.Path(tempfile.gettempdir())/("claude-ctx-"+sid+"-warned.json")
    wd:dict={"callsSinceWarn":0,"lastLevel":None}; fw=True
    if wp.exists():
        try: wd=json.loads(wp.read_text()); fw=False
        except(json.JSONDecodeError,OSError): pass
    wd["callsSinceWarn"]=(wd.get("callsSinceWarn") or 0)+1
    ic=rem<=CT; cl="critical" if ic else "warning"
    se=cl=="critical" and wd.get("lastLevel")=="warning"
    if not fw and wd["callsSinceWarn"]<DC and not se:
        try: wp.write_text(json.dumps(wd))
        except OSError: pass
        raise typer.Exit(0)
    wd["callsSinceWarn"]=0; wd["lastLevel"]=cl
    try: wp.write_text(json.dumps(wd))
    except OSError: pass
    pa=bool(cwd and(pathlib.Path(cwd)/".planning"/"STATE.md").exists())
    if ic:
        msg=("CONTEXT CRITICAL: Usage at "+str(used)+"%. Remaining: "+f"{rem:.0f}"+"%. Context nearly exhausted. Do NOT start new complex work. "+("GSD/mnemosyne state tracked in STATE.md. Inform user to run /mnemosyne-pause." if pa else "Inform user context is low."))
    else:
        msg=("CONTEXT WARNING: Usage at "+str(used)+"%. Remaining: "+f"{rem:.0f}"+"%. Context getting limited."+(" Inform user to prepare to pause." if pa else " Avoid new complex work."))
    sys.stdout.write(json.dumps({"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":msg}}))


@app.command("prompt-guard")
def prompt_guard() -> None:
    """PreToolUse hook: scan .planning/ writes for prompt injection patterns. Advisory only."""
    try:
        raw=sys.stdin.read(); data:dict=json.loads(raw) if raw and raw.strip() else {}
    except(json.JSONDecodeError,OSError):
        data={}
    tn:str=data.get("tool_name") or ""
    if tn not in("Write","Edit"): sys.stdout.write(json.dumps({"decision":"allow"})); return
    ti:dict=data.get("tool_input") or {}; fp:str=ti.get("file_path") or ""
    if ".planning/" not in fp and ".planning\\" not in fp: sys.stdout.write(json.dumps({"decision":"allow"})); return
    ct:str=ti.get("content") or ti.get("new_string") or ""
    if not ct: sys.stdout.write(json.dumps({"decision":"allow"})); return
    findings:list[str]=[]
    for pat in _INJECTION_PATTERNS:
        if pat.search(ct): findings.append(pat.pattern)
    if _INVISIBLE_UNICODE.search(ct): findings.append("invisible-unicode-characters")
    if not findings: sys.stdout.write(json.dumps({"decision":"allow"})); return
    bn=pathlib.Path(fp).name
    sys.stdout.write(json.dumps({"decision":"allow","hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"⚠️ PROMPT INJECTION WARNING: Content written to "+bn+" triggered "+str(len(findings))+" pattern(s): "+", ".join(findings)+". Review for embedded instructions."}}))
