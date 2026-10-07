"""自动跟单规则域 — 监控事件触发模拟盘自动下单 (V2)。

规则匹配: 事件 source=="strategy" 且 strategy_id 相等 (跟策略), 或 rule_id 相等
(跟任意监控规则, 含信号/价格规则)。仓位: 固定金额 或 账户权益百分比。
冷却: 同规则同 symbol 最近一次自动下单后 N 个交易日内不再触发 (按订单
created_at 日期判断, 无独立状态文件 — 可由订单列表重放推导)。

规则按账户存放 (accounts/{account_id}/auto_rules/), 账户间隔离;
on_rule_events 由钩子对每个账户各调一次。

触发链路: quote_service._evaluate_monitors 产出的 rule_events →
paper_auto.on_rule_events → paper.create_order(source=f"auto:{rule_id}")。
所有下单走 paper 域的同一把锁与校验, 账户冻结/资金/涨跌停等约束自动生效。
"""
from __future__ import annotations

import json
import logging
import uuid
from bisect import bisect_right
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from app.market_time import cn_now, cn_today
from app.services.auction_benchmark import _local_trading_days
from app.services.fs_utils import atomic_write_text
from app.strategy import paper

logger = logging.getLogger(__name__)


def _dir(data_dir: Path, account_id: str) -> Path:
    d = paper._root(data_dir, account_id) / "auto_rules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return cn_now().isoformat()


def _new_id() -> str:
    return f"arule_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def load_daily_signals(data_dir: Path, account_id: str, limit: int = 20) -> list[dict]:
    """最近盘后信号快照，供模拟盘核对选股与下单。"""
    root = paper._root(data_dir, account_id) / "daily_signals"
    out = []
    for path in sorted(root.glob("*.json"), reverse=True)[:max(0, min(limit, 100))]:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            logger.warning("盘后信号快照读取失败 %s: %s", path.name, exc)
    return out


