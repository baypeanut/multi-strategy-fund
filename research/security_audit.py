"""Security audit agent.

A read-only reviewer that walks the source tree on a rotation so every file is
inspected on a fixed cycle, and writes a report. It has no write access to
anything except its own report directory, and it never proposes or applies a
code change. Findings go to a human.

Two layers, deliberately:

  1. Deterministic scanners run on every file, every pass. They catch the
     things that must never regress and that a model should not be trusted to
     catch reliably: credential literals, shell injection surfaces, unsafe
     deserialization, world readable secret files, gitignore coverage.

  2. A model pass reviews a rotating slice with a security only prompt. The
     rotation is tracked in state so coverage is provable rather than assumed:
     the report states the oldest unreviewed file and the cycle position.

Run: python research/security_audit.py [--slice N] [--no-model]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.env import get_key

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "security" / "reports"
STATE_PATH = ROOT / "security" / "audit_state.json"

AUDIT_MODEL = "claude-opus-5"
SCAN_DIRS = ("core", "systems", "runtime", "research", "scripts", "backtest",
             "dashboard", "config", "deploy")
SCAN_EXT = {".py", ".yaml", ".yml", ".sh"}

# Files that must never be readable by anyone but their owner. Anything here
# that is group or world readable is a finding regardless of content.
SECRET_PATHS = (".env", "config/config.ini")

# Deterministic rules. Each is (id, severity, compiled pattern, why it matters).
# Kept narrow on purpose: a scanner that cries wolf gets ignored, and an ignored
# scanner is worse than no scanner.
RULES = [
    ("SEC001", "critical",
     re.compile(r"""(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*["'][A-Za-z0-9_\-/+]{16,}["']""", re.I),
     "credential literal in source"),
    ("SEC002", "critical",
     re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
     "Anthropic key literal"),
    ("SEC003", "high",
     re.compile(r"subprocess\.(?:run|Popen|call|check_output)\([^)]*shell\s*=\s*True"),
     "shell=True on a subprocess call is an injection surface"),
    ("SEC004", "high",
     re.compile(r"\b(?:pickle|dill)\.loads?\s*\(|\byaml\.load\s*\((?![^)]*Loader\s*=\s*yaml\.SafeLoader)"),
     "unsafe deserialization of untrusted input"),
    ("SEC005", "high",
     re.compile(r"(?<![\w.])\b(?:eval|exec)\s*\("),
     "dynamic code execution"),
    ("SEC006", "medium",
     re.compile(r"verify\s*=\s*False|ssl\._create_unverified_context"),
     "TLS verification disabled"),
    ("SEC007", "medium",
     re.compile(r"except\s+Exception\s*:\s*(?:\n\s*)?(?:pass|return\s+None)\s*(?:$|\n)"),
     "broad exception swallowed, can hide a security failure"),
]

_MODEL_SYSTEM = """You are a security reviewer for a systematic trading
platform. You review one file at a time and report only security relevant
findings. You do not review style, performance, or design.

What matters here, in order:
  1. Credential handling. Keys must come from the environment and must never
     be logged, echoed into an error message, written to state, or sent to a
     model as context.
  2. Anything that reaches outside the process: subprocess calls, network
     calls, file writes outside the repo, broker orders. Look at what an
     attacker who controlled the input could make it do.
  3. Deserialization, dynamic execution, and path handling. Untrusted paths
     that are joined without validation, archives extracted without checks.
  4. Failure modes that hide problems: broad exception handlers that swallow
     errors, guards that fail open instead of closed, logging that leaks
     values it should redact.
  5. Financial safety: an order path that can execute without passing the
     risk checks, or a limit that can be bypassed.

Report only what you can point at in this file. No speculation, no
"consider adding" suggestions unless the absence is itself the defect. If the
file is clean, say so with an empty findings list. Severity is one of
critical, high, medium, low."""

_MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string",
                                 "enum": ["critical", "high", "medium", "low"]},
                    "line_hint": {"type": "string"},
                    "issue": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                },
                "required": ["severity", "line_hint", "issue", "why_it_matters"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "findings"],
    "additionalProperties": False,
}


def source_files(root: Path = ROOT) -> list[Path]:
    """Every file in scope, in a stable order so rotation is deterministic."""
    out: list[Path] = []
    for d in SCAN_DIRS:
        base = root / d
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and f.suffix in SCAN_EXT and "__pycache__" not in f.parts:
                out.append(f)
    return sorted(out)


def scan_file(path: Path, text: str) -> list[dict]:
    """Deterministic rules. Runs on every file, every pass."""
    findings = []
    for rid, severity, pattern, why in RULES:
        for m in pattern.finditer(text):
            line = text[:m.start()].count("\n") + 1
            findings.append({"rule": rid, "severity": severity,
                             "line": line, "issue": why,
                             "excerpt": m.group(0)[:80]})
    return findings


def check_permissions(root: Path = ROOT) -> list[dict]:
    """Secret files must be owner only. A 644 .env is a finding by itself."""
    findings = []
    for rel in SECRET_PATHS:
        p = root / rel
        if not p.exists():
            continue
        mode = p.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            findings.append({"rule": "SEC010", "severity": "critical",
                             "line": 0, "file": rel,
                             "issue": f"{rel} is readable beyond its owner "
                                      f"({oct(stat.S_IMODE(mode))})",
                             "excerpt": ""})
    return findings


def check_gitignore(root: Path = ROOT) -> list[dict]:
    """A secret that is merely untracked is one `git add -A` from being public."""
    gi = root / ".gitignore"
    body = gi.read_text() if gi.exists() else ""
    findings = []
    for pattern in (".env", "config.ini"):
        if pattern not in body:
            findings.append({"rule": "SEC011", "severity": "critical",
                             "line": 0, "file": ".gitignore",
                             "issue": f"'{pattern}' is not in .gitignore",
                             "excerpt": ""})
    return findings


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"reviewed": {}, "passes": 0}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True))


