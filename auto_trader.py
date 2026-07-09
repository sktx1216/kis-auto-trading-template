import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from kis_overseas import (
    KisApiError,
    KisOverseasClient,
    calculate_order_qty,
    extract_open_orders,
    extract_order_ids,
    extract_positions,
    extract_usd_cash,
    summarize_balance_response,
)
from market_hours import (
    is_us_market_day,
    is_us_market_open,
    market_date_key,
    market_status_note,
    trading_days_between,
)
from portfolio_exporter import (
    export_portfolio_snapshot,
    load_trader_state,
    push_decision_log,
    push_trader_state,
)
from scanner import (
    check_market_filter,
    evaluate_qqq_fallback,
    evaluate_sell_decision,
    rank_candidates,
    scan_universe,
)
from token_manager import require_cached_token
from universe import load_nasdaq100_universe

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
STATE_PATH = BASE_DIR / "state.json"


def main(mode="full"):
    print(f"DRY_RUN={config.DRY_RUN}")
    print(f"MODE={mode}")
    state = _load_state()
    market_hours = market_status_note()
    print(
        f"      market_day={market_hours['is_market_day']} "
        f"market_open={market_hours['is_open']} market_time={market_hours['market_time']}"
    )
    if mode in {"full", "sell-only"} and not is_us_market_day():
        print("[SKIP] US market is closed today. No KIS API calls or orders will be attempted.")
        print("[DONE] market-closed run skipped")
        return

    print("[1/9] Preparing KIS access token...")

    client = KisOverseasClient()
    require_cached_token(client)

    if mode == "cancel-open-orders":
        print("[2/9] Cancel-open-orders mode: checking overseas open orders")
        logs = _cancel_open_orders(client)
        _push_portfolio_snapshot_after_cancel(client)
        log_path = _write_logs(logs, push_decision_log_enabled=False)
        print(f"[9/9] log saved: {log_path}")
        print("[DONE] cancel-open-orders run finished")
        return

    if mode == "portfolio-snapshot":
        print("[2/9] Portfolio-snapshot mode: exporting account snapshot only")
        if not _push_portfolio_snapshot_only(client):
            sys.exit(1)
        print("[DONE] portfolio-snapshot run finished")
        return

    print("[2/9] Loading overseas account balance...")
    balance = client.get_present_balance()
    positions = _merge_position_state(extract_positions(balance), state)
    usd_cash = extract_usd_cash(balance)
    stock_asset_usd = _stock_asset_usd(positions)
    total_asset_usd = usd_cash + stock_asset_usd
    held_symbols = {position["symbol"] for position in positions}

    print(
        f"[2/9] positions={len(positions)}, usd_cash={usd_cash:.2f}, "
        f"stock_asset_usd={stock_asset_usd:.2f}, total_asset_usd={total_asset_usd:.2f}"
    )
    if positions:
        for position in positions:
            print(
                f"      holding {position['symbol']} qty={position.get('qty')} "
                f"avg={position.get('avg_price')} profit_rate={position.get('profit_rate')}"
            )

    logs = []
    half_sold_symbols = set()

    if mode == "diagnose":
        print(f"[DIAG] base_url_type={'paper' if client.is_paper_trading else 'live'}")
        print(f"[DIAG] balance_summary={json.dumps(summarize_balance_response(balance), ensure_ascii=False)}")
        print("[3/9] Diagnose mode: skipping sell rules and order execution")
        print("[4/9] Checking QQQ market filter...")
        market = check_market_filter(client)
        print(
            "[DIAG] buyable_amount_attempts="
            f"{json.dumps(_summarize_buyable_amount_attempts(client, 'QQQ', market['current_price']), ensure_ascii=False)}"
        )
        logs.append(_log_row("MARKET_FILTER", "QQQ", decision=market))
        print(
            f"[4/9] market_filter={market['allowed']} "
            f"state={market['state']} qqq={market['current_price']} "
            f"ma20={market['ma20']} ma60={market['ma60']} ({market['reason']})"
        )
        log_path = _write_logs(logs, push_decision_log_enabled=False)
        print(f"[9/9] log saved: {log_path}")
        print("[DONE] diagnose run finished without buy/sell orders")
        return

    if mode == "score-only":
        print("[3/9] Score-only mode: skipping sell rules and order execution")
        print("[4/9] Checking QQQ market filter...")
        market = check_market_filter(client)
        logs.append(_log_row("MARKET_FILTER", "QQQ", decision=market))
        print(
            f"[4/9] market_filter={market['allowed']} "
            f"state={market['state']} qqq={market['current_price']} "
            f"ma20={market['ma20']} ma60={market['ma60']} ({market['reason']})"
        )
        universe = load_nasdaq100_universe()
        print(f"[6/9] Scanning NASDAQ100 universe for scores only: {len(universe)} symbols")
        scan_results = scan_universe(client, universe, held_symbols)
        passed_count = len([result for result in scan_results if result.passed])
        print(f"[6/9] score scan complete: passed={passed_count}, rejected={len(scan_results) - passed_count}")
        ranked_candidates = rank_candidates(scan_results)
        for rank, result in enumerate(ranked_candidates, start=1):
            logs.append(_log_row("SCORE_RANK", result.symbol, result=result, decision={"rank": rank}))
        for result in scan_results:
            logs.append(_log_row("SCAN", result.symbol, result=result))
        logs.append(
            _log_row(
                "SCORE_ONLY",
                "",
                decision={
                    "action": "NO_ORDER",
                    "reason": "score-only mode does not submit buy or sell orders",
                },
            )
        )
        log_path = _write_logs(logs, push_decision_log_enabled=True)
        print(f"[9/9] log saved: {log_path}")
        print("[DONE] score-only run finished without buy/sell orders")
        return

    print("[3/9] Checking sell rules for current positions...")
    for position in positions:
        decision = _handle_sell_decision(client, position, state)
        logs.append(_log_row("SELL_CHECK", position["symbol"], decision=decision))
        print(f"      sell_check {position['symbol']}: {decision['action']} ({decision['reason']})")
        if decision.get("rule_action") == "SELL_HALF" and decision["action"] == "SELL_CONFIRMED":
            half_sold_symbols.add(position["symbol"])

    print("[4/9] Checking QQQ market filter...")
    if mode == "sell-only":
        print("[4/9] Skipping market filter in sell-only mode")
        logs.append(_log_row("MODE", "sell-only", decision={"action": "SELL_ONLY", "reason": "buy scan skipped"}))
        _save_state(positions, half_sold_symbols, state)
        log_path = _write_logs(logs, push_decision_log_enabled=False)
        print(f"[9/9] log saved: {log_path}")
        print("[DONE] sell-only run finished")
        return

    market = check_market_filter(client)
    logs.append(_log_row("MARKET_FILTER", "QQQ", decision=market))
    print(
        f"[4/9] market_filter={market['allowed']} "
        f"state={market['state']} qqq={market['current_price']} "
        f"ma20={market['ma20']} ma60={market['ma60']} ({market['reason']})"
    )

    buy_count = 0
    scan_results = []
    max_buy_count = _max_buy_count_for_market(market["state"])
    buy_allowed = market["allowed"] or _allow_weak_market_relative_strength_buy(market["state"])

    print("[5/9] Checking buy guards...")
    if buy_allowed and max_buy_count > 0 and _can_buy(positions, balance):
        if not market["allowed"]:
            print("[5/9] Weak market: allowing one limited high-score stock entry")
        universe = load_nasdaq100_universe()
        print(f"[6/9] Scanning NASDAQ100 universe: {len(universe)} symbols")
        scan_results = scan_universe(client, universe, held_symbols)
        passed_count = len([result for result in scan_results if result.passed])
        print(f"[6/9] scan complete: passed={passed_count}, rejected={len(scan_results) - passed_count}")
        ranked_candidates = rank_candidates(scan_results)
        cash_remaining = usd_cash
        sizing_total_asset_usd = _sizing_total_asset_usd(total_asset_usd, cash_remaining)
        max_positions = _max_positions_for_account(sizing_total_asset_usd)
        print(f"[6/9] sizing_total_asset_usd={sizing_total_asset_usd:.2f}")
        print(f"[6/9] max_positions_for_account={max_positions}")
        positions_count = len(positions)
        qqq_metrics = {
            "current_price": market["current_price"],
            "ma20": market["ma20"],
            "ma60": market["ma60"],
        }
        rejected_buy_candidates = []

        while buy_count < max_buy_count and positions_count < max_positions:
            selected, decision = _choose_affordable_candidate(
                client,
                ranked_candidates,
                cash_remaining,
                sizing_total_asset_usd,
                positions,
                state,
                qqq_metrics,
                market["state"],
                rejected_buy_candidates,
            )

            if selected is None:
                break

            print(
                f"[8/9] Selected buy candidate: {selected.symbol} "
                f"score={selected.score} price={selected.metrics.get('current_price')}"
            )
            decision = decision or _build_buy_decision(selected, cash_remaining, sizing_total_asset_usd, positions, state)
            decision = _execute_buy_decision(client, selected, decision, state, positions)
            logs.append(_log_row("BUY_CHECK", selected.symbol, result=selected, decision=decision))
            print(f"[8/9] buy_decision {selected.symbol}: {decision['action']} ({decision['reason']})")
            if _counts_toward_buy_limit(decision):
                buy_count += 1
                positions_count += 1
                cash_remaining -= decision.get("amount", 0)
                held_symbols.add(selected.symbol)
            ranked_candidates = [candidate for candidate in ranked_candidates if candidate.symbol != selected.symbol]

        if ranked_candidates:
            limit_reason = _remaining_candidate_limit_reason(buy_count, max_buy_count, positions_count, max_positions)
            if limit_reason:
                for candidate in ranked_candidates:
                    rejected_buy_candidates.append(
                        (
                            candidate,
                            {
                                "action": "NO_BUY",
                                "reason": limit_reason,
                                "price": candidate.metrics.get("current_price"),
                                "score": candidate.score,
                            },
                        )
                    )

        if buy_count == 0 and config.ENABLE_QQQ_FALLBACK:
            print("[7/9] No stock candidate passed. Checking QQQ fallback...")
            fallback = evaluate_qqq_fallback(client)
            if not fallback.passed:
                print(f"[7/9] QQQ fallback rejected: {fallback.reason}")
            else:
                selected, decision = _choose_affordable_candidate(
                    client,
                    [fallback],
                    cash_remaining,
                    sizing_total_asset_usd,
                    positions,
                    state,
                    qqq_metrics,
                    market["state"],
                    rejected_buy_candidates,
                )
                if selected:
                    decision = _execute_buy_decision(client, selected, decision, state, positions)
                    logs.append(_log_row("BUY_CHECK", selected.symbol, result=selected, decision=decision))
                    print(f"[8/9] buy_decision {selected.symbol}: {decision['action']} ({decision['reason']})")
                    if _counts_toward_buy_limit(decision):
                        buy_count += 1
        elif buy_count == 0:
            print("[7/9] QQQ fallback disabled")

        if buy_count == 0:
            print("[8/9] No buy order: no candidate")
            logs.append(_log_row("BUY_CHECK", "", decision={"action": "NO_BUY", "reason": "no candidate"}))
        for candidate, decision in rejected_buy_candidates:
            logs.append(_log_row("BUY_CANDIDATE_REJECTED", candidate.symbol, result=candidate, decision=decision))
    else:
        if not buy_allowed:
            reason = market["reason"]
        elif max_buy_count <= 0:
            reason = f"market state {market['state']} allows no new buys"
        else:
            reason = "position or daily-loss guard blocked buying"
        print(f"[5/9] Buy blocked: {reason}")
        logs.append(_log_row("BUY_CHECK", "", decision={"action": "NO_BUY", "reason": reason}))

    for result in scan_results:
        logs.append(_log_row("SCAN", result.symbol, result=result))

    _save_state(positions, half_sold_symbols, state)
    log_path = _write_logs(logs, push_decision_log_enabled=True)
    print(f"[9/9] log saved: {log_path}")
    print("[DONE] run finished")


