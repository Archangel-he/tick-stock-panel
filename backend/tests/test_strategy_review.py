from datetime import date
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.paper import router as paper_router
from app.jobs import daily_pipeline
from app.services import strategy_review


def test_weekly_review_records_baseline_and_oos_without_changing_rule(tmp_path, monkeypatch):
    day = date(2026, 10, 2)  # Friday
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), latest_enriched_date=lambda _asset: day,
    )
    rule = {
        "id": "arule_one", "match_id": "candidate", "trigger_mode": "daily_close",
        "max_positions": 10, "size_mode": "pct_equity", "size_value": 8,
    }
    monkeypatch.setattr(strategy_review.paper, "list_account_ids", lambda _path: ["validation"])
    monkeypatch.setattr(strategy_review.paper, "get_account", lambda *_: {
        "initial_cash": 100_000, "commission_pct": 0.00025,
        "stamp_tax_pct": 0.0005, "slippage_bps": 5,
    })
    monkeypatch.setattr(strategy_review.paper_auto, "load_auto_rules", lambda *_args, **_kw: [rule])
    monkeypatch.setattr(strategy_review.strategy_config, "load_override", lambda *_: {"params": {"threshold": 3}})
    engine = SimpleNamespace(get=lambda _sid: SimpleNamespace(meta={"params": [
        {"id": "threshold", "type": "int", "default": 3, "min": 2, "max": 6, "step": 1},
    ]}))
    calls = []

    def fake_worker(task):
        calls.append(task)
        if task["kind"] == "backtest":
            return {"stats": {"total_return": -0.08, "n_trades": 24}}
        return {"summary": {"n_folds": 2, "compounded_oos_return": -0.03, "consistency": 0.5},
                "folds": [{"best_params": {"threshold": 2}}], "skipped": []}

    monkeypatch.setattr(strategy_review, "run_worker_task", fake_worker)
    assert strategy_review.run_weekly_review(repo, engine, day)["completed"] == 1
    review = strategy_review.load_reviews(tmp_path, "validation")[0]
    assert review["baseline"]["total_return"] == -0.08
    assert review["walkforward"]["summary"]["compounded_oos_return"] == -0.03
    assert review["param_grid"] == {"threshold": [2, 3, 4]}
    assert review["max_exposure_pct"] == 0.8
    assert review["auto_applied"] is False
    assert rule == {"id": "arule_one", "match_id": "candidate", "trigger_mode": "daily_close",
                    "max_positions": 10, "size_mode": "pct_equity", "size_value": 8}
    assert len(calls) == 2
    assert strategy_review.run_weekly_review(repo, engine, day)["completed"] == 0
    assert len(calls) == 2


def test_weekly_review_waits_for_friday_and_current_enriched_data(tmp_path):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_enriched_date=lambda _asset: date(2026, 9, 29),
    )
    assert strategy_review.run_weekly_review(repo, None, date(2026, 9, 29))["skipped"] == "仅周五运行"
    assert strategy_review.run_weekly_review(repo, None, date(2026, 10, 2))["skipped"] == "当日策略数据未就绪"


def test_scheduled_review_runs_only_after_successful_pipeline(monkeypatch):
    calls = []
    state = SimpleNamespace(repo="repo", strategy_engine="engine")
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: state)
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: date(2026, 10, 2))
    monkeypatch.setattr("app.services.mining_schedule.run_weekly_mining", lambda _state: calls.append("mining"))
    monkeypatch.setattr(strategy_review, "run_weekly_review",
                        lambda repo, engine, day: calls.append((repo, engine, day)))
    monkeypatch.setattr(daily_pipeline, "_run_tracked", lambda *_: False)
    daily_pipeline._scheduled_pipeline_task(lambda: None)
    assert calls == []
    monkeypatch.setattr(daily_pipeline, "_run_tracked", lambda *_: True)
    daily_pipeline._scheduled_pipeline_task(lambda: None)
    assert calls == ["mining", ("repo", "engine", date(2026, 10, 2))]


def test_paper_review_endpoint_reads_account_history(tmp_path):
    app = FastAPI()
    app.include_router(paper_router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    response = TestClient(app).get("/api/paper/research_reviews?account=validation")
    assert response.status_code == 200
    assert response.json() == {"reviews": []}
