"""盘后策略结果进入独立模拟账户，不依赖盘中通知。"""
from datetime import date, datetime, time
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.jobs import daily_pipeline
from app.services.screener import ScreenerService
from app.strategy import paper, paper_auto
from app.strategy.engine import StrategyResult
from app.tickflow.repository import DataStore, KlineRepository


def test_daily_close_rule_uses_confirmed_rows_and_does_not_repeat(tmp_path, monkeypatch):
    day = date(2026, 9, 29)
    repo = KlineRepository(DataStore(tmp_path))
    paper.create_account(tmp_path, 100_000, account_id="research")
    rule = paper_auto.create_auto_rule(tmp_path, {
        "name": "盘后测试", "match_kind": "strategy", "match_id": "test_strategy",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 10_000,
        "order_type": "next_open", "cooldown_days": 0,
        "trigger_mode": "daily_close", "max_positions": 1,
    }, account_id="research")
    current = pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH"],
        "raw_close": [10.0, 20.0], "raw_low": [9.8, 19.8],
    })
    result = StrategyResult(as_of=day, strategy_id="test_strategy", rows=[
        {"symbol": "600001.SH", "score": 2.0},
        {"symbol": "600002.SH", "score": 1.0},
    ], total=2, exit_signal_hits=[{"symbol": "600002.SH", "signals": ["exit_1"]}])

    class Engine:
        def has(self, sid):
            return sid == "test_strategy"

        def get(self, sid):
            return SimpleNamespace(meta={"asset_types": ["stock"], "timeframes": ["1d"]},
                                   stop_loss=None, max_hold_days=None)

        def run(self, sid, context, **kwargs):
            return result

    monkeypatch.setattr(ScreenerService, "latest_date", lambda self: day)
    monkeypatch.setattr(ScreenerService, "build_strategy_context", lambda self, *args, **kwargs: SimpleNamespace(current=current))
    monkeypatch.setattr(paper_auto, "cn_now", lambda: datetime.combine(day, time(16), tzinfo=CN_TZ))
    monkeypatch.setattr(paper_auto, "cn_today", lambda: day, raising=False)

    first = paper_auto.run_daily_close(repo, Engine(), day)
    second = paper_auto.run_daily_close(repo, Engine(), day)
    orders = paper.load_orders(tmp_path, "research")
    assert first["created"] == 1
    assert second["created"] == 0
    assert len(orders) == 1
    assert orders[0]["symbol"] == "600001.SH"
    assert orders[0]["qty"] == 1000
    assert orders[0]["order_type"] == "next_open"
    assert orders[0]["source"] == f"auto:{rule['id']}"
    snapshots = paper_auto.load_daily_signals(tmp_path, "research")
    assert snapshots[0]["date"] == "2026-09-29"
    assert snapshots[0]["strategy_id"] == "test_strategy"
    assert snapshots[0]["selected_symbols"] == ["600001.SH", "600002.SH"]
    assert snapshots[0]["order_ids"] == [orders[0]["id"]]
    assert snapshots[0]["exit_symbols"] == []
    assert paper_auto.on_rule_events(tmp_path, [{"source": "strategy", "type": "pool_entry",
        "strategy_id": "test_strategy", "symbol": "600002.SH", "price": 20.0}], "research") == []


def test_daily_close_skips_stale_snapshot(tmp_path, monkeypatch):
    day = date(2026, 9, 29)
    repo = KlineRepository(DataStore(tmp_path))
    paper.create_account(tmp_path, 100_000, account_id="research")
    paper_auto.create_auto_rule(tmp_path, {
        "name": "盘后测试", "match_kind": "strategy", "match_id": "test_strategy",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 10_000,
        "order_type": "next_open", "trigger_mode": "daily_close",
    }, account_id="research")
    monkeypatch.setattr(ScreenerService, "latest_date", lambda self: date(2026, 9, 28))
    monkeypatch.setattr(paper_auto, "cn_today", lambda: day, raising=False)
    summary = paper_auto.run_daily_close(repo, SimpleNamespace(), day)
    assert summary["created"] == 0
    assert paper.load_orders(tmp_path, "research") == []