def parse_args():
    parser = argparse.ArgumentParser(description="KIS overseas auto trader")
    parser.add_argument(
        "--mode",
        choices=[
            "full",
            "sell-only",
            "score-only",
            "diagnose",
            "cancel-open-orders",
            "portfolio-snapshot",
        ],
        default="full",
        help=(
            "full runs sell checks and buy scan. sell-only only checks existing positions. "
            "score-only scans and writes decision_log.json without orders. "
            "cancel-open-orders cancels all overseas open orders. "
            "portfolio-snapshot updates portfolio JSON files without orders. "
            "diagnose checks config, token, balance, market filter, and snapshot export without orders."
        ),
    )
    return parser.parse_args()


def _handle_sell_decision(client, position, state):
    symbol = position["symbol"]
    exchange = position.get("exchange", "NASD")
    prices = client.get_daily_prices(symbol, exchange=exchange, days=100)
    current_price = client.get_current_price(symbol, exchange=exchange)
    decision = evaluate_sell_decision(position, prices, current_price)
    action = decision["action"]
    decision["rule_action"] = action
    decision["sell_reason_code"] = decision.get("sell_reason_code")

    if action == "HOLD":
        return decision

    if not is_us_market_open():
        decision.update({"action": "HOLD", "reason": "US market is closed"})
        return decision

    if action == "SELL_HALF" and position.get("qty", 0) < 2:
        decision.update({"action": "HOLD", "reason": "single-share position skips half sell"})
        return decision

    qty = int(position["qty"] * decision["qty_ratio"])
    if qty <= 0:
        decision.update({"action": "HOLD", "reason": "calculated sell qty is zero"})
        return decision

    limit_price = _sell_limit_price(current_price)
    decision["qty"] = qty
    decision["price"] = limit_price
    decision["reference_price"] = current_price
    decision["amount"] = qty * limit_price
    if config.DRY_RUN:
        decision["action"] = f"DRY_RUN_{action}"
        print(f"[DRY_RUN] sell {symbol} qty={qty} price={limit_price:.2f}: {decision['reason']}")
        return decision

    before_qty = position.get("qty") or 0
    try:
        response = client.sell_limit_order(symbol, qty, limit_price, exchange=exchange)
        decision["response"] = response
        _record_sell_today(state, symbol, {**decision, "action": "SELL_ATTEMPT"})
        if _confirm_position_qty_change(client, symbol, before_qty, "decrease"):
            decision["action"] = "SELL_CONFIRMED"
            print(f"[ORDER_CONFIRMED] sell {symbol} qty={qty} price={limit_price:.2f}")
        else:
            decision["action"] = "SELL_SUBMITTED_NOT_CONFIRMED"
            decision["reason"] = "sell order submitted but balance quantity did not decrease yet"
            print(f"[ORDER_PENDING] sell {symbol}: submitted but not confirmed in balance")
    except KisApiError as error:
        decision.update({"action": "ORDER_FAILED", "reason": f"sell order failed: {error}"})
        print(f"[ORDER_FAILED] sell {symbol}: {error}")
    return decision


