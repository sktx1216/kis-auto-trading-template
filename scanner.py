from dataclasses import dataclass, field
from statistics import pstdev

import config


@dataclass
class ScanResult:
    symbol: str
    exchange: str
    name: str = ""
    asset_type: str = "STOCK"
    passed: bool = False
    score: int = 0
    reason: str = ""
    metrics: dict = field(default_factory=dict)


def scan_universe(client, universe, held_symbols):
    results = []
    total = len(universe)
    benchmark_metrics = _load_benchmark_metrics(client)
    for index, item in enumerate(universe, start=1):
        symbol = item["symbol"]
        exchange = item.get("exchange", "NASD")
        if index == 1 or index % 10 == 0 or index == total:
            print(f"      scanning {index}/{total}: {symbol}")
        try:
            prices = client.get_daily_prices(symbol, exchange=exchange, days=260)
            current_price = client.get_current_price(symbol, exchange=exchange)
            result = evaluate_buy_candidate(
                item,
                prices,
                current_price,
                held_symbols,
                benchmark_metrics=benchmark_metrics,
            )
        except Exception as error:
            result = ScanResult(
                symbol=symbol,
                exchange=exchange,
                name=item.get("name", ""),
                asset_type=item.get("asset_type", "STOCK"),
                passed=False,
                reason=f"error: {error}",
            )
        results.append(result)
    return results


def _load_benchmark_metrics(client):
    try:
        prices = client.get_daily_prices("QQQ", exchange="NASD", days=260)
        current_price = client.get_current_price("QQQ", exchange="NASD")
        return calculate_metrics(prices, current_price)
    except Exception as error:
        print(f"      benchmark QQQ metrics unavailable: {error}")
        return None


def select_best_candidate(results):
    passed = [result for result in results if result.passed]
    if not passed:
        return None
    return rank_candidates(passed)[0]


def rank_candidates(results):
    passed = [result for result in results if result.passed]
    return sorted(
        passed,
        key=lambda result: (
            -result.score,
            -result.metrics["avg_trade_value_20"],
            result.metrics["volatility_20"],
            result.metrics["recent_5d_return"],
            result.symbol,
        ),
    )