def test_daily_close_records_strategy_error_for_account(tmp_path, monkeypatch):
    day = date(2026, 9, 29)
    repo = KlineRepository(DataStore(tmp_path))
    paper.create_account(tmp_path, 100_000, account_id="research")
    paper_auto.create_auto_rule(tmp_path, {
        "name": "盘后测试", "match_kind": "strategy", "match_id": "missing_strategy",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 10_000,
        "order_type": "next_open", "trigger_mode": "daily_close",
    }, account_id="research")
    monkeypatch.setattr(ScreenerService, "latest_date", lambda self: day)
    monkeypatch.setattr(paper_auto, "cn_today", lambda: day)
    summary = paper_auto.run_daily_close(repo, SimpleNamespace(has=lambda _sid: False), day)
    assert summary["created"] == 0
    assert summary["errors"][0]["error"] == "策略不存在"
    assert paper_auto.load_daily_signals(tmp_path, "research")[0]["status"] == "failed"
    assert paper.load_orders(tmp_path, "research") == []


def test_daily_close_only_exits_positions_wholly_owned_by_its_rule(tmp_path, monkeypatch):
    positions = {"600001.SH": {"qty": 1100}, "600002.SH": {"qty": 1000}}
    orders = [
        {"id": "a", "source": "auto:one"}, {"id": "b", "source": "manual"},
        {"id": "c", "source": "auto:one"},
    ]
    fills = [
        {"symbol": "600001.SH", "side": "buy", "qty": 1000, "order_id": "a"},
        {"symbol": "600001.SH", "side": "buy", "qty": 100, "order_id": "b"},
        {"symbol": "600002.SH", "side": "buy", "qty": 1000, "order_id": "c"},
    ]
    monkeypatch.setattr(paper, "load_fills", lambda *_: fills)
    assert paper_auto._rule_owned_symbols(tmp_path, "research", "one", positions, orders) == {"600002.SH"}


@pytest.mark.parametrize("exit_signal,stop_loss,max_hold_days", [
    (True, None, 15),
    (False, -0.06, 15),
    (False, None, 1),
])
def test_daily_close_queues_exit_for_owned_position(tmp_path, monkeypatch,
                                                     exit_signal, stop_loss, max_hold_days):
    day = date(2026, 9, 29)
    repo = KlineRepository(DataStore(tmp_path))
    paper.create_account(tmp_path, 100_000, account_id="research")
    rule = paper_auto.create_auto_rule(tmp_path, {
        "name": "盘后测试", "match_kind": "strategy", "match_id": "test_strategy",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 10_000,
        "order_type": "next_open", "trigger_mode": "daily_close",
    }, account_id="research")
    current_day = [date(2026, 9, 28)]
    monkeypatch.setattr(paper, "_now_iso", lambda: datetime.combine(current_day[0], time(16), tzinfo=CN_TZ).isoformat())
    order, err = paper.create_order(tmp_path, "600001.SH", "buy", account_id="research",
                                    qty=1000, order_type="next_open", ref_price=10.0,
                                    source=f"auto:{rule['id']}")
    assert err is None
    paper._fill_order(tmp_path, order, 10.0, "2026-09-28", "research")
    current_day[0] = day
    monkeypatch.setattr(paper, "cn_today", lambda: day)
    monkeypatch.setattr(paper_auto, "cn_today", lambda: day)
    monkeypatch.setattr(paper_auto, "_local_trading_days", lambda data_dir: [date(2026, 9, 28), day])
    monkeypatch.setattr(ScreenerService, "latest_date", lambda self: day)
    current = pl.DataFrame({"symbol": ["600001.SH"], "raw_close": [9.5], "raw_low": [9.3]})
    monkeypatch.setattr(ScreenerService, "build_strategy_context", lambda self, *args, **kwargs: SimpleNamespace(current=current))
    result = StrategyResult(as_of=day, strategy_id="test_strategy", rows=[], total=0,
                            exit_signal_hits=[{"symbol": "600001.SH", "signals": ["exit_1"]}]
                            if exit_signal else [])

    class Engine:
        def has(self, sid):
            return True

        def get(self, sid):
            return SimpleNamespace(meta={"asset_types": ["stock"], "timeframes": ["1d"]},
                                   stop_loss=stop_loss, max_hold_days=max_hold_days)

        def run(self, sid, context, **kwargs):
            return result

    summary = paper_auto.run_daily_close(repo, Engine(), day)
    sell_orders = [o for o in paper.load_orders(tmp_path, "research") if o["side"] == "sell"]
    assert summary["created"] == 1
    assert len(sell_orders) == 1
    assert sell_orders[0]["symbol"] == "600001.SH"
    assert sell_orders[0]["qty"] == 1000
    assert sell_orders[0]["order_type"] == "next_open"
    assert paper_auto.load_daily_signals(tmp_path, "research")[0]["exit_symbols"] == ["600001.SH"]