def _summarize_buyable_amount_attempts(client, symbol, price):
    attempts = client.get_buyable_amount_attempts(symbol, price, "NASD")
    summaries = []
    for attempt in attempts:
        if not attempt["ok"]:
            summaries.append(
                {"tr_id": attempt["tr_id"], "ok": False, "error": attempt.get("error")}
            )
            continue

        output = attempt.get("data", {}).get("output", {})
        summaries.append(
            {
                "tr_id": attempt["tr_id"],
                "ok": True,
                "rt_cd": attempt["data"].get("rt_cd"),
                "msg_cd": attempt["data"].get("msg_cd"),
                "msg1": attempt["data"].get("msg1"),
                "output_keys": sorted(output.keys())[:30],
                "amount_candidates": {
                    key: output.get(key)
                    for key in (
                        "ovrs_ord_psbl_amt",
                        "ord_psbl_frcr_amt",
                        "max_ord_psbl_amt",
                        "frcr_ord_psbl_amt",
                    )
                    if key in output
                },
            }
        )
    return summaries


def _choose_affordable_candidate(
    client,
    candidates,
    cash_usd,
    total_asset_usd,
    positions,
    state,
    qqq_metrics,
    market_state="normal",
    rejected_candidates=None,
):
    for candidate in candidates:
        candidate_cash_usd = _buyable_cash_for_candidate(client, candidate, cash_usd)
        candidate_total_asset_usd = _sizing_total_asset_usd(total_asset_usd, candidate_cash_usd)
        if candidate_total_asset_usd <= 0 and candidate_cash_usd > 0:
            candidate_total_asset_usd = candidate_cash_usd
        decision = _build_buy_decision(
            candidate,
            candidate_cash_usd,
            candidate_total_asset_usd,
            positions,
            state,
            qqq_metrics,
            market_state,
        )
        decision.setdefault("cash", round(candidate_cash_usd, 2))
        if decision["action"] != "NO_BUY":
            return candidate, decision
        if rejected_candidates is not None:
            rejected_candidates.append((candidate, decision))
        print(
            f"      skip {candidate.symbol}: {decision['reason']} "
            f"(price={candidate.metrics.get('current_price')}, cash={candidate_cash_usd:.2f})"
        )
    return None, None


def _remaining_candidate_limit_reason(buy_count, max_buy_count, positions_count, max_positions=None):
    max_positions = max_positions or config.MAX_POSITIONS
    if buy_count >= max_buy_count:
        return f"not purchased because daily buy limit was reached ({buy_count}/{max_buy_count})"
    if positions_count >= max_positions:
        return (
            f"not purchased because max positions limit was reached ({positions_count}/{max_positions}); "
            "replacement buys require an existing sell first"
        )
    return ""


def _buyable_cash_for_candidate(client, candidate, fallback_cash_usd):
    try:
        buyable_cash = client.get_buyable_amount(
            candidate.symbol,
            candidate.metrics["current_price"],
            candidate.exchange,
        )
    except KisApiError as error:
        print(f"      buyable amount lookup failed for {candidate.symbol}: {error}")
        return fallback_cash_usd

    if buyable_cash > fallback_cash_usd:
        print(
            f"      buyable cash {candidate.symbol}: {buyable_cash:.2f} "
            f"(balance cash={fallback_cash_usd:.2f})"
        )
    return max(fallback_cash_usd, buyable_cash)


def _sizing_total_asset_usd(account_total_asset_usd, available_cash_usd=0):
    if config.SIZING_TOTAL_ASSET_USD is not None:
        return max(account_total_asset_usd, config.SIZING_TOTAL_ASSET_USD)
    if account_total_asset_usd > 0:
        return account_total_asset_usd
    return available_cash_usd