def evaluate_buy_candidate(item, prices, current_price, held_symbols, benchmark_metrics=None):
    symbol = item["symbol"]
    result = ScanResult(
        symbol=symbol,
        exchange=item.get("exchange", "NASD"),
        name=item.get("name", ""),
        asset_type=item.get("asset_type", "STOCK"),
    )
    metrics = calculate_metrics(prices, current_price)
    if metrics and benchmark_metrics:
        _apply_relative_strength_metrics(metrics, benchmark_metrics)
    result.metrics = metrics
    if metrics:
        result.score = score_candidate(metrics, symbol, benchmark_metrics)
        result.metrics["score_breakdown"] = score_candidate_breakdown(metrics, symbol, benchmark_metrics)

    if symbol in held_symbols:
        return _reject(result, "already held")
    if not metrics:
        return _reject(result, "not enough 60-day data")
    if symbol.upper() in config.MANUAL_BLOCK_LIST:
        return _reject(result, "symbol is manually blocked")
    if current_price <= metrics["ma20"]:
        return _reject(result, "current price is not above ma20")
    if not (metrics["ma5"] > metrics["ma20"] > metrics["ma60"]):
        return _reject(result, "moving averages are not aligned")
    if metrics["recent_5d_return"] > 15:
        return _reject(result, "recent 5-day return is above 15%")
    if metrics["recent_20d_return"] > config.MAX_RECENT_20D_RETURN_PERCENT:
        return _reject(
            result,
            f"recent 20-day return is above {config.MAX_RECENT_20D_RETURN_PERCENT:g}%",
        )
    if not (
        config.MIN_BUY_TODAY_RETURN_PERCENT
        <= metrics["today_return"]
        <= config.MAX_BUY_TODAY_RETURN_PERCENT
    ):
        return _reject(
            result,
            (
                "today return is outside "
                f"{config.MIN_BUY_TODAY_RETURN_PERCENT:g}% to {config.MAX_BUY_TODAY_RETURN_PERCENT:g}%"
            ),
        )
    if metrics["gap_percent"] < -config.MAX_BUY_GAP_DOWN_PERCENT:
        return _reject(result, f"opening gap down is above {config.MAX_BUY_GAP_DOWN_PERCENT:g}%")
    if abs(metrics["gap_percent"]) > config.MAX_BUY_GAP_PERCENT:
        return _reject(result, f"opening gap is above {config.MAX_BUY_GAP_PERCENT:g}%")
    if metrics["distance_from_ma60"] > 25:
        return _reject(result, "price is more than 25% above ma60")
    if (
        metrics.get("has_52w_high_data")
        and metrics["position_vs_52w_high_percent"] >= config.MAX_52W_HIGH_POSITION_PERCENT
    ):
        return _reject(
            result,
            f"price is within {100 - config.MAX_52W_HIGH_POSITION_PERCENT:g}% of 52-week high",
        )
    if metrics["volatility_20"] > config.MAX_BUY_VOLATILITY_20_PERCENT:
        return _reject(
            result,
            f"20-day volatility is above {config.MAX_BUY_VOLATILITY_20_PERCENT:g}%",
        )
    if metrics["intraday_range_percent"] > config.MAX_BUY_INTRADAY_RANGE_PERCENT:
        return _reject(
            result,
            f"intraday range is above {config.MAX_BUY_INTRADAY_RANGE_PERCENT:g}%",
        )
    if metrics["previous_day_return"] < config.MIN_BUY_PREVIOUS_DAY_RETURN_PERCENT:
        return _reject(
            result,
            f"previous day return is below {config.MIN_BUY_PREVIOUS_DAY_RETURN_PERCENT:g}%",
        )
    if metrics["recent_3d_return"] < config.MIN_BUY_RECENT_3D_RETURN_PERCENT:
        return _reject(
            result,
            f"recent 3-day return is below {config.MIN_BUY_RECENT_3D_RETURN_PERCENT:g}%",
        )
    if (
        metrics["volume_spike_ratio"] >= config.MAX_BUY_VOLUME_SPIKE_RATIO
        and (metrics["today_return"] < 0 or metrics["previous_day_return"] < 0)
    ):
        return _reject(
            result,
            f"negative move volume spike is above {config.MAX_BUY_VOLUME_SPIKE_RATIO:g}x",
        )
    if metrics["avg_trade_value_20"] < config.MIN_AVG_TRADE_VALUE_20:
        return _reject(result, "20-day average trade value is too low")
    if (
        config.REQUIRE_BUY_RELATIVE_STRENGTH
        and benchmark_metrics
        and not metrics.get("relative_strength_positive")
    ):
        return _reject(result, "relative strength vs QQQ is not positive")

    result.passed = True
    result.reason = "passed"
    return result


def check_market_filter(client):
    prices = client.get_daily_prices("QQQ", exchange="NASD", days=60)
    current_price = client.get_current_price("QQQ", exchange="NASD")
    metrics = calculate_metrics(prices, current_price)
    if not metrics:
        state = "weak"
        allowed = False
        reason = "not enough QQQ data"
    elif current_price > metrics["ma20"] > metrics["ma60"]:
        state = "strong"
        allowed = True
        reason = "QQQ current price > QQQ ma20 > QQQ ma60"
    elif current_price > metrics["ma20"]:
        state = "normal"
        allowed = True
        reason = "QQQ current price > QQQ ma20"
    else:
        state = "weak"
        allowed = False
        reason = "QQQ current price <= QQQ ma20"
    return {
        "allowed": allowed,
        "state": state,
        "symbol": "QQQ",
        "current_price": current_price,
        "ma20": metrics.get("ma20") if metrics else None,
        "ma60": metrics.get("ma60") if metrics else None,
        "reason": reason,
    }


