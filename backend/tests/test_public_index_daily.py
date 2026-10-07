from datetime import datetime

import httpx
import pytest

from app.plugins.stocksdk import index_daily


def test_index_prices_units_and_window(monkeypatch):
    def get(url, params, timeout):
        assert params['param'].startswith('sh000001,day,')
        return httpx.Response(200, request=httpx.Request('GET', url), json={
            'code': 0, 'data': {'sh000001': {'day': [
                ['2026-09-23','3900','3890','3920','3880','1234'],
                ['2026-09-24','3925.32','3888.37','3930.50','3888.37','438530412'],
            ]}}})
    monkeypatch.setattr(index_daily.httpx, 'get', get)
    df = index_daily.get_daily(['000001.SH'], datetime(2026,9,24), datetime(2026,9,24))
    assert df.height == 1
    assert df['symbol'][0] == '000001.SH'
    assert df['close'][0] == 3888.37
    assert df['volume'][0] == 438530412  # Tencent returns lots, already internal unit
    assert df['amount'][0] is None  # not turnover money


def test_bad_payload_fails_without_fabricating_data(monkeypatch):
    monkeypatch.setattr(index_daily.httpx, 'get', lambda url, **kw: httpx.Response(
        200, request=httpx.Request('GET',url), json={'code':0,'data':{}}))
    with pytest.raises(RuntimeError, match='000001.SH'):
        index_daily.get_daily(['000001.SH'],datetime(2026,9,24),datetime(2026,9,24))


def test_exchange_is_required():
    with pytest.raises(ValueError):
        index_daily.get_daily(['000001'],datetime(2026,9,24),datetime(2026,9,24))


def test_fuyao_index_routes_to_stocksdk_but_stocks_stay(monkeypatch):
    import polars as pl
    from app.services import kline_sync
    from app.data_providers import custom
    seen = []
    class Provider:
        def get_daily(self, symbols, **kwargs):
            return pl.DataFrame({'symbol':symbols,'date':['2026-09-24'],'open':[1.0],'high':[1.0],'low':[1.0],'close':[1.0],'volume':[1.0]})
    monkeypatch.setattr(kline_sync.preferences,'get_daily_data_provider',lambda:'fuyao')
    monkeypatch.setattr(custom,'provider_has_dataset',lambda *args:True)
    monkeypatch.setattr(custom,'get_provider',lambda name: (seen.append(name) or Provider()))
    kline_sync.sync_daily_batch(['000001.SH'],asset_type='index')
    kline_sync.sync_daily_batch(['600519.SH'],asset_type='stock')
    assert seen == ['stocksdk','fuyao']