def _build_buy_decision(selected, cash_usd, total_asset_usd, positions, state, qqq_metrics=None, market_state="normal"):
    current_price = selected.metrics["current_price"]
    if not is_us_market_open():
        return {
            "action": "NO_BUY",
            "reason": "US market is closed",
            "price": current_price,
            "amount": 0,
            "score": selected.score,
        }
    if _already_bought_today(state, selected.symbol):
        return {
            "action": "NO_BUY",
            "reason": "symbol already bought today",
            "price": current_price,
            "amount": 0,
            "score": selected.score,
        }
    cooldown = _cooldown_status(state, selected.symbol)
    if cooldown["blocked"]:
        return {
            "action": "NO_BUY",
            "reason": cooldown["excluded_reason"],
            "price": current_price,
            "amount": 0,
            "score": selected.score,
            **cooldown,
        }
    if (
        cooldown["last_sell_reason"] == "TAKE_PROFIT_FULL_20"
        and qqq_metrics
        and not _has_positive_relative_strength(selected.metrics, qqq_metrics)
    ):
        return {
            "action": "NO_BUY",
            "reason": "relative strength vs QQQ is not positive after full take-profit cooldown",
            "price": current_price,
            "amount": 0,
            "score": selected.score,
            "last_sell_date": cooldown["last_sell_date"],
            "last_sell_reason": cooldown["last_sell_reason"],
            "excluded_reason": "relative strength vs QQQ is not positive after full take-profit cooldown",
        }
    reentry_min_score = _reentry_min_score(cooldown["last_sell_reason"])
    if reentry_min_score and selected.score < reentry_min_score:
        return {
            "action": "NO_BUY",
            "reason": (
                f"reentry after {cooldown['last_sell_reason']} requires score >= "
                f"{reentry_min_score} (score={selected.score})"
            ),
            "price": current_price,
            "amount": 0,
            "score": selected.score,
            "last_sell_date": cooldown["last_sell_date"],
            "last_sell_reason": cooldown["last_sell_reason"],
            "excluded_reason": f"reentry score below {reentry_min_score}",
        }
    if market_state == "weak":
        if selected.asset_type == "ETF":
            return {
                "action": "NO_BUY",
                "reason": "ETF fallback is disabled in weak market",
                "price": current_price,
                "amount": 0,
                "score": selected.score,
            }
        if selected.score < config.MIN_WEAK_MARKET_RELATIVE_STRENGTH_SCORE:
            return {
                "action": "NO_BUY",
                "reason": (
                    "weak market requires high relative-strength score "
                    f"(score={selected.score}, min={config.MIN_WEAK_MARKET_RELATIVE_STRENGTH_SCORE})"
                ),
                "price": current_price,
                "amount": 0,
                "score": selected.score,
            }
        if (
            config.REQUIRE_WEAK_MARKET_RELATIVE_STRENGTH
            and (not qqq_metrics or not _has_positive_relative_strength(selected.metrics, qqq_metrics))
        ):
            return {
                "action": "NO_BUY",
                "reason": "weak market requires positive 20/60-day relative strength vs QQQ",
                "price": current_price,
                "amount": 0,
                "score": selected.score,
            }

    current_position_value = _position_value(positions, selected.symbol)
    is_new_position = current_position_value <= 0
    target_order_amount = total_asset_usd * _target_position_ratio_for_account(total_asset_usd)
    max_position_value = total_asset_usd * _max_position_ratio_for_account(total_asset_usd)
    max_additional_amount = max_position_value - current_position_value
    cash_limited_amount = cash_usd * (1 - config.CASH_BUFFER_RATIO)
    limit_price = _buy_limit_price(current_price)

    if (
        is_new_position
        and _is_small_account(total_asset_usd)
        and cash_usd > 0
        and limit_price > cash_usd * config.SMALL_ACCOUNT_MAX_FIRST_SHARE_CASH_RATIO
    ):
        return {
            "action": "NO_BUY",
            "reason": (
                "first share uses too much available cash "
                f"(cash_ratio={_percent(limit_price, cash_usd):.2f}%)"
            ),
            "price": current_price,
            "amount": 0,
            "score": selected.score,
            "cash_ratio": _percent(limit_price, cash_usd),
        }

    if is_new_position and limit_price > max_position_value and _is_small_account(total_asset_usd):
        max_additional_amount = limit_price
    elif is_new_position and limit_price > max_position_value:
        return {
            "action": "NO_BUY",
            "reason": (
                "first share exceeds MAX_POSITION_RATIO "
                f"(share_ratio={_percent(limit_price, total_asset_usd):.2f}%)"
            ),
            "price": current_price,
            "amount": 0,
            "score": selected.score,
            "share_ratio": _percent(limit_price, total_asset_usd),
            "max_position_value": max_position_value,
        }

    if is_new_position:
        target_order_amount = max(target_order_amount, limit_price)

    order_budget = min(target_order_amount, max_additional_amount, cash_limited_amount)
    qty = calculate_order_qty(order_budget, limit_price)
    if qty <= 0:
        return {
            "action": "NO_BUY",
            "reason": "candidate is not affordable with available cash",
            "price": current_price,
            "amount": 0,
            "score": selected.score,
            "order_budget": order_budget,
        }

    amount = qty * limit_price
    if amount < config.MIN_ORDER_AMOUNT_USD:
        return {"action": "NO_BUY", "reason": "order amount is below MIN_ORDER_AMOUNT_USD"}

    return {
        "action": "DRY_RUN_BUY" if config.DRY_RUN else "BUY",
        "reason": selected.reason,
        "qty": qty,
        "price": limit_price,
        "reference_price": current_price,
        "amount": amount,
        "score": selected.score,
        "order_budget": order_budget,
        "target_order_amount": target_order_amount,
        "max_additional_amount": max_additional_amount,
        "cash_limited_amount": cash_limited_amount,
    }


def _is_small_account(total_asset_usd):
    return total_asset_usd <= config.SMALL_ACCOUNT_THRESHOLD_USD


def _max_positions_for_account(total_asset_usd):
    if _is_small_account(total_asset_usd):
        return config.SMALL_ACCOUNT_MAX_POSITIONS
    return config.MAX_POSITIONS


def _target_position_ratio_for_account(total_asset_usd):
    if _is_small_account(total_asset_usd):
        return config.SMALL_ACCOUNT_TARGET_POSITION_RATIO
    return config.TARGET_POSITION_RATIO


def _max_position_ratio_for_account(total_asset_usd):
    if _is_small_account(total_asset_usd):
        return config.SMALL_ACCOUNT_MAX_POSITION_RATIO
    return config.MAX_POSITION_RATIO


def _reentry_min_score(last_sell_reason):
    requirements = {
        "STALE_POSITION": config.REENTRY_MIN_SCORE_AFTER_STALE_POSITION,
        "STOP_LOSS": config.REENTRY_MIN_SCORE_AFTER_STOP_LOSS,
        "PROFIT_MA20_BREAK": config.REENTRY_MIN_SCORE_AFTER_PROFIT_MA20_BREAK,
    }
    return requirements.get(last_sell_reason, 0)


def _execute_buy_decision(client, selected, decision, state, positions):
    if decision["action"] == "NO_BUY":
        return decision

    qty = decision["qty"]
    current_price = decision["price"]
    amount = decision["amount"]
    before_qty = _position_qty(positions, selected.symbol)

    if config.DRY_RUN:
        print(
            f"[DRY_RUN] buy {selected.symbol} qty={qty} price={current_price:.2f} "
            f"amount={amount:.2f} score={selected.score}"
        )
        return decision

    _record_buy_today(state, selected.symbol, {**decision, "action": "BUY_ATTEMPT"})
    try:
        decision["response"] = client.buy_limit_order(selected.symbol, qty, current_price, selected.exchange)
        if _confirm_position_qty_change(client, selected.symbol, before_qty, "increase"):
            decision["action"] = "BUY_CONFIRMED"
            _record_buy_today(state, selected.symbol, decision)
            print(f"[ORDER_CONFIRMED] buy {selected.symbol} qty={qty} price={current_price:.2f}")
        else:
            decision["action"] = "BUY_SUBMITTED_NOT_CONFIRMED"
            decision["reason"] = "buy order submitted but balance quantity did not increase yet"
            _record_buy_today(state, selected.symbol, decision)
            print(f"[ORDER_PENDING] buy {selected.symbol}: submitted but not confirmed in balance")
    except KisApiError as error:
        decision.update({"action": "ORDER_FAILED", "reason": f"buy order failed: {error}"})
        print(f"[ORDER_FAILED] buy {selected.symbol}: {error}")
    return decision


