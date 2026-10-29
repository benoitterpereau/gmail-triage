#!/usr/bin/env python3
"""Block secrets and account identifiers before they reach a commit or a public release.

    python3 tools/guard.py --staged         what is staged for commit (the pre-commit hook)
    python3 tools/guard.py --all            every tracked file
    python3 tools/guard.py --tree <dir>     a release folder: strict, tasks/ refused too

Secrets are refused everywhere, tasks/ included. Account identifiers (Gmail label and message ids, n8n ids,
email addresses, the VPS host, IP addresses, the ids and digest subjects of n8n/config.local.json) are refused
outside tasks/, which stays in the private repo. A line containing "guard: allow" is skipped.
Findings show the file, the line and the kind of match, never the matched value.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_DIRS = ("tasks/",)   # kept out of public releases, so identifiers are fine there
FORBIDDEN_FILES = [re.compile(p) for p in (r"(^|/)\.env($|\.)", r"(^|/)[^/]*\.local\.json$", r"(^|/)dist/")]

SECRET_PATTERNS = {
    "Anthropic API key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "JWT (n8n API key)": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "GitHub token": r"\b(ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "Google OAuth token": r"\bya29\.[A-Za-z0-9_-]{20,}",
    "Slack token": r"\bxox[abpr]-[A-Za-z0-9-]{10,}",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}
IDENTIFIER_PATTERNS = {
    "Gmail label id": r"\bLabel_\d+\b",
    "Gmail message id": r"\b1[89a-f][0-9a-f]{14}\b",
    "n8n id": r"\b(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{16}\b",
    "email address": r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)\b)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b",
    "VPS host": r"\bvps-[0-9a-f]{6,}\.vps\.ovh\.net\b",
    "IP address": r"\b(?!127\.0\.0\.1\b|0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b",
}


def local_values():
    """Secret values from .env and account values from config.local.json, as (kind, value)."""
    values = []
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            value = value.strip().strip("'\"")
            if sep and not key.startswith("#") and len(value) >= 12 and not value.startswith("http"):
                values.append(("secret", f"value of {key.strip()} from .env", value))
    config = ROOT / "n8n" / "config.local.json"
    if config.exists():
        cfg = json.loads(config.read_text())
        values += [("identifier", f"credential id from config ({k})", v["id"])     # {"id": ..., "name": ...}
                   for k, v in cfg.get("credentials", {}).items() if isinstance(v, dict) and v.get("id")]
        values += [("identifier", "digest subject from config", v) for v in cfg.get("digestSubjects", []) if v]
    return values


def check(files, strict):
    """files: {path: text}. Returns findings as strings."""
    secret_res = [(kind, re.compile(p)) for kind, p in SECRET_PATTERNS.items()]
    ident_res = [(kind, re.compile(p)) for kind, p in IDENTIFIER_PATTERNS.items()]
    values = local_values()
    findings = []
    for path, text in sorted(files.items()):
        if any(r.search(path) for r in FORBIDDEN_FILES):
            findings.append(f"{path}: file must never be committed or published")
            continue
        private = path.startswith(PRIVATE_DIRS)
        if strict and private:
            findings.append(f"{path}: private folder, not part of a release")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if "guard: allow" in line:
                continue
            hits = [kind for kind, r in secret_res if r.search(line)]
            hits += [kind for level, kind, v in values if level == "secret" and v in line]
            if not private:
                hits += [kind for kind, r in ident_res if r.search(line)]
                hits += [kind for level, kind, v in values if level == "identifier" and v in line]
            findings += [f"{path}:{number}: {kind}" for kind in dict.fromkeys(hits)]
    return findings


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True).stdout


def read(blob):
    try:
        return blob.decode()
    except UnicodeDecodeError:
        return ""   # binary file: nothing to read line by line


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--staged":
        paths = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode().split("\0")
        files = {p: read(git("show", f":{p}")) for p in paths if p}
        strict = False
    elif mode == "--all":
        files = {p: read(git("show", f"HEAD:{p}")) for p in git("ls-files", "-z").decode().split("\0") if p}
        strict = False
    elif mode == "--tree" and len(sys.argv) == 3:
        base = Path(sys.argv[2])
        files = {f.relative_to(base).as_posix(): read(f.read_bytes()) for f in base.rglob("*")
                 if f.is_file() and ".git" not in f.relative_to(base).parts}
        strict = True
    else:
        sys.exit(__doc__)
    findings = check(files, strict)
    if findings:
        print("✗ guard: blocked\n  " + "\n  ".join(findings), file=sys.stderr)
        print("  Remove them, or mark a line that is safe with the comment `guard: allow`.", file=sys.stderr)
        sys.exit(1)
    print(f"✓ guard: {len(files)} files clean")


if __name__ == "__main__":
    main()
