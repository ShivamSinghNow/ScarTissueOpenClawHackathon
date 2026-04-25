#!/usr/bin/env python3
"""Clean markdown fence corruption from attack patches and test them."""

import os
import re
import subprocess
import sys

ATTACKS_DIR = os.path.join(os.path.dirname(__file__), "attacks")
LANGCHAIN_DIR = os.path.expanduser("~/Desktop/langchain")


def clean_patch(content: str) -> tuple[str, int]:
    """Remove +``` lines and fix hunk headers. Returns (cleaned, num_removed).

    Hunks that become context-only after fence removal are dropped entirely,
    since git apply rejects no-op hunks as corrupt.
    """
    lines = content.splitlines(keepends=True)
    result = []
    new_file_offset = 0  # cumulative +lines removed (affects subsequent hunk start positions)
    total_removed = 0

    i = 0
    while i < len(lines):
        line = lines[i]

        # Reset offset at each new file header (multi-file patches)
        if line.startswith("--- "):
            new_file_offset = 0
            result.append(line)
            i += 1
            continue

        hunk_match = re.match(
            r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)\n?$", line
        )
        if hunk_match:
            old_start = int(hunk_match.group(1))
            old_count = int(hunk_match.group(2)) if hunk_match.group(2) is not None else 1
            new_start = int(hunk_match.group(3))
            new_count = int(hunk_match.group(4)) if hunk_match.group(4) is not None else 1
            rest = hunk_match.group(5)

            new_start_adjusted = new_start - new_file_offset

            # Collect hunk body
            hunk_body = []
            i += 1
            while i < len(lines):
                l = lines[i]
                if re.match(r"^@@|^---|^\+\+\+|^diff --git", l):
                    break
                hunk_body.append(l)
                i += 1

            # Strip fence lines
            filtered = []
            removed = 0
            for hl in hunk_body:
                if re.match(r"^\+```", hl):
                    removed += 1
                else:
                    filtered.append(hl)

            new_count_adjusted = new_count - removed
            new_file_offset += removed
            total_removed += removed

            # Drop context-only hunks — git apply rejects them as corrupt
            has_changes = any(l[0] in ("+", "-") for l in filtered)
            if has_changes:
                result.append(
                    f"@@ -{old_start},{old_count} +{new_start_adjusted},{new_count_adjusted} @@{rest}\n"
                )
                result.extend(filtered)
        else:
            result.append(line)
            i += 1

    return "".join(result), total_removed


def try_apply(patch_path: str) -> tuple[bool, str]:
    """Run git apply --check on a patch. Returns (success, error_msg)."""
    result = subprocess.run(
        ["git", "apply", "--check", patch_path],
        cwd=LANGCHAIN_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout).strip()
    return False, err


def read_warning_md(sha_dir: str) -> str:
    path = os.path.join(sha_dir, "expected_warning.md")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return ""


def main():
    shas = sorted(os.listdir(ATTACKS_DIR))
    rows = []

    for sha in shas:
        sha_dir = os.path.join(ATTACKS_DIR, sha)
        if not os.path.isdir(sha_dir):
            continue
        patch_path = os.path.join(sha_dir, "attack.patch")
        clean_path = os.path.join(sha_dir, "attack.patch.clean")

        with open(patch_path) as f:
            original = f.read()

        cleaned, removed = clean_patch(original)
        had_corruption = removed > 0

        with open(clean_path, "w") as f:
            f.write(cleaned)

        ok, err = try_apply(clean_path)
        warning = read_warning_md(sha_dir)

        rows.append({
            "sha": sha[:12],
            "full_sha": sha,
            "had_corruption": had_corruption,
            "applies": ok,
            "error": err,
            "warning": warning,
        })
        print(f"  {sha[:12]}  corruption={had_corruption}  applies={ok}  err={err[:80] if err else ''}")

    # Results table
    print("\n" + "=" * 100)
    print(f"{'SHA':14} {'Fence Corruption':18} {'Applies Cleanly':16} Error / Notes")
    print("-" * 100)
    for r in rows:
        corruption = "yes" if r["had_corruption"] else "no"
        applies = "YES" if r["applies"] else "NO"
        err_short = r["error"].replace("\n", " ")[:60] if r["error"] else ""
        print(f"{r['sha']:14} {corruption:18} {applies:16} {err_short}")

    # Candidates
    candidates = [r for r in rows if r["applies"]]
    print(f"\n\nCANDIDATES ({len(candidates)} patches apply cleanly):")
    for c in candidates:
        print(f"  {c['full_sha']}")
        if c["warning"]:
            print(f"    warning: {c['warning'][:200]}")

    return rows


if __name__ == "__main__":
    main()