def evaluate_qqq_fallback(client):
    prices = client.get_daily_prices("QQQ", exchange="NASD", days=60)
    current_price = client.get_current_price("QQQ", exchange="NASD")
    metrics = calculate_metrics(prices, current_price)
    result = ScanResult(symbol="QQQ", exchange="NASD", name="Invesco QQQ Trust", asset_type="ETF")
    result.metrics = metrics

    if not metrics:
        return _reject(result, "not enough qqq data")
    if current_price <= metrics["ma20"]:
        return _reject(result, "qqq current price is not above ma20")
    if metrics["ma5"] <= metrics["ma20"]:
        return _reject(result, "qqq ma5 is not above ma20")
    if metrics["recent_5d_return"] > 10:
        return _reject(result, "qqq recent 5-day return is above 10%")

    result.passed = True
    result.score = score_candidate(metrics, result.symbol)
    result.metrics["score_breakdown"] = score_candidate_breakdown(metrics, result.symbol)
    result.reason = "qqq fallback passed"
    return result


def evaluate_sell_decision(position, prices, current_price):
    metrics = calculate_metrics(prices, current_price)
    if not metrics:
        return {"action": "HOLD", "qty_ratio": 0, "reason": "not enough data", "metrics": {}}

    profit_rate = position.get("profit_rate")
    if profit_rate is None:
        avg_price = position.get("avg_price")
        profit_rate = ((current_price / avg_price) - 1) * 100 if avg_price else 0

    if profit_rate >= 20:
        return _sell("SELL_ALL", 1.0, "profit >= 20%", metrics, "TAKE_PROFIT_FULL_20")
    if profit_rate >= 10 and not position.get("already_half_sold"):
        return _sell("SELL_HALF", 0.5, "profit >= 10%", metrics, "TAKE_PROFIT_HALF_10")
    if profit_rate >= 5 and current_price < metrics["ma20"]:
        if _still_buyable_after_ma20_break(metrics):
            return {
                "action": "HOLD",
                "qty_ratio": 0,
                "reason": "ma20 break is shallow and trend remains buyable",
                "metrics": metrics,
                "sell_reason_code": None,
                "deferred_sell_reason_code": "PROFIT_MA20_BREAK",
            }
        return _sell("SELL_ALL", 1.0, "profit >= 5% and price < ma20", metrics, "PROFIT_MA20_BREAK")

    stop_loss = (
        profit_rate <= -8
        and current_price < metrics["ma20"]
        and metrics["ma20"] < metrics["ma60"]
        and metrics["recent_5d_return"] < 0
    )
    if stop_loss:
        return _sell("SELL_ALL", 1.0, "stop loss conditions met", metrics, "STOP_LOSS")

    holding_days = position.get("holding_days")
    early_drawdown = (
        holding_days is not None
        and holding_days <= config.EARLY_DRAWDOWN_EXIT_DAYS
        and profit_rate <= config.EARLY_DRAWDOWN_EXIT_RATE
        and current_price < metrics["ma20"]
        and metrics["recent_3d_return"] < 0
    )
    if early_drawdown:
        return _sell(
            "SELL_ALL",
            1.0,
            (
                f"early drawdown: held {holding_days} trading days "
                f"with profit <= {config.EARLY_DRAWDOWN_EXIT_RATE:g}%"
            ),
            metrics,
            "EARLY_DRAWDOWN",
        )

    stale_position = (
        holding_days is not None
        and holding_days >= config.STALE_POSITION_DAYS
        and profit_rate < config.STALE_POSITION_MIN_PROFIT_RATE
        and (
            current_price < metrics["ma20"]
            or metrics["recent_5d_return"] <= 0
        )
    )
    if stale_position:
        return _sell(
            "SELL_ALL",
            1.0,
            (
                f"stale position: held {holding_days} trading days "
                f"with profit < {config.STALE_POSITION_MIN_PROFIT_RATE:g}%"
            ),
            metrics,
            "STALE_POSITION",
        )

    return {"action": "HOLD", "qty_ratio": 0, "reason": "no sell condition", "metrics": metrics}


