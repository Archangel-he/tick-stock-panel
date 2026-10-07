"""腾讯公开指数日线。股票仍走 stock-sdk，指数使用独立端点避免同码歧义。

接口字段顺序 date/open/close/high/low/volume，volume 为手；未提供成交额。
参考 AKShare index_stock_zh.stock_zh_index_daily_tx 的接口契约。
"""
from datetime import date, datetime, timedelta
import logging
import math
import re

import httpx
import polars as pl

from app.data_providers.normalizer import normalize_daily

logger = logging.getLogger(__name__)
_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'


def get_daily(symbols, start_time, end_time, on_chunk_done=None):
    end = (end_time or datetime.now()).date()
    start = (start_time or datetime.combine(end - timedelta(days=365), datetime.min.time())).date()
    rows = []
    for i, symbol in enumerate(symbols):
        if not re.fullmatch(r'\d{6}\.(SH|SZ)', symbol):
            raise ValueError(f'指数代码必须明确交易所: {symbol}')
        code, exchange = symbol.split('.')
        key = exchange.lower() + code
        cursor = start
        while cursor <= end:
            stop = min(cursor + timedelta(days=365), end)
            response = httpx.get(_URL, params={'param':f'{key},day,{cursor},{stop},640,'}, timeout=20)
            response.raise_for_status()
            payload = response.json()
            data = (payload.get('data') or {}).get(key)
            if payload.get('code') not in (0, '0') or not isinstance(data, dict) or not isinstance(data.get('day'), list):
                raise RuntimeError(f'腾讯指数日K响应不可用: {symbol}')
            for bar in data['day']:
                day = date.fromisoformat(bar[0])
                if not cursor <= day <= stop:
                    continue
                op, close, high, low, volume = map(float, bar[1:6])
                if not all(math.isfinite(v) for v in (op, close, high, low, volume)) or not (0 < low <= min(op, close) <= max(op, close) <= high) or volume < 0:
                    raise RuntimeError(f'腾讯指数日K价格或成交量异常: {symbol} {day}')
                rows.append(dict(symbol=symbol, date=day, open=op, close=close, high=high, low=low, volume=volume, amount=None))
            cursor = stop + timedelta(days=1)
        if on_chunk_done:
            on_chunk_done(i + 1, len(symbols))
    logger.info('指数日K via 腾讯公开接口: %d symbols, %d rows', len(symbols), len(rows))
    return normalize_daily(rows, source='stocksdk/tencent-index').unique(subset=['symbol','date']).sort(['symbol','date']) if rows else pl.DataFrame()