def _cancel_open_orders(client, exchanges=("NASD", "NYSE", "AMEX")):
    logs = []
    total_orders = 0
    canceled_count = 0
    failed_count = 0

    for exchange in exchanges:
        try:
            data = client.get_open_orders(exchange)
            orders = extract_open_orders(data, default_exchange=exchange)
        except KisApiError as error:
            failed_count += 1
            decision = {
                "action": "OPEN_ORDER_LOOKUP_FAILED",
                "reason": f"open order lookup failed: {error}",
                "exchange": exchange,
            }
            logs.append(_log_row("CANCEL_OPEN_ORDERS", exchange, decision=decision))
            print(f"[CANCEL_OPEN_ORDERS] {exchange}: lookup failed ({error})")
            continue

        if not orders:
            print(f"[CANCEL_OPEN_ORDERS] {exchange}: no open orders")
            logs.append(
                _log_row(
                    "CANCEL_OPEN_ORDERS",
                    exchange,
                    decision={
                        "action": "NO_OPEN_ORDERS",
                        "reason": "no open overseas orders",
                        "exchange": exchange,
                    },
                )
            )
            continue

        for order in orders:
            total_orders += 1
            symbol = order["symbol"]
            order_exchange = order.get("exchange") or exchange
            qty = order["qty"]
            price = order.get("price") or 0
            decision = {
                "action": "DRY_RUN_CANCEL_OPEN_ORDER" if config.DRY_RUN else "CANCEL_OPEN_ORDER",
                "reason": "open overseas order will be canceled",
                "exchange": order_exchange,
                "qty": qty,
                "price": price,
                "order_no": order.get("order_no"),
                "org_no": order.get("org_no"),
            }

            if config.DRY_RUN:
                print(
                    f"[DRY_RUN] cancel open order {symbol} exchange={order_exchange} "
                    f"qty={qty} order_no={order.get('order_no')}"
                )
                logs.append(_log_row("CANCEL_OPEN_ORDER", symbol, decision=decision))
                continue

            try:
                response = client.cancel_order(symbol, qty, price, order_exchange, order["raw"])
            except KisApiError as error:
                failed_count += 1
                decision.update(
                    {
                        "action": "CANCEL_OPEN_ORDER_FAILED",
                        "reason": f"open order cancel failed: {error}",
                        "cancel_error": str(error),
                    }
                )
                print(f"[CANCEL_FAILED] {symbol} order_no={order.get('order_no')}: {error}")
            else:
                canceled_count += 1
                decision.update(
                    {
                        "action": "CANCEL_OPEN_ORDER_SUBMITTED",
                        "reason": "open overseas order cancel submitted",
                        "response": response,
                    }
                )
                print(f"[CANCEL_SUBMITTED] {symbol} order_no={order.get('order_no')} qty={qty}")
            logs.append(_log_row("CANCEL_OPEN_ORDER", symbol, decision=decision))

    print(
        f"[CANCEL_OPEN_ORDERS] total_open_orders={total_orders} "
        f"canceled={canceled_count} failed={failed_count}"
    )
    logs.append(
        _log_row(
            "CANCEL_OPEN_ORDERS_SUMMARY",
            "",
            decision={
                "action": "CANCEL_OPEN_ORDERS_DONE",
                "reason": "open overseas order cancellation completed",
                "total_open_orders": total_orders,
                "canceled": canceled_count,
                "failed": failed_count,
            },
        )
    )
    return logs


def _push_portfolio_snapshot_after_cancel(client):
    try:
        result = export_portfolio_snapshot(client, "cancel_open_orders_snapshot")
    except Exception as error:
        print(f"[PORTFOLIO_SNAPSHOT_FAILED] {error}")
        return
    if result.get("pushed"):
        print(f"[PORTFOLIO_SNAPSHOT_PUSHED] {result['latest_path']}")
    else:
        print(f"[PORTFOLIO_SNAPSHOT_NOT_PUSHED] {result.get('reason')}")


def _push_portfolio_snapshot_only(client):
    try:
        result = export_portfolio_snapshot(client, "portfolio_snapshot")
    except Exception as error:
        print(f"[PORTFOLIO_SNAPSHOT_FAILED] {error}")
        return False
    if result.get("pushed"):
        print(f"[PORTFOLIO_SNAPSHOT_PUSHED] {result['dashboard_path']}")
    else:
        print(f"[PORTFOLIO_SNAPSHOT_NOT_PUSHED] {result.get('reason')}")
    return True


def _can_buy(positions, balance):
    if len(positions) >= config.MAX_POSITIONS:
        return False
    daily_loss_rate = _daily_loss_rate(balance)
    return daily_loss_rate is None or daily_loss_rate > config.MAX_DAILY_LOSS_RATE


def _max_buy_count_for_market(market_state):
    if market_state == "strong":
        return config.MAX_BUY_PER_DAY_STRONG
    if market_state == "normal":
        return config.MAX_BUY_PER_DAY_NORMAL
    if _allow_weak_market_relative_strength_buy(market_state):
        return config.MAX_BUY_PER_DAY_WEAK_RELATIVE_STRENGTH
    return config.MAX_BUY_PER_DAY_WEAK


def _allow_weak_market_relative_strength_buy(market_state):
    return market_state == "weak" and config.ALLOW_WEAK_MARKET_RELATIVE_STRENGTH_BUY


def _counts_toward_buy_limit(decision):
    return decision.get("action") in {
        "DRY_RUN_BUY",
        "BUY_ATTEMPT",
        "BUY_SUBMITTED_NOT_CONFIRMED",
        "BUY_CONFIRMED",
    }


def _stock_asset_usd(positions):
    return sum(position.get("market_value") or position.get("purchase_amount") or 0 for position in positions)


def _position_value(positions, symbol):
    for position in positions:
        if position["symbol"] == symbol:
            return position.get("market_value") or position.get("purchase_amount") or 0
    return 0


def _position_qty(positions, symbol):
    for position in positions:
        if position["symbol"] == symbol:
            return position.get("qty") or 0
    return 0


def _buy_limit_price(current_price):
    return round(current_price * (1 + config.BUY_LIMIT_PRICE_BUFFER), 2)


def _sell_limit_price(current_price):
    return round(current_price * (1 - config.SELL_LIMIT_PRICE_BUFFER), 2)


def _confirm_position_qty_change(client, symbol, before_qty, direction):
    for attempt in range(config.ORDER_CONFIRM_RETRIES + 1):
        if attempt > 0 or config.ORDER_CONFIRM_WAIT_SECONDS > 0:
            time.sleep(config.ORDER_CONFIRM_WAIT_SECONDS)
        try:
            balance = client.get_present_balance()
            positions = extract_positions(balance)
            after_qty = _position_qty(positions, symbol)
        except KisApiError as error:
            print(f"[ORDER_CONFIRM_FAILED] {symbol}: {error}")
            continue

        if direction == "increase" and after_qty > before_qty:
            return True
        if direction == "decrease" and after_qty < before_qty:
            return True
    return False


def _daily_loss_rate(balance):
    for section in ("output2", "output3"):
        value = balance.get(section)
        if isinstance(value, dict):
            value = [value]
        for item in value or []:
            for key in ("evlu_pfls_rt", "tot_evlu_pfls_rt", "dncl_evlu_pfls_rt"):
                raw = item.get(key)
                if raw not in (None, ""):
                    try:
                        return float(str(raw).replace(",", ""))
                    except ValueError:
                        pass
    return None


def _load_state():
    if not STATE_PATH.exists():
        return _normalize_state(load_trader_state())
    with STATE_PATH.open("r", encoding="utf-8") as f:
        return _normalize_state(json.load(f))


def _save_state(positions, half_sold_symbols, state):
    state = _normalize_state(state)
    previous_positions = dict(state.get("positions", {}) or {})
    state["positions"] = {}
    for position in positions:
        symbol = position["symbol"]
        saved = previous_positions.get(symbol, {})
        entry_date = saved.get("entry_date") or _first_confirmed_buy_date(state, symbol) or _today_key()
        holding_days = trading_days_between(entry_date)
        state["positions"][symbol] = {
            "already_half_sold": bool(position.get("already_half_sold") or symbol in half_sold_symbols),
            "entry_date": entry_date,
            "holding_days": holding_days,
            "qty": position.get("qty"),
            "avg_price": position.get("avg_price"),
            "market_value": position.get("market_value"),
            "purchase_amount": position.get("purchase_amount"),
            "profit_rate": position.get("profit_rate"),
        }
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    try:
        result = push_trader_state(state)
        if result.get("pushed"):
            print(f"[TRADER_STATE_PUSHED] {result['state_path']}")
    except Exception as error:
        print(f"[TRADER_STATE_PUSH_FAILED] {error}")