def validate_rule(rule: dict) -> None:
    """校验规则字段, 非法抛 ValueError (中文信息)。"""
    if (rule.get("name") or "").strip() == "":
        raise ValueError("规则名称不能为空")
    if rule.get("match_kind") not in ("strategy", "rule"):
        raise ValueError(f"match_kind 非法: {rule.get('match_kind')!r} (应为 strategy / rule)")
    if not (rule.get("match_id") or "").strip():
        raise ValueError("match_id 不能为空")
    if rule.get("side") not in ("buy", "sell"):
        raise ValueError(f"side 非法: {rule.get('side')!r}")
    if rule.get("size_mode") not in ("fixed_amount", "pct_equity"):
        raise ValueError(f"size_mode 非法: {rule.get('size_mode')!r}")
    value = rule.get("size_value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("size_value 必须是正数")
    if rule.get("size_mode") == "pct_equity" and value > 100:
        raise ValueError("pct_equity 的 size_value 不能超过 100 (%)")
    if rule.get("order_type") not in ("market", "next_open", "close"):
        raise ValueError(f"order_type 非法: {rule.get('order_type')!r}")
    if rule.get("trigger_mode", "realtime") not in ("realtime", "daily_close"):
        raise ValueError("trigger_mode 非法")
    if rule.get("trigger_mode") == "daily_close" and (
        rule["match_kind"] != "strategy" or rule["side"] != "buy" or rule["order_type"] != "next_open"
    ):
        raise ValueError("盘后确认仅支持策略买入、次日开盘成交")
    max_positions = rule.get("max_positions", 10)
    if isinstance(max_positions, bool) or not isinstance(max_positions, int) or not 1 <= max_positions <= 50:
        raise ValueError("max_positions 必须为 1-50")
    cooldown = rule.get("cooldown_days", 0)
    if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
        raise ValueError("cooldown_days 必须是非负整数")


def normalize_rule(rule: dict) -> dict:
    d = dict(rule)
    d["name"] = (d.get("name") or "").strip()
    d["match_id"] = (d.get("match_id") or "").strip()
    d.setdefault("side", "buy")
    d.setdefault("size_mode", "fixed_amount")
    d.setdefault("order_type", "next_open")
    d.setdefault("cooldown_days", 5)
    d.setdefault("trigger_mode", "realtime")
    d.setdefault("max_positions", 10)
    d.setdefault("enabled", True)
    d.setdefault("created_at", _now_iso())
    return d


def load_auto_rules(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID, enabled_only: bool = False) -> list[dict]:
    out: list[dict] = []
    for f in sorted(_dir(data_dir, account_id).glob("arule_*.json")):
        try:
            r = normalize_rule(json.loads(f.read_text(encoding="utf-8")))
            if enabled_only and not r.get("enabled"):
                continue
            out.append(r)
        except Exception as e:
            logger.warning("paper auto rule load failed %s: %s", f.name, e)
    return out


def save_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    validate_rule(rule)
    atomic_write_text(_dir(data_dir, account_id) / f"{rule['id']}.json", json.dumps(rule, ensure_ascii=False, indent=2))
    return rule


def create_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    rule = normalize_rule({**rule, "id": _new_id()})
    validate_rule(rule)
    return save_auto_rule(data_dir, rule, account_id)


def delete_auto_rule(data_dir: Path, rule_id: str, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> bool:
    p = _dir(data_dir, account_id) / f"{rule_id}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def set_enabled(data_dir: Path, rule_id: str, enabled: bool, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    p = _dir(data_dir, account_id) / f"{rule_id}.json"
    if not p.exists():
        return None
    rule = normalize_rule(json.loads(p.read_text(encoding="utf-8")))
    rule["enabled"] = enabled
    return save_auto_rule(data_dir, rule, account_id)


def _matches(rule: dict, ev: dict) -> bool:
    if ev.get("source") == "strategy":
        allowed = ("buy_signal", "pool_entry") if rule["side"] == "buy" else ("sell_signal", "pool_exit")
        if ev.get("type") not in allowed:
            return False
    if rule["match_kind"] == "strategy":
        return ev.get("source") == "strategy" and ev.get("strategy_id") == rule["match_id"]
    return ev.get("rule_id") == rule["match_id"]


def _in_cooldown(data_dir: Path, rule: dict, symbol: str, cooldown_days: int, account_id: str,
                 trading_days: list[_date]) -> bool:
    """同规则同 symbol 最近一次自动下单是否仍在冷却期 (按本地交易日)。"""
    if cooldown_days <= 0:
        return False
    prefix = f"auto:{rule['id']}"
    today = cn_now().date()
    for order in paper.load_orders(data_dir, account_id):
        if order.get("source") != prefix or order.get("symbol") != symbol:
            continue
        try:
            created = _date.fromisoformat(order["created_at"][:10])
        except (KeyError, ValueError):
            continue
        # 缺失行情分区只会延长冷却期, 不会在休市日提前放行。
        if bisect_right(trading_days, today) - bisect_right(trading_days, created) < cooldown_days:
            return True
    return False


def _sizing_qty(data_dir: Path, rule: dict, ref_price: float, account_id: str) -> int:
    if ref_price <= 0:
        return 0
    if rule["size_mode"] == "fixed_amount":
        amount = float(rule["size_value"])
    else:  # pct_equity: 按账户总权益 (现金 + 最新定版持仓市值)
        acc = paper.get_account(data_dir, account_id)
        if acc is None:
            return 0
        nav_rows = paper.load_nav(data_dir, account_id)
        equity = nav_rows[-1]["nav"] if nav_rows else float(acc["cash"])
        amount = equity * float(rule["size_value"]) / 100.0
    return paper.qty_from_amount(amount, ref_price)


def _rule_owned_symbols(data_dir: Path, account_id: str, rule_id: str,
                        positions: dict, orders: list[dict]) -> set[str]:
    """Only positions whose entire current holding cycle came from this rule."""
    source_by_order = {order["id"]: order.get("source") for order in orders}
    cycles: dict[str, tuple[float, bool]] = {}
    for fill in paper.load_fills(data_dir, account_id):
        symbol = fill.get("symbol")
        if not symbol:
            continue
        qty, pure = cycles.get(symbol, (0.0, False))
        if fill.get("kind") == "corp_action":
            cycles[symbol] = (round(qty * float(fill.get("factor", 1)), 6), pure)
        elif fill.get("side") == "buy":
            from_rule = source_by_order.get(fill.get("order_id")) == f"auto:{rule_id}"
            cycles[symbol] = (qty + float(fill["qty"]), from_rule if qty <= 0 else pure and from_rule)
        elif fill.get("side") == "sell":
            remaining = max(0.0, qty - float(fill["qty"]))
            cycles[symbol] = (remaining, pure if remaining > 0 else False)
    return {
        symbol for symbol, pos in positions.items()
        if (cycle := cycles.get(symbol)) and cycle[1] and cycle[0] > 0
        and abs(cycle[0] - float(pos.get("qty", 0))) < 1e-6
    }


def on_rule_events(data_dir: Path, events: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """监控事件 → 自动下单。返回本次创建的订单列表 (被拒订单只记日志)。

    触发条件: 事件带 symbol 与价格、有规则匹配、未冷却; 下单复用 paper.create_order
    (冻结/资金/T+1/涨跌停等校验自动生效), ref_price 用事件价格 (当前快照价)。
    """
    created: list[dict] = []
    if not events:
        return created
    rules = [r for r in load_auto_rules(data_dir, account_id, enabled_only=True)
             if r["trigger_mode"] == "realtime"]
    if not rules:
        return created
    trading_days = _local_trading_days(data_dir)
    with paper.PAPER_LOCK:
        for ev in events:
            symbol = (ev.get("symbol") or "").strip()
            price = ev.get("price")
            if not symbol or price is None or price <= 0:
                continue
            for rule in rules:
                if not _matches(rule, ev):
                    continue
                if _in_cooldown(data_dir, rule, symbol, int(rule.get("cooldown_days", 0)), account_id,
                                trading_days):
                    continue
                qty = _sizing_qty(data_dir, rule, float(price), account_id)
                if qty <= 0:
                    logger.info("paper auto %s: %s 金额不足以一手 (价 %s)", rule["name"], symbol, price)
                    continue
                order, err = paper.create_order(
                    data_dir, symbol, rule["side"],
                    account_id=account_id,
                    qty=qty,
                    order_type=rule["order_type"],
                    ref_price=float(price),
                    source=f"auto:{rule['id']}",
                )
                if err:
                    logger.info("paper auto %s: %s 下单被拒: %s", rule["name"], symbol, err)
                    continue
                created.append(order)
                logger.info("paper auto %s: %s 触发 %s %d 股 (%s)", rule["name"], symbol, rule["side"], qty, order["id"])
    return created


def run_daily_close(repo, engine, as_of: _date) -> dict:
    """成功盘后管道刷新缓存后，以当日定版策略结果生成次日开盘模拟单。"""
    summary = {"as_of": as_of.isoformat(), "created": 0, "skipped": None, "errors": []}
    if as_of != cn_today():
        summary["skipped"] = "非当日数据"
        return summary
    if engine is None:
        summary["skipped"] = "策略引擎未就绪"
        return summary
    data_dir = repo.store.data_dir
    account_rules = [
        (acc_id, rule)
        for acc_id in paper.list_account_ids(data_dir)
        for rule in load_auto_rules(data_dir, acc_id, enabled_only=True)
        if rule["trigger_mode"] == "daily_close"
    ]
    if not account_rules:
        return summary

    from app.services.screener import ScreenerService
    from app.strategy import config as strategy_config

    svc = ScreenerService(repo, asset_type="stock")
    if svc.latest_date() != as_of:
        summary["skipped"] = "当日日线选股数据未就绪"
        return summary
    results = {}
    errors = {}
    for _, rule in account_rules:
        sid = rule["match_id"]
        if sid in results:
            continue
        if not engine.has(sid):
            logger.warning("盘后模拟跟单跳过未知策略: %s", sid)
            errors[sid] = "策略不存在"
            continue
        strategy = engine.get(sid)
        if "stock" not in strategy.meta.get("asset_types", ["stock"]) or "1d" not in strategy.meta.get("timeframes", ["1d"]):
            logger.warning("盘后模拟跟单跳过非股票日线策略: %s", sid)
            errors[sid] = "仅支持股票日线策略"
            continue
        overrides = strategy_config.load_override(data_dir, sid)
        params = dict(overrides.get("params") or {})
        try:
            context = svc.build_strategy_context(
                engine, as_of, [sid], params_map={sid: params}, overrides_map={sid: overrides},
            )
            result = engine.run(sid, context, params=params, overrides=overrides or None)
            if result.as_of != as_of or context.current is None or context.current.is_empty():
                errors[sid] = "当日策略数据为空或日期不匹配"
                continue
            bars = {
                str(row["symbol"]): row
                for row in context.current.select("symbol", "raw_close", "raw_low").drop_nulls().iter_rows(named=True)
            }
            stop_loss = overrides.get("stop_loss", getattr(strategy, "stop_loss", None))
            max_hold_days = overrides.get("max_hold_days", getattr(strategy, "max_hold_days", None))
            results[sid] = (result, bars, stop_loss, max_hold_days, params, overrides)
        except Exception:
            logger.exception("盘后模拟跟单策略计算失败: %s", sid)
            errors[sid] = "策略计算失败，请查看服务日志"

    with paper.PAPER_LOCK:
        for acc_id, rule in account_rules:
            evaluated = results.get(rule["match_id"])
            if evaluated is None:
                reason = errors.get(rule["match_id"], "策略结果不可用")
                summary["errors"].append({"rule_id": rule["id"], "error": reason})
                signal_dir = paper._root(data_dir, acc_id) / "daily_signals"
                signal_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_text(
                    signal_dir / f"{as_of.isoformat()}_{rule['id']}.json",
                    json.dumps({
                        "date": as_of.isoformat(), "strategy_id": rule["match_id"],
                        "rule_id": rule["id"], "status": "failed", "error": reason,
                        "selected_symbols": [], "exit_symbols": [], "order_ids": [],
                        "params": {}, "overrides": {},
                    }, ensure_ascii=False, indent=2),
                )
                continue
            if paper.get_account(data_dir, acc_id) is None:
                continue
            result, bars, stop_loss, max_hold_days, params, overrides = evaluated
            orders = paper.load_orders(data_dir, acc_id)
            positions = paper.load_positions(data_dir, acc_id)
            held = {s for s, p in positions.items() if int(p.get("qty", 0)) > 0}
            pending = {o["symbol"] for o in orders if o["status"] == "pending" and o["side"] == "buy"}
            owned = _rule_owned_symbols(data_dir, acc_id, rule["id"], positions, orders)
            exits = {str(hit.get("symbol")) for hit in result.exit_signal_hits}
            pending_sells = {o["symbol"] for o in orders if o["status"] == "pending" and o["side"] == "sell"}
            trading_days = _local_trading_days(data_dir)
            for symbol in held & owned:
                pos = positions[symbol]
                bar = bars.get(symbol)
                if bar is None:
                    continue
                if stop_loss is not None and float(bar["raw_low"]) <= float(pos["avg_cost"]) * (1 - abs(float(stop_loss))):
                    exits.add(symbol)
                if max_hold_days and any(
                    sum(lot["date"] < d.isoformat() <= as_of.isoformat() for d in trading_days) >= int(max_hold_days)
                    for lot in pos.get("lots", [])
                ):
                    exits.add(symbol)
            for symbol in sorted(held & owned & exits - pending_sells):
                bar = bars.get(symbol)
                price = float(bar["raw_close"]) if bar is not None else None
                qty = paper._available_of(positions[symbol], as_of.isoformat())
                if not price or price <= 0 or qty <= 0:
                    continue
                order, err = paper.create_order(
                    data_dir, symbol, "sell", account_id=acc_id, qty=qty,
                    order_type="next_open", ref_price=price, source=f"auto:{rule['id']}",
                )
                if err:
                    logger.info("盘后模拟卖出被拒: %s %s: %s", rule["name"], symbol, err)
                    continue
                orders.append(order)
                summary["created"] += 1
            room = max(0, int(rule["max_positions"]) - len(held | pending))
            for row in result.rows:
                if room <= 0:
                    break
                symbol = str(row.get("symbol") or "")
                bar = bars.get(symbol)
                price = float(bar["raw_close"]) if bar is not None else None
                if not price or price <= 0 or symbol in held | pending:
                    continue
                if any(o.get("source") == f"auto:{rule['id']}" and o.get("symbol") == symbol
                       and str(o.get("created_at", ""))[:10] == as_of.isoformat() for o in orders):
                    continue
                if _in_cooldown(data_dir, rule, symbol, int(rule["cooldown_days"]), acc_id, trading_days):
                    continue
                qty = _sizing_qty(data_dir, rule, price, acc_id)
                if qty <= 0:
                    continue
                order, err = paper.create_order(
                    data_dir, symbol, "buy", account_id=acc_id, qty=qty,
                    order_type="next_open", ref_price=price, source=f"auto:{rule['id']}",
                )
                if err:
                    logger.info("盘后模拟下单被拒: %s %s: %s", rule["name"], symbol, err)
                    continue
                orders.append(order)
                pending.add(symbol)
                room -= 1
                summary["created"] += 1
            signal_dir = paper._root(data_dir, acc_id) / "daily_signals"
            signal_dir.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "date": as_of.isoformat(), "strategy_id": rule["match_id"], "rule_id": rule["id"],
                "status": "completed",
                "selected_symbols": [str(row["symbol"]) for row in result.rows],
                "exit_symbols": sorted(held & owned & exits),
                "order_ids": [o["id"] for o in orders if o.get("source") == f"auto:{rule['id']}"
                              and str(o.get("created_at", ""))[:10] == as_of.isoformat()],
                "params": params, "overrides": overrides,
            }
            atomic_write_text(
                signal_dir / f"{as_of.isoformat()}_{rule['id']}.json",
                json.dumps(snapshot, ensure_ascii=False, indent=2),
            )
    return summary


_ORDER_TYPE_LABEL = {"market": "即时", "next_open": "次日开盘", "close": "当日收盘"}


def auto_order_events(created: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """自动跟单创建的订单 → 推送事件。与成交事件同构 (source=paper 对齐
    AlertEvent), 走同一推送通道; rule_id 从 order.source (auto:{rule_id}) 还原。
    """
    events: list[dict] = []
    for o in created:
        source = str(o.get("source", ""))
        rule_id = source.removeprefix("auto:") if source.startswith("auto:") else ""
        side_label = "买入" if o["side"] == "buy" else "卖出"
        type_label = _ORDER_TYPE_LABEL.get(o["order_type"], o["order_type"])
        events.append({
            "source": "paper",
            "type": "auto_order",
            "severity": "info",
            "ts": int(cn_now().timestamp() * 1000),
            "symbol": o["symbol"],
            "rule_id": rule_id,
            "side": o["side"],
            "qty": o["qty"],
            "account_id": account_id,
            "message": f"自动跟单触发{side_label}下单 ({type_label}) {o['qty']}股",
        })
    return events