def test_pipeline_runs_daily_follow_after_refresh(monkeypatch):
    refreshed = []
    repo = SimpleNamespace(refresh_cache=lambda: refreshed.append(True))
    monkeypatch.setattr(daily_pipeline, "run_now", lambda *args, **kwargs: {"daily_days": 1})

    def follow(repo_arg, engine_arg, day_arg):
        assert repo_arg is repo
        assert engine_arg == "engine"
        assert refreshed == [True]
        return {"created": 1}

    monkeypatch.setattr(paper_auto, "run_daily_close", follow)
    result = daily_pipeline.run_with_daily_follow(repo, None, strategy_engine="engine")
    assert result["paper_daily_follow"] == {"created": 1}


def test_daily_close_respects_cooldown_after_cancelled_order(tmp_path, monkeypatch):
    day = date(2026, 9, 30)
    repo = KlineRepository(DataStore(tmp_path))
    paper.create_account(tmp_path, 100_000, account_id="research")
    rule = paper_auto.create_auto_rule(tmp_path, {
        "name": "盘后测试", "match_kind": "strategy", "match_id": "test_strategy",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 10_000,
        "order_type": "next_open", "trigger_mode": "daily_close", "cooldown_days": 3,
    }, account_id="research")
    monkeypatch.setattr(paper, "_now_iso", lambda: "2026-09-29T16:00:00+08:00")
    old, err = paper.create_order(tmp_path, "600001.SH", "buy", account_id="research", qty=1000,
                                  order_type="next_open", ref_price=10.0, source=f"auto:{rule['id']}")
    assert err is None
    paper.cancel_order(tmp_path, old["id"], "research")
    monkeypatch.setattr(paper_auto, "cn_today", lambda: day)
    monkeypatch.setattr(paper_auto, "cn_now", lambda: datetime.combine(day, time(16), tzinfo=CN_TZ))
    monkeypatch.setattr(paper_auto, "_local_trading_days", lambda data_dir: [date(2026, 9, 29), day])
    monkeypatch.setattr(ScreenerService, "latest_date", lambda self: day)
    current = pl.DataFrame({"symbol": ["600001.SH"], "raw_close": [10.0], "raw_low": [9.9]})
    monkeypatch.setattr(ScreenerService, "build_strategy_context", lambda self, *args, **kwargs: SimpleNamespace(current=current))

    class Engine:
        def has(self, sid):
            return True

        def get(self, sid):
            return SimpleNamespace(meta={"asset_types": ["stock"], "timeframes": ["1d"]},
                                   stop_loss=None, max_hold_days=None)

        def run(self, sid, context, **kwargs):
            return StrategyResult(as_of=day, strategy_id=sid, rows=[{"symbol": "600001.SH"}], total=1)

    summary = paper_auto.run_daily_close(repo, Engine(), day)
    assert summary["created"] == 0
    assert len(paper.load_orders(tmp_path, "research")) == 1
