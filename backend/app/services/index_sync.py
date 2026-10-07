"""指数 / ETF 数据同步服务。"""
from __future__ import annotations

import logging
import gc
from collections.abc import Callable
from datetime import datetime, timedelta

import polars as pl

from app.indicators.pipeline import compute_enriched
from app.services import kline_sync, preferences
from app.services.index_const import CORE_INDEX_NAMES, CORE_INDEX_SYMBOLS
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.rate_limits import chunked, min_batch, resolve_limit, sleep_between_batches
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)


def _fetch_instruments_by_type(instrument_type: str, asset_type_label: str) -> pl.DataFrame:
    """核心指数使用产品清单；ETF 暂保留本地已有维表。"""
    if instrument_type == "index":
        from app.data_providers.normalizer import normalize_instruments
        return normalize_instruments([
            {"symbol": symbol, "name": name, "exchange": symbol.split(".")[1]}
            for symbol, name in CORE_INDEX_NAMES.items()
        ], "index", source="core")
    return pl.DataFrame()


def sync_index_instruments(
    repo: KlineRepository,
    pull_index: bool = True,
    pull_etf: bool = True,
) -> int:
    """同步指数 / ETF 标的维表,返回标的总数。

    新版物理分开保存: 指数写 instruments_index, ETF 写 instruments_etf。
    读取层仍兼容旧版 instruments_index 中 asset_type='etf' 的历史数据。
    """
    index_parts: list[pl.DataFrame] = []
    etf_parts: list[pl.DataFrame] = []

    if pull_index:
        index_df = _fetch_instruments_by_type("index", "index")
        if not index_df.is_empty():
            index_parts.append(index_df)
    if pull_etf:
        etf_df = _fetch_instruments_by_type("etf", "etf")
        if not etf_df.is_empty():
            etf_parts.append(etf_df)

    total = 0
    if index_parts:
        index_inst = pl.concat(index_parts, how="diagonal_relaxed").unique(subset=["symbol"], keep="last").sort("symbol")
        if not index_inst.is_empty():
            total += index_inst.height
            existing = repo.get_index_instruments()
            if not existing.is_empty():
                index_inst = pl.concat([existing, index_inst], how="diagonal_relaxed").unique(subset=["symbol"], keep="last")
            repo.save_index_instruments(index_inst)
    if etf_parts:
        etf_inst = pl.concat(etf_parts, how="diagonal_relaxed").unique(subset=["symbol"], keep="last").sort("symbol")
        if not etf_inst.is_empty():
            repo.save_etf_instruments(etf_inst)
            total += etf_inst.height

    if total == 0:
        logger.warning("指数/ETF 标的列表为空(pull_index=%s, pull_etf=%s)", pull_index, pull_etf)
        return 0
    repo.refresh_index_views()
    logger.info("指数/ETF 标的同步完成: %d 只", total)
    return total


def sync_etf_instruments(repo: KlineRepository) -> int:
    """单独同步 ETF 标的维表(返回 ETF 数量)。"""
    etf_df = _fetch_instruments_by_type("etf", "etf")
    if etf_df.is_empty():
        return 0
    repo.save_etf_instruments(etf_df)
    repo.refresh_index_views()
    return etf_df.height


