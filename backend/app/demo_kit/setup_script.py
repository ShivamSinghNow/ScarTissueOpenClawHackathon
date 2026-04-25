from __future__ import annotations

from pathlib import Path


def write_setup_script(output_dir: Path, repo: str) -> Path:
    script = output_dir / "setup_demo.sh"
    script.write_text(_script_body(repo), encoding="utf-8")
    script.chmod(0o755)
    print(f"[demo-kit] Wrote setup script to {script}.")
    return script


def _script_body(repo: str) -> str:
    owner, name = repo.split("/", 1)
    safe_name = f"{owner}_{name}"
    return f"""#!/usr/bin/env bash
set -euo pipefail

TARGET_REPO="{repo}"
DEMO_ROOT="/tmp/scartissue_demo"
CLONE_DIR="$DEMO_ROOT/{safe_name}"
ATTACKS_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)/attacks"
FORK_REMOTE="${{1:-}}"
BASE_BRANCH="${{BASE_BRANCH:-main}}"

mkdir -p "$DEMO_ROOT"

if [[ ! -d "$CLONE_DIR/.git" ]]; then
  echo "[setup-demo] Cloning https://github.com/$TARGET_REPO.git to $CLONE_DIR"
  git clone "https://github.com/$TARGET_REPO.git" "$CLONE_DIR"
else
  echo "[setup-demo] Reusing $CLONE_DIR"
fi

cd "$CLONE_DIR"
git fetch origin --prune
if git show-ref --verify --quiet "refs/remotes/origin/$BASE_BRANCH"; then
  git checkout "$BASE_BRANCH"
else
  BASE_BRANCH="$(git remote show origin | awk '/HEAD branch/ {{print $NF}}')"
  git checkout "$BASE_BRANCH"
fi
git pull --ff-only origin "$BASE_BRANCH" || true

if [[ -n "$FORK_REMOTE" ]]; then
  if git remote get-url demo-fork >/dev/null 2>&1; then
    git remote set-url demo-fork "$FORK_REMOTE"
  else
    git remote add demo-fork "$FORK_REMOTE"
  fi
fi

for attack in "$ATTACKS_DIR"/*; do
  [[ -d "$attack" ]] || continue
  sha="$(basename "$attack")"
  short="${{sha:0:12}}"
  branch="demo/$short"
  title="$(tr -d '\\r' < "$attack/pr_title.txt" | head -n 1)"
  echo "[setup-demo] Staging $branch: $title"
  git checkout "$BASE_BRANCH"
  git branch -D "$branch" >/dev/null 2>&1 || true
  git checkout -b "$branch"
  git apply --index "$attack/attack.patch"
  git commit -m "$title"

  if [[ -n "$FORK_REMOTE" && -n "${{GITHUB_TOKEN:-}}" ]]; then
    git push demo-fork "$branch" --force
    if command -v gh >/dev/null 2>&1; then
      body_file="$attack/pr_description.md"
      gh pr create --repo "$TARGET_REPO" --head "$branch" --base "$BASE_BRANCH" --title "$title" --body-file "$body_file" || true
    else
      echo "[setup-demo] gh not found; pushed branch but did not open PR."
    fi
  fi
done

echo "[setup-demo] Demo branches are ready in $CLONE_DIR"
"""

