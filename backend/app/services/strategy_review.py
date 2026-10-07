"""Weekly paper-strategy review: frozen live parameters, bounded historical search."""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.walkforward import WalkForwardConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.services.fs_utils import atomic_write_text
from app.services.heavy_job_limiter import shared_heavy_job_limiter
from app.strategy import config as strategy_config
from app.strategy import paper, paper_auto

logger = logging.getLogger(__name__)


def _candidate_grid(meta: list[dict], params: dict) -> dict:
    """Search one declared numeric parameter at its current value and adjacent steps."""
    for item in meta:
        if item.get("type") not in ("int", "float") or not item.get("step"):
            continue
        key = item["id"]
        center = float(params.get(key, item.get("default", 0)))
        step = float(item["step"])
        lo = float(item.get("min", center))
        hi = float(item.get("max", center))
        values = sorted({round(max(lo, min(hi, center + offset * step)), 10)
                         for offset in (-1, 0, 1)})
        if item["type"] == "int":
            values = sorted({int(round(value)) for value in values})
        if len(values) > 1:
            return {key: values}
    return {}


def _review_path(data_dir: Path, account_id: str, rule_id: str, as_of: date) -> Path:
    year, week, _ = as_of.isocalendar()
    return paper._root(data_dir, account_id) / "research_reviews" / f"{year}-W{week:02d}_{rule_id}.json"


def load_reviews(data_dir: Path, account_id: str, limit: int = 20) -> list[dict]:
    root = paper._root(data_dir, account_id) / "research_reviews"
    reviews = []
    for path in sorted(root.glob("*.json"), reverse=True)[:max(0, min(limit, 100))]:
        try:
            reviews.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            logger.warning("strategy review read failed %s: %s", path.name, exc)
    return reviews


def run_weekly_review(repo, engine, as_of: date) -> dict:
    """On Friday after a successful daily pipeline, record one review per enabled rule.

    Search results are suggestions only. The paper rule keeps its frozen parameters.
    """
    summary = {"as_of": as_of.isoformat(), "completed": 0, "failed": 0, "skipped": None}
    if as_of.weekday() != 4:
        summary["skipped"] = "仅周五运行"
        return summary
    if engine is None or repo.latest_enriched_date("stock") != as_of:
        summary["skipped"] = "当日策略数据未就绪"
        return summary
    data_dir = repo.store.data_dir
    for account_id in paper.list_account_ids(data_dir):
        account = paper.get_account(data_dir, account_id)
        if account is None:
            continue
        rules = [rule for rule in paper_auto.load_auto_rules(data_dir, account_id, enabled_only=True)
                 if rule["trigger_mode"] == "daily_close"]
        for rule in rules:
            path = _review_path(data_dir, account_id, rule["id"], as_of)
            if path.exists():
                try:
                    if json.loads(path.read_text(encoding="utf-8")).get("status") == "completed":
                        continue
                except (OSError, ValueError):
                    pass
            report = {
                "date": as_of.isoformat(), "account_id": account_id,
                "rule_id": rule["id"], "strategy_id": rule["match_id"],
                "status": "failed", "source": "scheduled", "auto_applied": False,
            }
            try:
                strategy = engine.get(rule["match_id"])
                overrides = strategy_config.load_override(data_dir, rule["match_id"])
                params = dict(overrides.get("params") or {})
                start = as_of - timedelta(days=180)
                if rule["size_mode"] == "pct_equity":
                    max_exposure = min(1.0, rule["size_value"] * rule["max_positions"] / 100.0)
                else:
                    max_exposure = min(
                        1.0, rule["size_value"] * rule["max_positions"] / account["initial_cash"],
                    )
                kwargs = {
                    "matching": "open_t+1", "max_positions": rule["max_positions"],
                    "max_exposure_pct": max_exposure,
                    "initial_capital": account["initial_cash"],
                    "commission_pct": account["commission_pct"],
                    "stamp_tax_pct": account["stamp_tax_pct"],
                    "slippage_bps": account["slippage_bps"],
                }
                report.update({
                    "start": start.isoformat(), "params": params, "overrides": overrides,
                    "max_exposure_pct": max_exposure,
                    "execution_note": "回测与模拟盘均按次日开盘成交；盘后止损检查不能复现盘中触价即成交。",
                })
                baseline = StrategyBacktestConfig(
                    strategy_id=rule["match_id"], symbols=None, start=start, end=as_of,
                    params=params, overrides=overrides or None, **kwargs,
                )
                with shared_heavy_job_limiter.slot("normal"):
                    result = run_worker_task(make_worker_task("backtest", data_dir, baseline))
                if result.get("error"):
                    raise RuntimeError(result["error"])
                report["baseline"] = {key: result.get("stats", {}).get(key)
                                      for key in ("total_return", "max_drawdown", "n_trades", "sharpe")}
                grid = _candidate_grid(strategy.meta.get("params", []), params)
                if grid:
                    report["param_grid"] = grid
                    config = WalkForwardConfig(
                        strategy_id=rule["match_id"], symbols=None, start=start, end=as_of,
                        param_grid=grid, objective="total_return", train_days=90,
                        test_days=30, step_days=30, max_workers=1,
                        base_params=params, overrides=overrides or None, backtest_kwargs=kwargs,
                    )
                    with shared_heavy_job_limiter.slot("normal"):
                        wf = run_worker_task(make_worker_task("walkforward", data_dir, config))
                    report["walkforward"] = {
                        "summary": wf.get("summary"), "folds": wf.get("folds"),
                        "skipped_folds": wf.get("skipped"),
                    }
                report["status"] = "completed"
                summary["completed"] += 1
            except Exception as exc:
                report["error"] = str(exc)
                summary["failed"] += 1
                logger.exception("weekly strategy review failed: %s", rule["id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return summary