def sync_and_persist_index_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    symbols_override: list[str] | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> int:
    """同步指数/ETF 日K到独立 parquet,并计算 enriched。

    symbols_override 非空时,只拉这些代码(跳过 instruments 表),用于自定义范围。
    否则取 index_instruments 表全量(指数+ETF 合并存储)。
    on_chunk_done(current, total) 每个批次完成后回调。
    """
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return 0

    if symbols_override:
        symbols = sorted(set(s for s in symbols_override if s))
        if not symbols:
            return 0
    else:
        instruments = repo.get_index_instruments()
        if instruments.is_empty():
            sync_index_instruments(repo, pull_index=True, pull_etf=False)
            instruments = repo.get_index_instruments()
        if not instruments.is_empty() and "asset_type" in instruments.columns:
            instruments = instruments.filter(pl.col("asset_type") != "etf")
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return 0
        # 产品固定展示核心指数，避免历史全指数维表触发数百次无关请求。
        symbols = sorted(CORE_INDEX_SYMBOLS)
    limit = resolve_limit(capset, Cap.KLINE_DAILY_BATCH)
    batch_size = min_batch(preferences.get_index_daily_batch_size(), limit)

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    total_rows = 0
    chunks = chunked(symbols, batch_size)
    for i, chunk in enumerate(chunks):
        sleep_between_batches(i, limit.rpm)
        raw = kline_sync.sync_daily_batch(
            chunk,
            count=count,
            batch_size=None,
            start_time=start_time,
            end_time=end_time,
            asset_type="index",
        )
        if raw.is_empty():
            continue

        repo.append_index_daily(raw)
        enriched = compute_enriched(raw, factors=None, instruments=None)
        repo.append_index_enriched(enriched)
        total_rows += raw.height
        logger.info("index/etf daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))
        del raw, enriched
        gc.collect()
    repo.refresh_index_views()
    return total_rows


def _load_etf_factors(repo: KlineRepository) -> pl.DataFrame:
    factor_path = repo.store.data_dir / "adj_factor_etf" / "all.parquet"
    if not factor_path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(factor_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("ETF 复权因子读取失败: %s", e)
        return pl.DataFrame()


def etf_adj_factor_window_start(adj_path, history_start: datetime) -> datetime:
    """ETF 除权拉取窗口起点。

    本地还没有因子文件时与日K历史对齐 (调用方传入 today-365 或最早分区日),
    不要只拉最近 30 天: 拆分落在窗口外则全年日K都不复权。已有文件则从最新
    事件日续拉。
    """
    if not adj_path.exists():
        return history_start
    try:
        max_date = pl.scan_parquet(adj_path).select(pl.col("trade_date").max()).collect().item()
    except Exception as e:
        logger.warning("读取 ETF 除权因子最新日期失败, 回退日K历史起点: %s", e)
        return history_start
    if max_date is None:
        return history_start
    if isinstance(max_date, str):
        stored = datetime.combine(datetime.fromisoformat(max_date).date(), datetime.min.time())
    elif isinstance(max_date, datetime):
        stored = datetime.combine(max_date.date(), datetime.min.time())
    else:
        stored = datetime.combine(max_date, datetime.min.time())
    return stored


def _load_local_etf_daily(repo: KlineRepository, symbols: list[str]) -> pl.DataFrame:
    """读本批 symbol 的完整本地 ETF 日K, 供前复权改写历史价。"""
    daily_dir = repo.store.data_dir / "kline_etf_daily"
    if not daily_dir.exists() or not any(daily_dir.glob("date=*")):
        return pl.DataFrame()
    from app.parquet import scan_daily_parquet
    try:
        return (
            scan_daily_parquet((daily_dir / "**" / "*.parquet").as_posix())
            .filter(pl.col("symbol").is_in(symbols))
            .collect()
        )
    except Exception as e:
        logger.warning("读取本地 ETF 日K失败: %s", e)
        return pl.DataFrame()


def sync_etf_adj_factor(
    symbols: list[str],
    repo: KlineRepository,
    capset: CapabilitySet,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    on_chunk_done=None,
) -> tuple[int, list[str]]:
    """同步 ETF 复权因子；失败由调用方降级为 warning。"""
    return kline_sync.sync_adj_factor(
        symbols,
        repo,
        capset,
        start_time=start_time,
        end_time=end_time,
        on_chunk_done=on_chunk_done,
        asset_type="etf",
    )


def sync_and_persist_etf_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    symbols_override: list[str] | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> int:
    """同步 ETF 日K到独立 kline_etf_* parquet,并计算 ETF enriched。
    on_chunk_done(current, total) 每个批次完成后回调。
    """
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return 0

    if symbols_override:
        symbols = sorted(set(s for s in symbols_override if s))
    else:
        instruments = repo.get_etf_instruments()
        if instruments.is_empty():
            sync_etf_instruments(repo)
            instruments = repo.get_etf_instruments()
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return 0
        symbols = sorted(set(instruments["symbol"].to_list()))
    if not symbols:
        return 0

    limit = resolve_limit(capset, Cap.KLINE_DAILY_BATCH)
    batch_size = min_batch(preferences.get_index_daily_batch_size(), limit)

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    total_rows = 0
    chunks = chunked(symbols, batch_size)
    factors = _load_etf_factors(repo)
    for i, chunk in enumerate(chunks):
        sleep_between_batches(i, limit.rpm)
        raw = kline_sync.sync_daily_batch(
            chunk,
            count=count,
            batch_size=None,
            start_time=start_time,
            end_time=end_time,
            asset_type="etf",
        )
        if raw.is_empty():
            continue

        repo.append_etf_daily(raw)
        batch_factors = factors.filter(pl.col("symbol").is_in(chunk)) if not factors.is_empty() else factors
        # 前复权 ratio = cum/total, 新除权事件会改写全部历史价。只用本次拉取
        # 窗口算 enriched 时, 拆分日前的分区停在未复权价, 日K 留下跳空。
        local = _load_local_etf_daily(repo, chunk)
        hist = local if not local.is_empty() else raw
        # ETF 使用复权和通用技术指标; 不传 instruments, 避免套用 A股涨跌停/连板逻辑。
        enriched = compute_enriched(hist, factors=batch_factors, instruments=None)
        repo.append_etf_enriched(enriched)
        total_rows += raw.height
        logger.info("etf daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))
        del raw, enriched, local, hist
        gc.collect()
    repo.refresh_index_views()
    return total_rows