def _merge_position_state(positions, state):
    state = _normalize_state(state)
    for position in positions:
        saved = state["positions"].get(position["symbol"], {})
        position["already_half_sold"] = bool(saved.get("already_half_sold"))
        entry_date = saved.get("entry_date") or _first_confirmed_buy_date(state, position["symbol"])
        position["entry_date"] = entry_date
        position["holding_days"] = trading_days_between(entry_date) if entry_date else None
        position["state_qty"] = saved.get("qty")
        position["state_avg_price"] = saved.get("avg_price")
    return positions


def _normalize_state(state):
    normalized = {"positions": {}, "orders_by_date": {}, "last_sells": {}, "token": {}}
    if not isinstance(state, dict):
        return normalized
    if "positions" in state or "orders_by_date" in state:
        normalized["positions"] = dict(state.get("positions", {}) or {})
        normalized["orders_by_date"] = _normalize_orders_by_date(state.get("orders_by_date", {}) or {})
        normalized["last_sells"] = dict(state.get("last_sells", {}) or {})
        normalized["token"] = dict(state.get("token", {}) or {})
        state.clear()
        state.update(normalized)
        return normalized
    normalized["positions"] = dict(state)
    return normalized


def _normalize_orders_by_date(orders_by_date):
    normalized = {}
    for date_key, day in dict(orders_by_date).items():
        if not isinstance(day, dict):
            continue

        orders = list(day.get("orders", []) or [])
        buys = set(day.get("buys", []) or [])
        buy_attempts = set(day.get("buy_attempts", []) or [])
        pending_buys = set(day.get("pending_buys", []) or [])
        sells = set(day.get("sells", []) or [])
        confirmed_buys = set()

        for order in orders:
            if not isinstance(order, dict):
                continue
            symbol = order.get("symbol")
            action = order.get("action")
            if not symbol:
                continue
            if action == "BUY_CONFIRMED":
                confirmed_buys.add(symbol)
            elif action == "BUY_ATTEMPT":
                buy_attempts.add(symbol)
            elif action == "BUY_SUBMITTED_NOT_CONFIRMED":
                pending_buys.add(symbol)

        if confirmed_buys:
            buys = confirmed_buys
        elif orders:
            buys = set()

        normalized[date_key] = {
            **day,
            "buys": sorted(buys),
            "buy_attempts": sorted(buy_attempts),
            "pending_buys": sorted(pending_buys),
            "sells": sorted(sells),
            "orders": orders,
        }
    return normalized


def _today_key():
    return market_date_key()


def _already_bought_today(state, symbol):
    state = _normalize_state(state)
    today = state["orders_by_date"].get(_today_key(), {})
    blocked_symbols = set(today.get("buys", []))
    blocked_symbols.update(today.get("buy_attempts", []))
    blocked_symbols.update(today.get("pending_buys", []))
    return symbol in blocked_symbols


def _sold_today(state, symbol):
    state = _normalize_state(state)
    today = state["orders_by_date"].get(_today_key(), {})
    return symbol in set(today.get("sells", []))


def _cooldown_status(state, symbol):
    state = _normalize_state(state)
    last_sell = state.get("last_sells", {}).get(symbol)
    if not last_sell:
        return {
            "blocked": False,
            "last_sell_date": None,
            "last_sell_reason": None,
            "cooldown_days": 0,
            "remaining_cooldown_days": 0,
            "excluded_reason": "",
        }

    last_sell_date = last_sell.get("date")
    last_sell_reason = last_sell.get("reason")
    cooldown_days = config.COOLDOWN_DAYS.get(last_sell_reason, 0)
    elapsed_days = trading_days_between(last_sell_date)
    same_day = last_sell_date == market_date_key()
    blocked = same_day or elapsed_days <= cooldown_days
    if not blocked:
        remaining = 0
    elif same_day:
        remaining = cooldown_days
    else:
        remaining = max(cooldown_days - elapsed_days + 1, 0)
    excluded_reason = ""
    if blocked:
        excluded_reason = (
            f"cooldown active after {last_sell_reason}: "
            f"last_sell_date={last_sell_date}, remaining_trading_days={remaining}"
        )

    return {
        "blocked": blocked,
        "last_sell_date": last_sell_date,
        "last_sell_reason": last_sell_reason,
        "cooldown_days": cooldown_days,
        "remaining_cooldown_days": remaining,
        "excluded_reason": excluded_reason,
    }


def _first_confirmed_buy_date(state, symbol):
    state = _normalize_state(state)
    for date_key in sorted(state.get("orders_by_date", {})):
        day = state["orders_by_date"].get(date_key, {})
        if symbol in set(day.get("buys", [])):
            return date_key
        for order in day.get("orders", []) or []:
            if (
                isinstance(order, dict)
                and order.get("symbol") == symbol
                and order.get("action") == "BUY_CONFIRMED"
            ):
                return date_key
    return None


def _has_positive_relative_strength(metrics, qqq_metrics):
    stock_rs20 = _ratio_over_ma(metrics, "ma20")
    stock_rs60 = _ratio_over_ma(metrics, "ma60")
    qqq_rs20 = _ratio_over_ma(qqq_metrics, "ma20")
    qqq_rs60 = _ratio_over_ma(qqq_metrics, "ma60")
    min_edge = config.MIN_RELATIVE_STRENGTH_EDGE_PERCENT / 100
    return stock_rs20 >= qqq_rs20 + min_edge and stock_rs60 >= qqq_rs60 + min_edge


def _ratio_over_ma(metrics, ma_key):
    ma_value = metrics.get(ma_key)
    current_price = metrics.get("current_price")
    if not ma_value:
        return 0
    return (current_price / ma_value) - 1


def _percent(value, base):
    if not base:
        return 0
    return (value / base) * 100


def _record_buy_today(state, symbol, decision):
    normalized = _normalize_state(state)
    state.clear()
    state.update(normalized)
    today = state["orders_by_date"].setdefault(
        _today_key(),
        {"buys": [], "buy_attempts": [], "pending_buys": [], "sells": [], "orders": []},
    )
    today.setdefault("buys", [])
    today.setdefault("buy_attempts", [])
    today.setdefault("pending_buys", [])
    today.setdefault("sells", [])
    today.setdefault("orders", [])
    action = decision.get("action")
    if action == "BUY_CONFIRMED" and symbol not in today["buys"]:
        today["buys"].append(symbol)
        position_state = state.setdefault("positions", {}).setdefault(symbol, {})
        position_state.setdefault("entry_date", _today_key())
    if action == "BUY_ATTEMPT" and symbol not in today["buy_attempts"]:
        today["buy_attempts"].append(symbol)
    if action == "BUY_SUBMITTED_NOT_CONFIRMED" and symbol not in today["pending_buys"]:
        today["pending_buys"].append(symbol)
    today["orders"].append(
        _compact_order_record(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "action": decision.get("action"),
                "qty": decision.get("qty"),
                "price": decision.get("price"),
                "amount": decision.get("amount"),
                **extract_order_ids(decision.get("response", {})),
                "cancel_error": decision.get("cancel_error"),
            }
        )
    )


def _compact_order_record(record):
    return {
        key: value
        for key, value in record.items()
        if value not in (None, "", [], {})
    }


