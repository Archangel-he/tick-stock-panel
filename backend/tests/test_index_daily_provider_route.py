"""指数/ETF 日K 通过当前 provider 获取; 缺数据时不回退已移除的 SDK。"""
from datetime import date, datetime

import polars as pl
import pytest

from app.services import index_sync, kline_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


@pytest.mark.parametrize("asset_type", ["index", "etf"])
def test_asset_daily_uses_configured_provider(monkeypatch, asset_type):
    seen = []
    daily = pl.DataFrame({
        "symbol": ["000001.SH"], "date": [date(2026, 9, 24)],
        "open": [3900.0], "high": [3930.0], "low": [3888.0],
        "close": [3888.37], "volume": [100.0], "amount": [1000.0],
    })

    class Provider:
        def get_daily(self, symbols, start_time, end_time, asset_type, on_chunk_done=None):
            seen.append((symbols, asset_type))
            return daily

    from app.data_providers import custom
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "sample")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom, "get_provider", lambda name: Provider())
    monkeypatch.setattr(kline_sync, "get_client", lambda: pytest.fail("must not call TickFlow"))

    result = kline_sync.sync_daily_batch(
        ["000001.SH"], start_time=datetime(2026, 9, 24),
        end_time=datetime(2026, 9, 26), asset_type=asset_type,
    )
    assert seen == [(["000001.SH"], asset_type)]
    assert result["close"].to_list() == [3888.37]


def test_asset_daily_empty_is_explicit_failure(monkeypatch):
    from app.data_providers import custom
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "fuyao")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom, "get_provider", lambda name: type("Provider", (), {
        "get_daily": lambda self, *a, **k: pl.DataFrame(),
    })())
    monkeypatch.setattr(kline_sync, "get_client", lambda: pytest.fail("must not call TickFlow"))

    with pytest.raises(RuntimeError, match=r"stocksdk.*指数"):
        kline_sync.sync_daily_batch(["000001.SH"], asset_type="index")


@pytest.mark.parametrize("asset_type", ["index", "etf"])
def test_index_etf_sync_passes_asset_type(monkeypatch, tmp_path, asset_type):
    class StopError(Exception):
        pass

    def capture(*args, **kwargs):
        assert kwargs["asset_type"] == asset_type
        raise StopError()

    monkeypatch.setattr(kline_sync, "sync_daily_batch", capture)
    instruments = pl.DataFrame({"symbol": ["000001.SH"], "asset_type": [asset_type]})
    repo = type("Repo", (), {
        "store": type("Store", (), {"data_dir": tmp_path})(),
        "get_index_instruments": lambda self: instruments,
        "get_etf_instruments": lambda self: instruments,
    })()
    capset = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=50, rpm=30)})
    sync = (index_sync.sync_and_persist_index_daily if asset_type == "index"
            else index_sync.sync_and_persist_etf_daily)
    with pytest.raises(StopError):
        sync(repo, capset)
