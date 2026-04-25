from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.demo_kit.attacks import stage_attacks
from app.demo_kit.curation import curate_incidents
from app.models.schemas import Incident


class FakeClaude:
    def __init__(self) -> None:
        self.json_calls = 0
        self.text_calls = 0

    def complete_json(self, **kwargs):
        self.json_calls += 1
        prompt = kwargs["prompt"]
        if "incidents" in prompt:
            payload = json.loads(prompt)
            return {
                "ratings": [
                    {
                        "commit_sha": item["commit_sha"],
                        "clarity_score": 9 if item["commit_sha"] == "a" * 40 else 3,
                        "bug_type": "race condition" if item["commit_sha"] == "a" * 40 else "other",
                        "visceral": item["commit_sha"] == "a" * 40,
                        "juicy_reason": "A tiny race fix with a closed issue.",
                    }
                    for item in payload["incidents"]
                ]
            }
        return {
            "pr_title": "Refactor request timeout handling",
            "pr_description": "Small cleanup with no behavior change.",
            "expected_warning": "Expected warning text.",
        }

    def complete(self, **kwargs):
        self.text_calls += 1
        return "def f():\n    return 2\n"


class DemoKitTests(unittest.TestCase):
    def test_curation_scoring_ranks_clear_focused_closed_visceral_incident(self) -> None:
        now = datetime.now(tz=timezone.utc)
        strong = _incident(
            "a" * 40,
            "fix race when streaming callback closes #123",
            now - timedelta(days=20),
            ["app/stream.py"],
            "+new\n-old\n",
            [123],
        )
        weak = _incident(
            "b" * 40,
            "fix typo",
            now - timedelta(days=1200),
            ["a.py", "b.py", "c.py", "d.py"],
            "\n".join(f"+line{i}" for i in range(250)),
            [],
        )

        scores = curate_incidents(
            repo="owner/repo",
            incidents=[weak, strong],
            claude=FakeClaude(),
            max_incidents=2,
            dry_run=False,
            issue_resolver=lambda _repo, issue: "closed" if issue == 123 else None,
        )

        self.assertEqual([score.incident.commit_sha for score in scores], [strong.commit_sha, weak.commit_sha])
        self.assertTrue(scores[0].issue_closed)
        self.assertTrue(scores[0].focused_fix)
        self.assertTrue(scores[0].visceral)
        self.assertGreater(scores[0].demo_score, scores[1].demo_score)

    def test_stage_attacks_writes_expected_files_and_skips_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            incident = _incident(
                "c" * 40,
                "fix off-by-one retry budget #9",
                datetime.now(tz=timezone.utc),
                ["app/retry.py"],
                "+if attempts <= max_attempts:\n-if attempts < max_attempts:\n",
                [9],
            )
            score = curate_incidents(
                repo="owner/repo",
                incidents=[incident],
                claude=None,
                max_incidents=1,
                dry_run=True,
                issue_resolver=lambda *_args: "closed",
            )[0]

            with (
                patch("app.demo_kit.attacks.ensure_source_clone", lambda repo: tmp_path / "repo"),
                patch("app.demo_kit.attacks.choose_target_file", lambda repo_path, inc: "app/retry.py"),
                patch("app.demo_kit.attacks.read_head_file", lambda repo_path, file_path: "def f():\n    return 1\n"),
            ):
                staged = stage_attacks(
                    repo="owner/repo",
                    scores=[score],
                    output_dir=tmp_path / "demo_kit",
                    claude=FakeClaude(),
                    dry_run=False,
                )
                attack_dir = staged[0]

                self.assertTrue((attack_dir / "metadata.json").exists())
                self.assertTrue((attack_dir / "attack.patch").read_text(encoding="utf-8").startswith("--- a/app/retry.py"))
                self.assertEqual(
                    (attack_dir / "pr_title.txt").read_text(encoding="utf-8").strip(),
                    "Refactor request timeout handling",
                )
                self.assertEqual(
                    (attack_dir / "expected_warning.md").read_text(encoding="utf-8").strip(),
                    "Expected warning text.",
                )

                fake = FakeClaude()
                stage_attacks(
                    repo="owner/repo",
                    scores=[score],
                    output_dir=tmp_path / "demo_kit",
                    claude=fake,
                    dry_run=False,
                )
                self.assertEqual(fake.text_calls, 0)
                self.assertEqual(fake.json_calls, 0)


def _incident(
    sha: str,
    message: str,
    date: datetime,
    files: list[str],
    diff: str,
    issues: list[int],
) -> Incident:
    return Incident(
        commit_sha=sha,
        commit_message=message,
        commit_date=date,
        author="Tester",
        files_changed=files,
        functions_changed=[],
        fix_diff=diff,
        buggy_parent_sha="0" * 40,
        issue_refs=issues,
        symptom_summary=None,
    )


if __name__ == "__main__":
    unittest.main()