def _record_sell_today(state, symbol, decision):
    normalized = _normalize_state(state)
    state.clear()
    state.update(normalized)
    today = state["orders_by_date"].setdefault(_today_key(), {"buys": [], "sells": [], "orders": []})
    today.setdefault("buys", [])
    today.setdefault("sells", [])
    today.setdefault("orders", [])
    if symbol not in today["sells"]:
        today["sells"].append(symbol)
    reason = decision.get("sell_reason_code")
    if reason:
        state.setdefault("last_sells", {})[symbol] = {
            "date": _today_key(),
            "reason": reason,
            "action": decision.get("action"),
        }
    today["orders"].append(
        _compact_order_record(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "action": decision.get("action"),
                "qty": decision.get("qty"),
                "price": decision.get("price"),
                "amount": decision.get("amount"),
                **extract_order_ids(decision.get("response", {})),
                "cancel_error": decision.get("cancel_error"),
            }
        )
    )


def _write_logs(rows, push_decision_log_enabled=True):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_id = f"trading_log_{timestamp}"
    path = LOG_DIR / f"trading_log_{timestamp}.csv"
    json_path = LOG_DIR / f"trading_log_{timestamp}.json"
    payload = _json_log_payload(rows, log_id)
    fieldnames = [
        "timestamp",
        "event",
        "symbol",
        "passed",
        "rank",
        "score",
        "action",
        "reason",
        "reason_ko",
        "current_price",
        "ma5",
        "ma20",
        "ma60",
        "previous_day_return",
        "recent_3d_return",
        "recent_5d_return",
        "recent_20d_return",
        "today_return",
        "gap_percent",
        "distance_from_ma60",
        "high_52w",
        "position_vs_52w_high_percent",
        "intraday_range_percent",
        "avg_trade_value_20",
        "volatility_20",
        "volume_spike_ratio",
        "relative_strength_20_edge",
        "relative_strength_60_edge",
        "relative_strength_positive",
        "cyclical_travel_risk_tier",
        "cyclical_travel_risk_penalty",
        "qty",
        "amount",
        "cash",
        "cash_ratio",
        "share_ratio",
        "blocked",
        "last_sell_date",
        "last_sell_reason",
        "cooldown_days",
        "remaining_cooldown_days",
        "excluded_reason",
        "excluded_reason_ko",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in fieldnames} for row in rows])
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    if push_decision_log_enabled:
        _push_decision_log(payload, log_id)
    else:
        print("[DECISION_LOG_NOT_PUSHED] disabled for this run mode")
    return path


def _push_decision_log(payload, log_id):
    try:
        result = push_decision_log(payload, log_id)
    except Exception as error:
        print(f"[DECISION_LOG_PUSH_FAILED] {error}")
        return
    if result.get("pushed"):
        print(f"[DECISION_LOG_PUSHED] {result['latest_path']}")
    else:
        print(f"[DECISION_LOG_NOT_PUSHED] {result.get('reason')}")


def _json_log_payload(rows, log_id):
    raw_rows = [_json_log_row(row) for row in rows]
    merged_rows = _merge_decision_rows_for_json(raw_rows)
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "data_type": "trading_decision_log",
        "schema_version": 2,
        "log_id": log_id,
        "latest_file": "decision_log.json",
        "history_file": f"decision_logs/{log_id}.json",
        "rows": merged_rows,
        "raw_rows": raw_rows,
    }


def _merge_decision_rows_for_json(rows):
    merged = []
    index_by_symbol = {}
    for row in rows:
        symbol = row.get("symbol")
        if not symbol or row.get("event") not in {"SCAN", "BUY_CANDIDATE_REJECTED", "BUY_CHECK"}:
            merged.append(row)
            continue

        if symbol not in index_by_symbol:
            index_by_symbol[symbol] = len(merged)
            merged.append(dict(row))
            continue

        existing = merged[index_by_symbol[symbol]]
        if row.get("event") == "SCAN" and existing.get("event") != "SCAN":
            _fill_empty_fields(existing, row)
        elif row.get("event") != "SCAN":
            _fill_empty_fields(row, existing)
            merged[index_by_symbol[symbol]] = dict(row)
    return merged


def _fill_empty_fields(target, source):
    for key, value in source.items():
        if target.get(key) in ("", None, [], {}):
            target[key] = value


def _json_log_row(row):
    return {
        key: value
        for key, value in row.items()
        if key != "score_breakdown" or value
    }


def _log_row(event, symbol, result=None, decision=None):
    metrics = result.metrics if result else {}
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "symbol": symbol,
        "passed": result.passed if result else "",
        "rank": "",
        "score": result.score if result else "",
        "action": "",
        "reason": result.reason if result else "",
        "reason_ko": _reason_ko(result.reason if result else ""),
        "current_price": metrics.get("current_price"),
        "ma5": metrics.get("ma5"),
        "ma20": metrics.get("ma20"),
        "ma60": metrics.get("ma60"),
        "previous_day_return": metrics.get("previous_day_return"),
        "recent_3d_return": metrics.get("recent_3d_return"),
        "recent_5d_return": metrics.get("recent_5d_return"),
        "recent_20d_return": metrics.get("recent_20d_return"),
        "today_return": metrics.get("today_return"),
        "gap_percent": metrics.get("gap_percent"),
        "distance_from_ma60": metrics.get("distance_from_ma60"),
        "high_52w": metrics.get("high_52w"),
        "position_vs_52w_high_percent": metrics.get("position_vs_52w_high_percent"),
        "intraday_range_percent": metrics.get("intraday_range_percent"),
        "avg_trade_value_20": metrics.get("avg_trade_value_20"),
        "volatility_20": metrics.get("volatility_20"),
        "volume_spike_ratio": metrics.get("volume_spike_ratio"),
        "relative_strength_20_edge": metrics.get("relative_strength_20_edge"),
        "relative_strength_60_edge": metrics.get("relative_strength_60_edge"),
        "relative_strength_positive": metrics.get("relative_strength_positive", ""),
        "cyclical_travel_risk_tier": metrics.get("cyclical_travel_risk_tier", ""),
        "cyclical_travel_risk_penalty": metrics.get("cyclical_travel_risk_penalty", ""),
        "score_breakdown": metrics.get("score_breakdown", {}),
        "qty": "",
        "amount": "",
        "cash": "",
        "cash_ratio": "",
        "share_ratio": "",
        "blocked": "",
        "last_sell_date": "",
        "last_sell_reason": "",
        "cooldown_days": "",
        "remaining_cooldown_days": "",
        "excluded_reason": "",
        "excluded_reason_ko": "",
    }
    if decision:
        row["action"] = decision.get("action", "")
        row["reason"] = decision.get("reason", row["reason"])
        row["reason_ko"] = decision.get("reason_ko", _reason_ko(row["reason"]))
        row["rank"] = decision.get("rank", row["rank"])
        row["qty"] = decision.get("qty", "")
        row["amount"] = decision.get("amount", "")
        row["cash"] = decision.get("cash", "")
        row["cash_ratio"] = decision.get("cash_ratio", "")
        row["share_ratio"] = decision.get("share_ratio", "")
        row["blocked"] = decision.get("blocked", "")
        row["last_sell_date"] = decision.get("last_sell_date", "")
        row["last_sell_reason"] = decision.get("last_sell_reason", "")
        row["cooldown_days"] = decision.get("cooldown_days", "")
        row["remaining_cooldown_days"] = decision.get("remaining_cooldown_days", "")
        row["excluded_reason"] = decision.get("excluded_reason", "")
        row["excluded_reason_ko"] = decision.get(
            "excluded_reason_ko",
            _reason_ko(row["excluded_reason"]),
        )
        for key in ("current_price", "ma5", "ma20", "ma60"):
            if key in decision:
                row[key] = decision[key]
    return row