def calculate_metrics(prices, current_price=None):
    clean = [row for row in prices if row.get("close") is not None and row.get("volume") is not None]
    if len(clean) < 60:
        return {}

    closes = [float(row["close"]) for row in clean]
    highs = [float(row.get("high") or row["close"]) for row in clean]
    volumes = [float(row["volume"]) for row in clean]
    current = float(current_price if current_price is not None else closes[-1])
    previous_close = closes[-2] if len(closes) >= 2 else closes[-1]
    latest = clean[-1]
    latest_open = float(latest.get("open") or current)
    latest_high = float(latest.get("high") or current)
    latest_low = float(latest.get("low") or current)

    ma5 = _average(closes[-5:])
    ma20 = _average(closes[-20:])
    ma60 = _average(closes[-60:])
    recent_5d_return = _percent_change(closes[-6], current) if len(closes) >= 6 else 0
    recent_3d_return = _percent_change(closes[-4], current) if len(closes) >= 4 else 0
    recent_20d_return = _percent_change(closes[-21], current) if len(closes) >= 21 else 0
    today_return = _percent_change(previous_close, current)
    previous_day_return = _percent_change(closes[-3], closes[-2]) if len(closes) >= 3 else 0
    gap_percent = _percent_change(previous_close, latest_open)
    distance_from_ma60 = _percent_change(ma60, current)
    high_52w = max(highs[-252:] + [current]) if len(highs) >= 252 else None
    position_vs_52w_high_percent = (current / high_52w) * 100 if high_52w else None
    intraday_range_percent = _percent_change(latest_low, latest_high)
    avg_volume_20 = _average(volumes[-20:])
    previous_volume = volumes[-2] if len(volumes) >= 2 else volumes[-1]
    volume_spike_ratio = previous_volume / avg_volume_20 if avg_volume_20 else 0
    avg_trade_value_20 = _average([close * volume for close, volume in zip(closes[-20:], volumes[-20:])])
    returns_20 = [_percent_change(closes[i - 1], closes[i]) for i in range(len(closes) - 19, len(closes))]
    volatility_20 = pstdev(returns_20) if len(returns_20) >= 2 else 0

    return {
        "current_price": current,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "recent_5d_return": recent_5d_return,
        "recent_3d_return": recent_3d_return,
        "recent_20d_return": recent_20d_return,
        "today_return": today_return,
        "previous_day_return": previous_day_return,
        "gap_percent": gap_percent,
        "distance_from_ma60": distance_from_ma60,
        "high_52w": high_52w,
        "position_vs_52w_high_percent": position_vs_52w_high_percent,
        "has_52w_high_data": high_52w is not None,
        "intraday_range_percent": intraday_range_percent,
        "avg_volume_20": avg_volume_20,
        "previous_volume": previous_volume,
        "volume_spike_ratio": volume_spike_ratio,
        "avg_trade_value_20": avg_trade_value_20,
        "volatility_20": volatility_20,
    }


def score_candidate(metrics, symbol=None, benchmark_metrics=None):
    return sum(item["points"] for item in score_candidate_breakdown(metrics, symbol, benchmark_metrics).values())


def score_candidate_breakdown(metrics, symbol=None, benchmark_metrics=None):
    breakdown = {}
    if metrics["current_price"] > metrics["ma20"]:
        breakdown["current_price_above_ma20"] = _score_item(20, "current price > ma20")
    else:
        breakdown["current_price_above_ma20"] = _score_item(0, "current price <= ma20")

    if metrics["ma5"] > metrics["ma20"] > metrics["ma60"]:
        breakdown["ma_alignment"] = _score_item(30, "ma5 > ma20 > ma60")
    else:
        breakdown["ma_alignment"] = _score_item(0, "moving averages are not aligned")

    if metrics["recent_5d_return"] <= 5:
        breakdown["recent_5d_return"] = _score_item(15, "recent 5-day return <= 5%")
    elif metrics["recent_5d_return"] <= 10:
        breakdown["recent_5d_return"] = _score_item(10, "recent 5-day return <= 10%")
    elif metrics["recent_5d_return"] <= 15:
        breakdown["recent_5d_return"] = _score_item(5, "recent 5-day return <= 15%")
    else:
        breakdown["recent_5d_return"] = _score_item(0, "recent 5-day return > 15%")

    if metrics["distance_from_ma60"] <= 10:
        breakdown["distance_from_ma60"] = _score_item(15, "distance from ma60 <= 10%")
    elif metrics["distance_from_ma60"] <= 20:
        breakdown["distance_from_ma60"] = _score_item(10, "distance from ma60 <= 20%")
    elif metrics["distance_from_ma60"] <= 25:
        breakdown["distance_from_ma60"] = _score_item(5, "distance from ma60 <= 25%")
    else:
        breakdown["distance_from_ma60"] = _score_item(0, "distance from ma60 > 25%")

    if metrics["avg_trade_value_20"] >= 1_000_000_000:
        breakdown["avg_trade_value_20"] = _score_item(10, "20-day average trade value >= 1,000,000,000")
    else:
        breakdown["avg_trade_value_20"] = _score_item(0, "20-day average trade value < 1,000,000,000")

    if metrics["volatility_20"] <= 5:
        breakdown["volatility_20"] = _score_item(10, "20-day volatility <= 5%")
    else:
        breakdown["volatility_20"] = _score_item(0, "20-day volatility > 5%")

    risk = cyclical_travel_risk(symbol)
    if risk["penalty"]:
        breakdown["cyclical_travel_risk"] = _score_item(
            -risk["penalty"],
            "cyclical travel/air/lodging sector risk penalty",
        )
    metrics["cyclical_travel_risk_tier"] = risk["tier"]
    metrics["cyclical_travel_risk_penalty"] = risk["penalty"]

    if benchmark_metrics:
        _apply_relative_strength_metrics(metrics, benchmark_metrics)
        if metrics.get("relative_strength_positive"):
            breakdown["relative_strength_vs_qqq"] = _score_item(
                config.BUY_RELATIVE_STRENGTH_BONUS,
                "20/60-day relative strength vs QQQ is positive",
            )
        else:
            breakdown["relative_strength_vs_qqq"] = _score_item(
                -config.BUY_RELATIVE_STRENGTH_PENALTY,
                "20/60-day relative strength vs QQQ is not positive",
            )
    return breakdown


