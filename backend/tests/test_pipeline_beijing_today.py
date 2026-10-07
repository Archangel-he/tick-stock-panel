"""盘后管道的「今天」必须是北京日期, 不能用服务器本地 date.today()。

调度默认 15:35 北京时间。美西主机此时本地日历日仍是昨天: 未修复代码
today_exists 把「北京昨天的日K」当成今天已齐, 窗口右端停在昨天, 当日官方
收盘价永远拉不进来。UTC 主机在北京 00:00-08:00 有同样错位。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.jobs import daily_pipeline
from app.services import data_integrity, instrument_sync, kline_sync

BJ = date(2026, 3, 2)
LOCAL = date(2026, 3, 1)


class _StopError(Exception):
    """日K窗口已经记下, 后面的管道阶段与本 bug 无关。"""


def test_pipeline_daily_window_ends_on_beijing_today(monkeypatch, tmp_path) -> None:
    """数据修正/补拉路径的窗口右端必须是北京今天。

    raising=False: 未修复代码没有调用 cn_today, 钉了也不会被用到,
    仍走 date.today() → 与 2026-03-02 不等。
    """
    captured: list[date] = []

    def fake_batch(universe, repo, capset, start_date=None, end_date=None, on_chunk_done=None):
        captured.append(end_date.date() if hasattr(end_date, "date") else end_date)
        raise _StopError()

    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: BJ, raising=False)
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda data_dir: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda capset, repo=None: ["600000.SH"])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", fake_batch)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: LOCAL,
    )
    capset = SimpleNamespace(has=lambda key: False)

    with pytest.raises(_StopError):
        daily_pipeline.run_now(repo, capset, override_start_date=LOCAL)  # type: ignore[arg-type]

    assert captured, "应发起日K范围拉取"
    assert captured[0] == BJ, f"窗口右端必须是北京日期 {BJ}, 实际 {captured[0]} (服务器本地 {date.today()})"


@pytest.mark.parametrize("factor_exists", [False, True])
def test_pipeline_backfills_missing_factor_history(monkeypatch, tmp_path, factor_exists) -> None:
    first_daily = date(2025, 9, 26)
    latest_daily = date(2026, 9, 24)
    if factor_exists:
        factor = tmp_path / "adj_factor" / "all.parquet"
        factor.parent.mkdir()
        factor.touch()
    captured = []

    def stop_at_factor(*args, start_time=None, **kwargs):
        captured.append(start_time.date())
        raise _StopError()

    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: date(2026, 9, 26))
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda data_dir: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda capset, repo: ["600000.SH"])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *a, **k: [])
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_adj_factor_provider", lambda: "fuyao")
    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", lambda *a, **k: 0)
    monkeypatch.setattr(kline_sync, "sync_adj_factor", stop_at_factor)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: latest_daily,
        earliest_daily_date=lambda: first_daily,
        symbols_lagging=lambda *a, **k: [],
    )
    capset = SimpleNamespace(has=lambda key: False)

    with pytest.raises(_StopError):
        daily_pipeline.run_now(repo, capset)  # type: ignore[arg-type]

    assert captured == [latest_daily if factor_exists else first_daily]
