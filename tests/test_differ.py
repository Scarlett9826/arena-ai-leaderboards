"""Self-contained sanity tests for analysis.differ + analysis.issue_writer.

Run::

    python tests/test_differ.py

Uses ``unittest`` from the stdlib only — no pytest required. Builds fake
snapshot data under a tempdir mirroring the real layout::

    {tmp}/data/lmarena/{date}/text.json
    {tmp}/data/artificial_analysis/{date}/llms.json
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis import differ  # noqa: E402
from analysis import issue_writer  # noqa: E402


# ---------------------------------------------------------------------------
# Fake data builders
# ---------------------------------------------------------------------------
def _lm_row(rank: int, model_name: str, organization: str, rating: float,
            category: str = "overall", license_: str = "open",
            votes: int = 10000) -> dict:
    return {
        "category": category,
        "rank": rank,
        "model_name": model_name,
        "organization": organization,
        "license": license_,
        "rating": rating,
        "rating_lower": rating - 5,
        "rating_upper": rating + 5,
        "variance": 4.0,
        "vote_count": votes,
    }


def _lmarena_text(rows: list[dict], fetched_at: str, subset: str = "text") -> dict:
    return {
        "meta": {
            "source": "lmarena",
            "subset": subset,
            "leaderboard_publish_date": fetched_at[:10],
            "fetched_at": fetched_at,
            "row_count": len(rows),
        },
        "rows": rows,
    }


def _aa_model(model_id: str, name: str, creator: str, *,
              ii_rank: int | None, ii_score: float | None,
              coding_rank: int | None = None, coding_score: float | None = None) -> dict:
    ranks: dict[str, int] = {}
    if ii_rank is not None:
        ranks["intelligence_index"] = ii_rank
    if coding_rank is not None:
        ranks["coding_index"] = coding_rank
    evals: dict[str, float] = {}
    if ii_score is not None:
        evals["artificial_analysis_intelligence_index"] = ii_score
    if coding_score is not None:
        evals["artificial_analysis_coding_index"] = coding_score
    return {
        "id": model_id,
        "slug": model_id,
        "name": name,
        "creator_name": creator,
        "creator_slug": creator.lower(),
        "evaluations": evals,
        "speed": {},
        "pricing": {},
        "ranks": ranks,
    }


def _aa_llms(models: list[dict], fetched_at: str) -> dict:
    return {
        "meta": {"source": "artificial_analysis", "fetched_at": fetched_at},
        "models": models,
    }


def _make_snapshot(root: Path, date_str: str, lm_rows: list[dict], aa_models: list[dict]) -> None:
    lm_dir = root / "lmarena" / date_str
    aa_dir = root / "artificial_analysis" / date_str
    lm_dir.mkdir(parents=True, exist_ok=True)
    aa_dir.mkdir(parents=True, exist_ok=True)
    (lm_dir / "text.json").write_text(
        json.dumps(_lmarena_text(lm_rows, f"{date_str}T00:00:00Z")),
        encoding="utf-8",
    )
    (aa_dir / "llms.json").write_text(
        json.dumps(_aa_llms(aa_models, f"{date_str}T00:00:00Z")),
        encoding="utf-8",
    )


def build_fake_world(root: Path, today: str, baseline: str) -> None:
    # --- BASELINE LMArena (text/overall) ---
    baseline_lm = [
        _lm_row(12, "mimo-v2.5-pro", "xiaomi", 1430.0, votes=50000),
        _lm_row(45, "mimo-mini", "xiaomi", 1350.0, votes=20000),
        _lm_row(3,  "claude-4-opus", "anthropic", 1500.0, license_="proprietary", votes=90000),
        _lm_row(1,  "gpt-5", "openai", 1520.0, license_="proprietary", votes=100000),
        _lm_row(50, "kimi-old", "moonshot", 1300.0, license_="proprietary", votes=5000),
        # non-watchlist filler
        _lm_row(30, "obscure-research-model", "someone", 1380.0, votes=8000),
    ]
    baseline_aa = [
        _aa_model("mimo-v2.5-pro", "MiMo v2.5 Pro", "Xiaomi",
                  ii_rank=5, ii_score=70.5),
        _aa_model("gpt-5", "GPT-5", "OpenAI",
                  ii_rank=1, ii_score=82.0),
    ]

    # --- TODAY ---
    today_lm = [
        # MiMo big jump: 12 → 8 (rank_delta=-4, crosses Top10)
        _lm_row(8, "mimo-v2.5-pro", "xiaomi", 1465.0, votes=55000),
        # MiMo mini tiny move: 45 → 44
        _lm_row(44, "mimo-mini", "xiaomi", 1351.0, votes=21000),
        # NEW MiMo model
        _lm_row(22, "mimo-v3-preview", "xiaomi", 1442.0, votes=12000),
        # Claude tanks: 3 → 11 (delta=+8, crosses Top5 and Top10)
        _lm_row(11, "claude-4-opus", "anthropic", 1455.0, license_="proprietary", votes=91000),
        # GPT-5 unchanged
        _lm_row(1, "gpt-5", "openai", 1520.0, license_="proprietary", votes=100000),
        # kimi-old DROPPED (not present)
        # non-watchlist unchanged
        _lm_row(30, "obscure-research-model", "someone", 1380.0, votes=8000),
    ]
    today_aa = [
        # MiMo AA: 5 → 4, score 70.5 → 71.0 (+0.71%, below 3*1=3%)
        _aa_model("mimo-v2.5-pro", "MiMo v2.5 Pro", "Xiaomi",
                  ii_rank=4, ii_score=71.0),
        _aa_model("gpt-5", "GPT-5", "OpenAI",
                  ii_rank=1, ii_score=82.5),
    ]

    _make_snapshot(root, baseline, baseline_lm, baseline_aa)
    _make_snapshot(root, today, today_lm, today_aa)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class DifferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mimo-differ-"))
        self.data = self.tmp / "data"
        self.today = "2026-06-30"
        self.baseline = "2026-06-25"
        build_fake_world(self.data, today=self.today, baseline=self.baseline)
        self.watchlist = ROOT / "analysis" / "watchlist.yaml"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- core differ unit tests ----------------------------------------
    def test_happy_path(self) -> None:
        rc = differ.run(
            data_root=self.data,
            watchlist_path=self.watchlist,
            today=self.today,
            baseline=self.baseline,
        )
        self.assertEqual(rc, 0)
        out_json = self.data / "alerts" / f"{self.today}.json"
        out_md = self.data / "alerts" / f"{self.today}.md"
        self.assertTrue(out_json.is_file())
        self.assertTrue(out_md.is_file())

        report = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(report["today"], self.today)
        self.assertEqual(report["baseline"], self.baseline)
        self.assertFalse(report["is_first_snapshot"])

        changes = report["changes"]
        # Index by model_id+source for assertions.
        by_id: dict[tuple[str, str], dict] = {(c["source"], c["model_id"]): c for c in changes}

        # MiMo big jump on LMArena (real schema => "lmarena::xiaomi::mimo-v2.5-pro")
        mimo_lm = by_id[("lmarena", "lmarena::xiaomi::mimo-v2.5-pro")]
        self.assertTrue(mimo_lm["is_primary"])
        self.assertEqual(mimo_lm["old_rank"], 12)
        self.assertEqual(mimo_lm["new_rank"], 8)
        self.assertEqual(mimo_lm["rank_delta"], -4)
        self.assertIn(10, mimo_lm["crossed_milestones"])
        self.assertEqual(mimo_lm["severity"], "ALERT", msg=mimo_lm["reasons"])

        # MiMo mini barely moved → primary min sev WARN
        mini = by_id[("lmarena", "lmarena::xiaomi::mimo-mini")]
        self.assertEqual(mini["rank_delta"], -1)
        self.assertEqual(mini["severity"], "WARN")
        self.assertTrue(mini["is_primary"])

        # NEW MiMo model
        new_mimo = by_id[("lmarena", "lmarena::xiaomi::mimo-v3-preview")]
        self.assertEqual(new_mimo["kind"], "new")
        self.assertIsNone(new_mimo["old_rank"])
        self.assertEqual(new_mimo["new_rank"], 22)
        self.assertTrue(new_mimo["is_primary"])
        self.assertIn(new_mimo["severity"], ("WARN", "ALERT"))

        # Claude big drop → ALERT (rank delta +8)
        claude = by_id[("lmarena", "lmarena::anthropic::claude-4-opus")]
        self.assertEqual(claude["rank_delta"], 8)
        self.assertEqual(claude["severity"], "ALERT")
        self.assertTrue(claude["is_competitor"])

        # Kimi dropped
        kimi = by_id[("lmarena", "lmarena::moonshot::kimi-old")]
        self.assertEqual(kimi["kind"], "dropped")
        self.assertIsNone(kimi["new_rank"])
        self.assertTrue(kimi["is_competitor"])

        # Non-watchlist obscure model must not appear
        self.assertNotIn(("lmarena", "lmarena::someone::obscure-research-model"), by_id)

        # MiMo on AA also present
        mimo_aa = by_id[("artificial_analysis", "aa::mimo-v2.5-pro")]
        self.assertEqual(mimo_aa["rank_delta"], -1)
        self.assertTrue(mimo_aa["is_primary"])

    def test_first_snapshot_no_baseline(self) -> None:
        # Wipe the baseline and re-run; no earlier date exists.
        shutil.rmtree(self.data / "lmarena" / self.baseline)
        shutil.rmtree(self.data / "artificial_analysis" / self.baseline)
        rc = differ.run(
            data_root=self.data,
            watchlist_path=self.watchlist,
            today=self.today,
            baseline=None,
        )
        self.assertEqual(rc, 0)
        report = json.loads((self.data / "alerts" / f"{self.today}.json").read_text())
        self.assertTrue(report["is_first_snapshot"])
        self.assertEqual(report["changes"], [])
        md = (self.data / "alerts" / f"{self.today}.md").read_text()
        self.assertIn("首次快照", md)

    def test_milestones_helper(self) -> None:
        # 12 → 8 crosses Top10 (and only Top10)
        self.assertEqual(differ._milestones_crossed(12, 8, [1, 3, 5, 10, 20, 50]), [10])
        # 3 → 11 (rank=3 was IN Top3, now #11 is out): crosses Top3, Top5, Top10.
        # Rule: milestone M is crossed iff lo <= M < hi, where lo=min(old,new),
        # hi=max(old,new). For 3→11: lo=3, hi=11 → 3, 5, 10 all in [3,11).
        self.assertEqual(
            differ._milestones_crossed(3, 11, [1, 3, 5, 10, 20, 50]),
            [3, 5, 10],
        )
        # New entry at #4 — counts as crossing every milestone it sits at or above.
        crossed = differ._milestones_crossed(None, 4, [1, 3, 5, 10, 20, 50])
        self.assertEqual(crossed, [5, 10, 20, 50])
        # 11 → 9: enters Top10 (lo=9, hi=11; 10 in [9,11) → yes)
        self.assertEqual(differ._milestones_crossed(11, 9, [10]), [10])
        # 9 → 11: leaves Top10
        self.assertEqual(differ._milestones_crossed(9, 11, [10]), [10])
        # No crossing
        self.assertEqual(differ._milestones_crossed(50, 49, [10, 20]), [])
        # Dropped (old=50, new=None) -> dropped from board; helper treats it as
        # "sitting at old rank" and reports milestones at/above it.
        dropped = differ._milestones_crossed(50, None, [10, 20, 50, 100])
        self.assertEqual(dropped, [50, 100])

    def test_no_changes(self) -> None:
        # Replace today with an exact copy of baseline.
        shutil.rmtree(self.data / "lmarena" / self.today)
        shutil.rmtree(self.data / "artificial_analysis" / self.today)
        # Copy baseline → today
        shutil.copytree(self.data / "lmarena" / self.baseline,
                        self.data / "lmarena" / self.today)
        shutil.copytree(self.data / "artificial_analysis" / self.baseline,
                        self.data / "artificial_analysis" / self.today)
        rc = differ.run(self.data, self.watchlist, self.today, self.baseline)
        self.assertEqual(rc, 0)
        report = json.loads((self.data / "alerts" / f"{self.today}.json").read_text())
        self.assertEqual(report["changes"], [])

    # --- issue_writer integration -------------------------------------
    def test_issue_writer(self) -> None:
        differ.run(self.data, self.watchlist, self.today, self.baseline)
        out_dir = self.tmp / "out"
        rc = issue_writer.run(self.today, self.data / "alerts", out_dir)
        self.assertEqual(rc, 0)
        title = (out_dir / "issue_title.txt").read_text().strip()
        body = (out_dir / "issue_body.md").read_text()
        labels = (out_dir / "issue_labels.txt").read_text().strip()

        # Severity should be ALERT (claude moved 8, mimo crossed Top10)
        self.assertIn("🚨", title)
        self.assertIn(self.today, title)
        self.assertIn("severity:alert", labels)
        self.assertIn("mimo-alert", labels)
        self.assertIn("MiMo 系列变动", body)
        self.assertIn("TL;DR", body)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------
class CliSmokeTests(unittest.TestCase):
    def test_differ_cli(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="mimo-cli-"))
        try:
            data = tmp / "data"
            build_fake_world(data, "2026-06-30", "2026-06-25")
            result = subprocess.run(
                [
                    sys.executable, "-m", "analysis.differ",
                    "--data-root", str(data),
                    "--today", "2026-06-30",
                    "--baseline-date", "2026-06-25",
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=True,
            )
            out = result.stdout
            self.assertIn("has_changes=true", out)
            self.assertIn("severity=ALERT", out)
            self.assertIn("summary_path=", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