def next_slice(files: list[Path], state: dict, size: int) -> list[Path]:
    """Least recently reviewed first, so coverage is a rotation and not a
    sample. A file that has never been reviewed always sorts ahead of one
    that has."""
    reviewed = state.get("reviewed", {})
    return sorted(files, key=lambda f: reviewed.get(
        str(f.relative_to(ROOT)), ""))[:size]


def review_with_model(path: Path, text: str, client) -> tuple[dict | None, str | None]:
    rel = str(path.relative_to(ROOT))
    try:
        with client.beta.messages.stream(
            model=AUDIT_MODEL,
            max_tokens=8000,
            betas=["server-side-fallback-2026-06-01"],
            fallbacks=[{"model": "claude-opus-4-8"}],
            system=_MODEL_SYSTEM,
            output_config={"format": {"type": "json_schema",
                                      "schema": _MODEL_SCHEMA},
                           "effort": "high"},
            messages=[{"role": "user",
                       "content": f"=== FILE: {rel} ===\n{text[:120_000]}"}],
        ) as stream:
            resp = stream.get_final_message()
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if resp.stop_reason == "refusal":
        return None, "refusal"
    raw = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"malformed JSON: {exc}"


def is_substantive(finding: dict) -> bool:
    """Drop findings the model emitted as placeholders.

    The first live pass produced a row reading `**critical** config.yaml (...):
    ...` with no content at all. An empty critical is worse than no finding: it
    costs a reader real time and there is nothing at the end of it. A finding
    has to carry its own evidence to survive into the report.
    """
    for field in ("issue", "why_it_matters"):
        body = str(finding.get(field, "")).strip().strip(".").strip()
        if len(body) < 25:
            return False
    return True


def write_report(now: datetime, deterministic: list[dict], model_findings: list[dict],
                 reviewed: list[str], coverage: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"audit_{now.strftime('%Y%m%d')}.md"
    crit = sum(1 for f in deterministic + model_findings
               if f.get("severity") == "critical")
    high = sum(1 for f in deterministic + model_findings
               if f.get("severity") == "high")

    lines = [f"# Security audit {now.date().isoformat()}", ""]
    lines.append(f"Scanned {coverage['total_files']} files with the "
                 f"deterministic rules. Model reviewed {len(reviewed)} this "
                 f"pass. Critical: {crit}. High: {high}.")
    lines.append("")
    lines.append(f"Coverage: {coverage['reviewed_ever']}/{coverage['total_files']} "
                 f"files reviewed by the model at least once. Oldest review "
                 f"{coverage['oldest_review'] or 'never'}.")
    lines.append("")

    if deterministic:
        lines.append("## Deterministic findings")
        for f in sorted(deterministic, key=lambda x: x["severity"]):
            lines.append(f"- **{f['severity']}** `{f.get('file', '')}`"
                         f"{':' + str(f['line']) if f.get('line') else ''} "
                         f"[{f['rule']}] {f['issue']}")
        lines.append("")
    else:
        lines.append("## Deterministic findings\n\nNone.\n")

    if model_findings:
        lines.append("## Model review")
        for f in model_findings:
            lines.append(f"- **{f['severity']}** `{f['file']}` "
                         f"({f['line_hint']}): {f['issue']}")
            lines.append(f"  - {f['why_it_matters']}")
        lines.append("")
    else:
        lines.append("## Model review\n\nNo findings in this pass.\n")

    lines.append("## Files reviewed this pass")
    lines.extend(f"- {r}" for r in reviewed)
    path.write_text("\n".join(lines) + "\n")
    return path


def run_audit(slice_size: int = 6, use_model: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    files = source_files()
    state = load_state()

    deterministic: list[dict] = []
    for f in files:
        try:
            text = f.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for finding in scan_file(f, text):
            finding["file"] = str(f.relative_to(ROOT))
            deterministic.append(finding)
    deterministic += check_permissions()
    deterministic += check_gitignore()

    model_findings: list[dict] = []
    reviewed: list[str] = []
    client = None
    if use_model:
        key = get_key("ANTHROPIC_API_KEY")
        if key:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=key, timeout=600)
            except Exception:
                client = None

    if client is not None:
        for f in next_slice(files, state, slice_size):
            rel = str(f.relative_to(ROOT))
            try:
                text = f.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            out, err = review_with_model(f, text, client)
            state.setdefault("reviewed", {})[rel] = now.isoformat()
            reviewed.append(rel if not err else f"{rel} (error: {err})")
            for finding in (out or {}).get("findings", []):
                if not is_substantive(finding):
                    continue
                finding["file"] = rel
                model_findings.append(finding)

    state["passes"] = state.get("passes", 0) + 1
    state["last_pass"] = now.isoformat()
    save_state(state)

    seen = state.get("reviewed", {})
    coverage = {
        "total_files": len(files),
        "reviewed_ever": sum(1 for f in files
                             if str(f.relative_to(ROOT)) in seen),
        "oldest_review": min(seen.values()) if seen else None,
    }
    report = write_report(now, deterministic, model_findings, reviewed, coverage)
    return {"report": str(report), "deterministic": len(deterministic),
            "model_findings": len(model_findings), "reviewed": reviewed,
            "coverage": coverage}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slice", type=int, default=6,
                    help="files sent to the model this pass")
    ap.add_argument("--no-model", action="store_true",
                    help="deterministic scanners only")
    args = ap.parse_args()
    out = run_audit(slice_size=args.slice, use_model=not args.no_model)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