def _reason_ko(reason):
    if not reason:
        return ""
    text = str(reason)
    lower = text.lower()
    translations = [
        ("passed", "매수 조건 통과"),
        ("already held", "이미 보유 중인 종목"),
        ("not enough 60-day data", "최근 60거래일 데이터 부족"),
        ("not enough qqq data", "QQQ 데이터 부족"),
        ("not enough qqq data", "QQQ 데이터 부족"),
        ("not enough data", "판단 데이터 부족"),
        ("symbol is manually blocked", "수동 제외 종목"),
        ("current price is not above ma20", "현재가가 20일 이동평균선 위가 아님"),
        ("moving averages are not aligned", "5일/20일/60일 이동평균 정배열 아님"),
        ("recent 5-day return is above 15%", "최근 5거래일 상승률이 15% 초과"),
        ("previous day return is below", "전일 급락으로 매수 제외"),
        ("recent 3-day return is below", "최근 3일 급락으로 매수 제외"),
        ("recent 20-day return is above", "최근 20거래일 상승률이 기준 초과"),
        ("today return is outside", "당일 등락률이 허용 범위 밖"),
        ("opening gap down is above", "시가 갭하락이 기준 초과"),
        ("opening gap is above", "시가 갭이 기준 초과"),
        ("price is more than 25% above ma60", "현재가가 60일선 대비 25% 초과 상승"),
        ("52-week high", "52주 고점에 너무 가까움"),
        ("20-day volatility is above", "20일 변동성이 기준 초과"),
        ("intraday range is above", "당일 고저 변동폭이 기준 초과"),
        ("negative move volume spike is above", "하락 중 거래량 급증으로 매수 제외"),
        ("cyclical travel/air/lodging sector risk penalty", "여행/항공/숙박 경기민감 리스크로 점수 감점"),
        ("20-day average trade value is too low", "20일 평균 거래대금 부족"),
        ("qqq current price > qqq ma20 > qqq ma60", "QQQ 강세: 현재가 > 20일선 > 60일선"),
        ("qqq current price > qqq ma20", "QQQ 보통: 현재가가 20일선 위"),
        ("qqq current price <= qqq ma20", "QQQ 약세: 현재가가 20일선 이하"),
        ("qqq current price is not above ma20", "QQQ 현재가가 20일선 위가 아님"),
        ("qqq ma5 is not above ma20", "QQQ 5일선이 20일선 위가 아님"),
        ("qqq recent 5-day return is above 10%", "QQQ 최근 5거래일 상승률이 10% 초과"),
        ("qqq fallback passed", "QQQ 대체 매수 조건 통과"),
        ("us market is closed", "미국 정규장이 열려 있지 않음"),
        ("symbol already bought today", "오늘 이미 매수 시도한 종목"),
        ("cooldown active after", "매도 후 재진입 쿨다운 적용 중"),
        ("relative strength vs qqq is not positive", "QQQ 대비 상대강도가 양수가 아님"),
        ("20/60-day relative strength vs qqq is positive", "QQQ 대비 20/60일 상대강도 양호"),
        ("20/60-day relative strength vs qqq is not positive", "QQQ 대비 20/60일 상대강도 부족"),
        ("reentry after", "재진입 최소 점수 조건 미달"),
        ("reentry score below", "재진입 점수 기준 미달"),
        ("etf fallback is disabled in weak market", "약세장에서는 ETF 대체 매수 비활성화"),
        ("weak market requires high relative-strength score", "약세장 매수에는 높은 상대강도 점수 필요"),
        ("weak market requires positive 20/60-day relative strength vs qqq", "약세장 매수에는 QQQ 대비 20/60일 상대강도 필요"),
        ("first share uses too much available cash", "1주 매수 금액이 사용 가능 현금 대비 너무 큼"),
        ("first share exceeds max_position_ratio", "1주 매수 시 종목 최대 비중 초과"),
        ("candidate is not affordable with available cash", "사용 가능 현금으로 매수 불가"),
        ("order amount is below min_order_amount_usd", "주문금액이 최소 주문금액 미만"),
        ("no candidate", "매수 후보 없음"),
        ("market state", "시장 상태상 신규 매수 불가"),
        ("position or daily-loss guard blocked buying", "보유 종목 수 또는 일일 손실 제한으로 매수 차단"),
        ("not purchased because daily buy limit was reached", "일일 매수 제한 도달로 미매수"),
        ("not purchased because max positions limit was reached", "최대 보유 종목 수 도달로 미매수"),
        ("buy order submitted but balance quantity did not increase yet", "매수 주문 제출됨, 잔고 증가 아직 미확인"),
        ("sell order submitted but balance quantity did not decrease yet", "매도 주문 제출됨, 잔고 감소 아직 미확인"),
        ("buy order failed", "매수 주문 실패"),
        ("sell order failed", "매도 주문 실패"),
        ("profit >= 20%", "수익률 20% 이상 전량 매도 조건"),
        ("profit >= 10%", "수익률 10% 이상 절반 매도 조건"),
        ("profit >= 5% and price < ma20", "수익 5% 이상이고 20일선 이탈"),
        ("stop loss conditions met", "손절 조건 충족"),
        ("early drawdown", "매수 직후 급락 조기 방어 조건"),
        ("ma20 break is shallow and trend remains buyable", "20일선 이탈폭이 작고 추세가 유지되어 매도 보류"),
        ("single-share position skips half sell", "1주 보유라 절반 매도 생략"),
        ("calculated sell qty is zero", "계산된 매도 수량이 0"),
        ("no sell condition", "매도 조건 없음"),
        ("stale position", "장기 보유 대비 수익 부진 조건"),
        ("score-only mode does not submit buy or sell orders", "점수 계산 전용 모드라 주문 없음"),
        ("buy scan skipped", "매수 스캔 생략"),
        ("no open overseas orders", "해외주식 미체결 주문 없음"),
        ("open overseas order will be canceled", "해외주식 미체결 주문 취소 예정"),
        ("open overseas order cancel submitted", "해외주식 미체결 주문 취소 제출"),
        ("open order lookup failed", "미체결 주문 조회 실패"),
        ("open order cancel failed", "미체결 주문 취소 실패"),
        ("open overseas order cancellation completed", "해외주식 미체결 주문 취소 점검 완료"),
    ]
    for pattern, translation in translations:
        if pattern in lower:
            return translation
    if lower.startswith("error:"):
        return "데이터 조회 또는 계산 오류"
    return text


if __name__ == "__main__":
    try:
        args = parse_args()
        main(mode=args.mode)
    except KisApiError as error:
        print(f"[KIS_API_FAILED] {error}")
        sys.exit(1)
    except RuntimeError as error:
        print(f"[TRADER_FAILED] {error}")
        sys.exit(1)
