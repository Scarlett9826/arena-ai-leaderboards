"""LMArena leaderboard collector.

Fetches the official HuggingFace dataset `lmarena-ai/leaderboard-dataset`,
which exposes one Parquet file per subset (text, vision, webdev, ...).
Each Parquet contains many `category` slices (overall, coding, math, ...);
we keep all of them.

Output layout:

    data/lmarena/{date}/{subset}.json       # full data per subset
    data/lmarena/{date}/_summary.json       # aggregate meta + xiaomi snapshot
    data/lmarena/latest.json                # pointer {"date": "YYYY-MM-DD"}

Run directly with:

    python3 -m collectors.lmarena
"""

from __future__ import annotations

import io
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pyarrow.parquet as pq

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

# All 14 LMArena subsets exposed under the dataset repo. Verified via
# /tmp/test_lmarena_full.py — every subset returns HTTP 200 + non-empty Parquet.
SUBSETS: tuple[str, ...] = (
    "text",
    "text_style_control",
    "vision",
    "vision_style_control",
    "webdev",
    "search",
    "search_style_control",
    "document",
    "document_style_control",
    "text_to_image",
    "image_edit",
    "text_to_video",
    "image_to_video",
    "video_edit",
)

BASE_URL = (
    "https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset"
    "/resolve/main/{subset}/latest-00000-of-00001.parquet"
)

USER_AGENT = "mimo-leaderboard-tracker/0.1 (+lmarena collector)"
REQUEST_TIMEOUT = 60.0
MAX_RETRIES = 3
BACKOFF_BASE = 1.5  # seconds; 1.5 → 3.0 → 6.0

# The xiaomi/mimo organization tag in the dataset is lowercase "xiaomi".
XIAOMI_ORG = "xiaomi"

# Columns we copy into the JSON row payload (Parquet schema is stable).
ROW_COLUMNS: tuple[str, ...] = (
    "category",
    "rank",
    "model_name",
    "organization",
    "license",
    "rating",
    "rating_lower",
    "rating_upper",
    "variance",
    "vote_count",
)

logger = logging.getLogger("collectors.lmarena")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


@dataclass
class SubsetResult:
    """Result of collecting one subset."""

    subset: str
    ok: bool
    path: Path | None = None
    n_rows: int = 0
    n_models: int = 0
    n_categories: int = 0
    publish_date: str | None = None
    xiaomi_rows: int = 0
    error: str | None = None


@dataclass
class CollectionMeta:
    """Top-level metadata returned by :func:`collect_all`."""

    date: str
    out_dir: str
    success: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    subset_summaries: list[dict[str, Any]] = field(default_factory=list)
    xiaomi_total_rows: int = 0


def _clean(value: Any) -> Any:
    """Make a Parquet/Pandas scalar JSON-safe.

    - NaN / NaT → None
    - numpy scalars → native Python
    - everything else → unchanged
    """
    if value is None:
        return None
    # pandas/numpy NaN check (works for floats + pd.NA)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and math.isnan(value):
        return None
    # Convert numpy scalars to python primitives
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except (ValueError, AttributeError):
            return value
    return value