def _score_item(points, reason):
    return {"points": points, "reason": reason}


def cyclical_travel_risk(symbol):
    if not config.CYCLICAL_TRAVEL_RISK_MODE or not symbol:
        return {"tier": "none", "penalty": 0}
    if symbol.upper() in config.CYCLICAL_TRAVEL_RISK_SYMBOLS:
        return {"tier": "travel_air_lodging", "penalty": config.CYCLICAL_TRAVEL_RISK_PENALTY}
    return {"tier": "none", "penalty": 0}


def _apply_relative_strength_metrics(metrics, benchmark_metrics):
    stock_rs20 = _ratio_over_ma(metrics, "ma20")
    stock_rs60 = _ratio_over_ma(metrics, "ma60")
    qqq_rs20 = _ratio_over_ma(benchmark_metrics, "ma20")
    qqq_rs60 = _ratio_over_ma(benchmark_metrics, "ma60")
    edge20 = (stock_rs20 - qqq_rs20) * 100
    edge60 = (stock_rs60 - qqq_rs60) * 100
    min_edge = config.MIN_RELATIVE_STRENGTH_EDGE_PERCENT
    metrics["relative_strength_20_edge"] = edge20
    metrics["relative_strength_60_edge"] = edge60
    metrics["relative_strength_positive"] = edge20 >= min_edge and edge60 >= min_edge


def _ratio_over_ma(metrics, ma_key):
    ma_value = metrics.get(ma_key) if metrics else None
    current_price = metrics.get("current_price") if metrics else None
    if not ma_value:
        return 0
    return (current_price / ma_value) - 1


def _still_buyable_after_ma20_break(metrics):
    ma20_break_depth = abs(_percent_change(metrics["ma20"], metrics["current_price"]))
    return (
        ma20_break_depth <= config.PROFIT_MA20_BREAK_HOLD_BAND_PERCENT
        and metrics["ma5"] > metrics["ma20"] > metrics["ma60"]
        and metrics["recent_5d_return"] <= 15
        and -2 <= metrics["today_return"] <= 5
        and metrics["distance_from_ma60"] <= 25
        and metrics["avg_trade_value_20"] >= config.MIN_AVG_TRADE_VALUE_20
    )


def _reject(result, reason):
    result.passed = False
    result.reason = reason
    return result


def _sell(action, qty_ratio, reason, metrics, sell_reason_code):
    return {
        "action": action,
        "qty_ratio": qty_ratio,
        "reason": reason,
        "sell_reason_code": sell_reason_code,
        "metrics": metrics,
    }


def _average(values):
    return sum(values) / len(values)


def _percent_change(base, value):
    if not base:
        return 0
    return ((value / base) - 1) * 100