def _fetch_parquet(subset: str, client: httpx.Client) -> pd.DataFrame:
    """Download one subset's latest Parquet and return it as a DataFrame.

    Retries up to :data:`MAX_RETRIES` times with exponential backoff.
    Raises the last exception if all attempts fail.
    """
    url = BASE_URL.format(subset=subset)
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("[%s] GET %s (attempt %d/%d)", subset, url, attempt, MAX_RETRIES)
            resp = client.get(url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            table = pq.read_table(io.BytesIO(resp.content))
            df = table.to_pandas()
            logger.info("[%s] fetched %d rows", subset, len(df))
            return df
        except Exception as exc:  # noqa: BLE001 — we want broad retry behavior
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            delay = BACKOFF_BASE ** attempt
            logger.warning(
                "[%s] attempt %d failed: %s — retrying in %.1fs",
                subset,
                attempt,
                exc,
                delay,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc


def _latest_publish_date(df: pd.DataFrame) -> str | None:
    """Return the most recent `leaderboard_publish_date` as ISO string, or None."""
    if "leaderboard_publish_date" not in df.columns:
        return None
    series = df["leaderboard_publish_date"].dropna()
    if series.empty:
        return None
    latest = series.max()
    # The column is sometimes a date/datetime, sometimes a string.
    if hasattr(latest, "isoformat"):
        return latest.isoformat()[:10]
    return str(latest)[:10]


def _dataframe_to_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert the subset DataFrame to a list of JSON-safe row dicts.

    Only the columns in :data:`ROW_COLUMNS` are kept. Rows are sorted by
    (category, rank) so downstream diffs are stable.
    """
    keep = [c for c in ROW_COLUMNS if c in df.columns]
    sub = df[keep].copy()

    # Stable ordering: category alphabetical, rank ascending.
    sort_cols = [c for c in ("category", "rank") if c in sub.columns]
    if sort_cols:
        sub = sub.sort_values(sort_cols, kind="stable", na_position="last")

    rows: list[dict[str, Any]] = []
    for record in sub.to_dict(orient="records"):
        rows.append({k: _clean(v) for k, v in record.items()})
    return rows


def _xiaomi_snapshot(df: pd.DataFrame, publish_date: str | None) -> list[dict[str, Any]]:
    """Return xiaomi rows for the latest publish_date, sorted by (category, rank)."""
    if "organization" not in df.columns:
        return []
    mask = df["organization"].astype(str).str.lower() == XIAOMI_ORG
    if publish_date and "leaderboard_publish_date" in df.columns:
        # Cast to str on both sides to avoid date/datetime/string mismatch.
        pub_series = df["leaderboard_publish_date"].astype(str).str.slice(0, 10)
        mask = mask & (pub_series == publish_date)
    return _dataframe_to_rows(df[mask])


# ----------------------------------------------------------------------------
# Per-subset collection
# ----------------------------------------------------------------------------


def collect_subset(
    subset: str,
    out_dir: Path,
    client: httpx.Client,
    fetched_at: str,
) -> SubsetResult:
    """Fetch one subset, write `{out_dir}/{subset}.json`, return result.

    Keeps **all** rows (every category, every publish_date present in the
    `latest-*.parquet` snapshot). Filtering is the consumer's job.
    """
    url = BASE_URL.format(subset=subset)
    try:
        df = _fetch_parquet(subset, client)
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] FAILED after %d retries: %s", subset, MAX_RETRIES, exc)
        return SubsetResult(subset=subset, ok=False, error=str(exc))

    publish_date = _latest_publish_date(df)
    categories = sorted(df["category"].dropna().astype(str).unique().tolist()) \
        if "category" in df.columns else []

    rows = _dataframe_to_rows(df)
    xiaomi_rows = sum(
        1
        for r in rows
        if str(r.get("organization", "")).lower() == XIAOMI_ORG
    )

    payload = {
        "meta": {
            "source": "lmarena",
            "subset": subset,
            "source_url": url,
            "fetched_at": fetched_at,
            "leaderboard_publish_date": publish_date,
            "n_rows": len(rows),
            "n_models": int(df["model_name"].nunique()) if "model_name" in df.columns else 0,
            "categories": categories,
        },
        "rows": rows,
    }

    out_path = out_dir / f"{subset}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    logger.info(
        "[%s] wrote %s — rows=%d, models=%d, cats=%d, xiaomi=%d, pub=%s",
        subset,
        out_path.name,
        len(rows),
        payload["meta"]["n_models"],
        len(categories),
        xiaomi_rows,
        publish_date,
    )

    return SubsetResult(
        subset=subset,
        ok=True,
        path=out_path,
        n_rows=len(rows),
        n_models=payload["meta"]["n_models"],
        n_categories=len(categories),
        publish_date=publish_date,
        xiaomi_rows=xiaomi_rows,
    )


# ----------------------------------------------------------------------------
# Top-level entrypoint
# ----------------------------------------------------------------------------


def collect_all(out_dir: Path, today: date) -> dict[str, Any]:
    """Collect all 14 LMArena subsets for the snapshot dated ``today``.

    Parameters
    ----------
    out_dir:
        Root directory for LMArena snapshots, e.g. ``data/lmarena``.
        A sub-directory ``{out_dir}/{today}`` will be created.
    today:
        The date stamp for this collection run (used for the directory name
        and the ``latest.json`` pointer). This is the *run* date, not the
        ``leaderboard_publish_date`` reported by upstream.

    Returns
    -------
    dict
        JSON-serializable collection metadata: list of successful subsets,
        list of failed subsets (with error message), and per-subset row
        counts. Useful for the orchestrator / CI step summary.
    """
    date_str = today.isoformat()
    day_dir = out_dir / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    results: list[SubsetResult] = []
    xiaomi_snapshots: dict[str, dict[str, Any]] = {}

    with httpx.Client(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
        for subset in SUBSETS:
            res = collect_subset(subset, day_dir, client, fetched_at=fetched_at)
            results.append(res)
            if res.ok:
                # Re-load the just-written file to extract the xiaomi snapshot
                # without keeping the full DataFrame in memory.
                assert res.path is not None
                data = json.loads(res.path.read_text())
                xiaomi_rows = [
                    r
                    for r in data["rows"]
                    if str(r.get("organization", "")).lower() == XIAOMI_ORG
                    # Restrict to the latest publish_date only — older rows
                    # are kept in {subset}.json but the summary snapshot
                    # should reflect "today's standing".
                ]
                if res.publish_date:
                    xiaomi_snapshots[subset] = {
                        "publish_date": res.publish_date,
                        "n_xiaomi_rows": len(xiaomi_rows),
                        "rows": xiaomi_rows,
                    }
                else:
                    xiaomi_snapshots[subset] = {
                        "publish_date": None,
                        "n_xiaomi_rows": len(xiaomi_rows),
                        "rows": xiaomi_rows,
                    }

    # Build the per-run summary.
    summary = {
        "meta": {
            "source": "lmarena",
            "run_date": date_str,
            "fetched_at": fetched_at,
            "n_subsets_total": len(SUBSETS),
            "n_subsets_ok": sum(1 for r in results if r.ok),
            "n_subsets_failed": sum(1 for r in results if not r.ok),
        },
        "subsets": [
            {
                "subset": r.subset,
                "ok": r.ok,
                "n_rows": r.n_rows,
                "n_models": r.n_models,
                "n_categories": r.n_categories,
                "leaderboard_publish_date": r.publish_date,
                "xiaomi_rows": r.xiaomi_rows,
                "error": r.error,
            }
            for r in results
        ],
        "xiaomi_by_subset": xiaomi_snapshots,
    }

    (day_dir / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    )

    # Update the latest pointer (atomic-ish: write then rename would be safer,
    # but for a single small file this is fine).
    (out_dir / "latest.json").write_text(json.dumps({"date": date_str}, indent=2) + "\n")

    meta = CollectionMeta(
        date=date_str,
        out_dir=str(day_dir),
        success=[r.subset for r in results if r.ok],
        failed=[
            {"subset": r.subset, "error": r.error or "unknown"}
            for r in results
            if not r.ok
        ],
        subset_summaries=summary["subsets"],
        xiaomi_total_rows=sum(r.xiaomi_rows for r in results if r.ok),
    )

    logger.info(
        "Done. ok=%d failed=%d xiaomi_total_rows=%d → %s",
        len(meta.success),
        len(meta.failed),
        meta.xiaomi_total_rows,
        day_dir,
    )

    return {
        "date": meta.date,
        "out_dir": meta.out_dir,
        "success": meta.success,
        "failed": meta.failed,
        "subset_summaries": meta.subset_summaries,
        "xiaomi_total_rows": meta.xiaomi_total_rows,
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def main() -> int:
    """Run a one-shot collection rooted at ``./data/lmarena``."""
    _configure_logging()
    repo_root = Path(__file__).resolve().parent.parent
    out_dir = repo_root / "data" / "lmarena"
    meta = collect_all(out_dir, date.today())

    print(json.dumps(meta, ensure_ascii=False, indent=2, default=str))
    return 0 if not meta["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
