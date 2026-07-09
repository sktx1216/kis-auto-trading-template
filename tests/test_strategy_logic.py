import json
import os
import unittest

os.environ["DRY_RUN"] = "true"
os.environ["MARKET_HOURS_GUARD"] = "false"
os.environ["MIN_AVG_TRADE_VALUE_20"] = "1000000000"

import auto_trader
import kis_overseas
import market_hours
import portfolio_exporter
import scanner


class FakeClient:
    def __init__(self, buyable_amount=0):
        self.buyable_amount = buyable_amount

    def get_buyable_amount(self, symbol, price, exchange):
        return self.buyable_amount


class FakePriceClient:
    def __init__(self, prices, current_price):
        self.prices = prices
        self.current_price = current_price
        self.sell_orders = []

    def get_daily_prices(self, symbol, exchange="NASD", days=100):
        return self.prices

    def get_current_price(self, symbol, exchange="NASD"):
        return self.current_price

    def sell_limit_order(self, symbol, qty, price, exchange="NASD"):
        self.sell_orders.append((symbol, qty, price, exchange))
        return {"ok": True}


class FakeUnconfirmedBuyClient:
    def __init__(self):
        self.cancel_orders = []

    def buy_limit_order(self, symbol, qty, price, exchange="NASD"):
        return {"output": {"KRX_FWDG_ORD_ORGNO": "001", "ODNO": "12345"}}

    def get_present_balance(self):
        return {"output1": [], "output2": []}

    def cancel_order(self, symbol, qty, price, exchange, order_response):
        self.cancel_orders.append((symbol, qty, price, exchange, order_response))
        return {"rt_cd": "0", "msg1": "canceled"}


class FakeOpenOrdersClient:
    def __init__(self):
        self.cancel_orders = []

    def get_open_orders(self, exchange):
        if exchange != "NASD":
            return {"output": []}
        return {
            "output": [
                {
                    "ovrs_excg_cd": "NASD",
                    "pdno": "AAPL",
                    "nccs_qty": "1",
                    "ovrs_ord_unpr": "160.12",
                    "krx_fwdg_ord_orgno": "001",
                    "odno": "12345",
                }
            ]
        }

    def cancel_order(self, symbol, qty, price, exchange, order_response):
        self.cancel_orders.append((symbol, qty, price, exchange, order_response))
        return {"rt_cd": "0", "msg1": "canceled"}


class FakePortfolioBalanceClient:
    def get_present_balance(self):
        return {
            "output1": [{"pdno": "LIN", "bass_exrt": "1300"}],
            "output2": [
                {
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "647.78",
                    "frcr_drwg_psbl_amt_1": "126.80",
                }
            ],
        }

    def get_balance(self, exchange="NASD", currency="USD"):
        if exchange != "NASD":
            return {"output1": [], "output2": {}}
        return {
            "output1": [
                {
                    "ovrs_pdno": "LIN",
                    "ovrs_item_name": "Linde plc",
                    "ovrs_cblc_qty": "1",
                    "pchs_avg_pric": "519.69",
                    "frcr_pchs_amt1": "519.69000",
                    "ovrs_stck_evlu_amt": "518.940000",
                    "frcr_evlu_pfls_amt": "-0.750000",
                    "evlu_pfls_rt": "-0.14",
                }
            ],
            "output2": {},
        }


def rising_prices(start=100.0, step=1.0, days=60, volume=20_000_000):
    return [
        {
            "date": f"2026-01-{(index % 28) + 1:02d}",
            "open": start + index * step,
            "high": start + index * step + 1,
            "low": start + index * step - 1,
            "close": start + index * step,
            "volume": volume,
        }
        for index in range(days)
    ]


def recent_20d_spike_prices(volume=20_000_000):
    closes = [100.0] * 40 + [100.0 + index * 1.45 for index in range(20)]
    return [
        {
            "date": f"2026-03-{(index % 28) + 1:02d}",
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": volume,
        }
        for index, close in enumerate(closes)
    ]


def high_volatility_prices(volume=20_000_000):
    closes = [100.0 + index + (6.0 if index % 2 else -6.0) for index in range(60)]
    rows = [
        {
            "date": f"2026-06-{(index % 28) + 1:02d}",
            "open": close,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": volume,
        }
        for index, close in enumerate(closes)
    ]
    rows[-1]["open"] = rows[-2]["close"] * 1.01
    return rows


def wide_intraday_range_prices(volume=20_000_000):
    prices = rising_prices(start=100.0, step=1.0, volume=volume)
    prices[-1]["high"] = prices[-1]["close"] * 1.10
    prices[-1]["low"] = prices[-1]["close"] * 0.98
    return prices


def large_gap_prices(volume=20_000_000):
    prices = rising_prices(start=100.0, step=1.0, volume=volume)
    prices[-2]["close"] = 150.0
    prices[-1]["open"] = 158.0
    prices[-1]["high"] = 158.5
    prices[-1]["low"] = 155.0
    prices[-1]["close"] = 155.5
    return prices


def large_gap_down_prices(volume=20_000_000):
    prices = rising_prices(start=100.0, step=1.0, volume=volume)
    prices[-2]["close"] = 160.0
    prices[-1]["open"] = 155.0
    prices[-1]["high"] = 161.0
    prices[-1]["low"] = 154.0
    prices[-1]["close"] = 160.0
    return prices


def previous_day_drop_prices(volume=20_000_000):
    prices = rising_prices(start=100.0, step=1.0, volume=volume)
    prices[-3]["close"] = 160.0
    prices[-2]["close"] = 154.0
    prices[-1]["open"] = 155.0
    prices[-1]["high"] = 161.0
    prices[-1]["low"] = 154.0
    prices[-1]["close"] = 160.0
    return prices


def recent_3d_drop_prices(volume=20_000_000):
    prices = rising_prices(start=100.0, step=1.0, volume=volume)
    prices[-4]["close"] = 168.0
    prices[-3]["close"] = 164.0
    prices[-2]["close"] = 161.5
    prices[-1]["open"] = 161.0
    prices[-1]["high"] = 162.0
    prices[-1]["low"] = 159.5
    prices[-1]["close"] = 160.0
    return prices


def negative_volume_spike_prices(volume=20_000_000):
    prices = rising_prices(start=100.0, step=1.0, volume=volume)
    prices[-3]["close"] = 158.0
    prices[-2]["close"] = 156.0
    prices[-2]["volume"] = volume * 4
    prices[-1]["open"] = 157.0
    prices[-1]["high"] = 161.0
    prices[-1]["low"] = 156.0
    prices[-1]["close"] = 160.0
    return prices


def shallow_ma20_break_prices(volume=20_000_000):
    closes = list(range(100, 140)) + [
        145,
        146,
        147,
        148,
        149,
        150,
        151,
        152,
        153,
        154,
        155,
        156,
        157,
        158,
        159,
        160,
        161,
        162,
        149,
        150,
    ]
    return [
        {
            "date": f"2026-02-{(index % 28) + 1:02d}",
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": volume,
        }
        for index, close in enumerate(closes)
    ]


def flat_then_weak_prices(volume=20_000_000):
    closes = [100.0] * 55 + [99.5, 99.2, 99.0, 98.8, 98.5]
    return [
        {
            "date": f"2026-04-{(index % 28) + 1:02d}",
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": volume,
        }
        for index, close in enumerate(closes)
    ]


def slow_but_healthy_prices(volume=20_000_000):
    closes = [100.0 + index * 0.05 for index in range(60)]
    return [
        {
            "date": f"2026-05-{(index % 28) + 1:02d}",
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": volume,
        }
        for index, close in enumerate(closes)
    ]


class StrategyLogicTests(unittest.TestCase):
    def test_buy_candidate_passes_and_scores(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, rising_prices(), 160.0, held_symbols=set())

        self.assertTrue(result.passed, result.reason)
        self.assertGreater(result.score, 0)
        self.assertGreater(result.metrics["ma5"], result.metrics["ma20"])
        self.assertGreater(result.metrics["ma20"], result.metrics["ma60"])
        self.assertEqual(
            result.score,
            sum(item["points"] for item in result.metrics["score_breakdown"].values()),
        )
        self.assertEqual(result.metrics["score_breakdown"]["ma_alignment"]["points"], 30)

    def test_rejected_candidate_still_has_score_breakdown(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, rising_prices(), 160.0, held_symbols={"AAPL"})

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "already held")
        self.assertGreater(result.score, 0)
        self.assertIn("score_breakdown", result.metrics)

    def test_cyclical_travel_risk_lowers_score_without_blocking(self):
        item = {"symbol": "BKNG", "exchange": "NASD", "name": "Booking Holdings", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, rising_prices(), 160.0, held_symbols=set())

        self.assertTrue(result.passed, result.reason)
        self.assertNotIn("geopolitical_oil_risk", result.metrics["score_breakdown"])
        self.assertEqual(result.metrics["cyclical_travel_risk_tier"], "travel_air_lodging")
        self.assertEqual(result.metrics["cyclical_travel_risk_penalty"], 10)
        self.assertEqual(result.metrics["score_breakdown"]["cyclical_travel_risk"]["points"], -10)
        self.assertEqual(result.score, 80)

    def test_cyclical_travel_risk_does_not_block_strong_candidate_ranking(self):
        risky = scanner.evaluate_buy_candidate(
            {"symbol": "BKNG", "exchange": "NASD", "name": "Booking Holdings", "asset_type": "STOCK"},
            rising_prices(),
            160.0,
            held_symbols=set(),
        )
        weaker = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=65,
            reason="passed",
            metrics={
                "avg_trade_value_20": 2_000_000_000,
                "volatility_20": 2.0,
                "recent_5d_return": 3.0,
            },
        )

        ranked = scanner.rank_candidates([weaker, risky])

        self.assertEqual(ranked[0].symbol, "BKNG")

    def test_recent_20d_spike_is_rejected(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, recent_20d_spike_prices(), 130.0, held_symbols=set())

        self.assertFalse(result.passed)
        self.assertIn("20-day return", result.reason)
        self.assertGreater(result.metrics["recent_20d_return"], 25)

    def test_near_52w_high_is_rejected_when_enough_data_exists(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(
            item,
            rising_prices(start=100.0, step=0.1, days=260),
            126.0,
            held_symbols=set(),
        )

        self.assertFalse(result.passed)
        self.assertIn("52-week high", result.reason)
        self.assertTrue(result.metrics["has_52w_high_data"])
        self.assertGreaterEqual(result.metrics["position_vs_52w_high_percent"], 98)

    def test_high_volatility_buy_candidate_is_rejected(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, high_volatility_prices(), 158.0, held_symbols=set())

        self.assertFalse(result.passed)
        self.assertIn("volatility", result.reason)

    def test_wide_intraday_range_buy_candidate_is_rejected(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, wide_intraday_range_prices(), 160.0, held_symbols=set())

        self.assertFalse(result.passed)
        self.assertIn("intraday range", result.reason)

    def test_large_opening_gap_buy_candidate_is_rejected(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, large_gap_prices(), 155.5, held_symbols=set())

        self.assertFalse(result.passed)
        self.assertIn("opening gap", result.reason)

    def test_large_opening_gap_down_buy_candidate_is_rejected(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, large_gap_down_prices(), 160.0, held_symbols=set())

        self.assertFalse(result.passed)
        self.assertIn("gap down", result.reason)

    def test_previous_day_drop_buy_candidate_is_rejected(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, previous_day_drop_prices(), 160.0, held_symbols=set())

        self.assertFalse(result.passed)
        self.assertIn("previous day", result.reason)

    def test_recent_3d_drop_buy_candidate_is_rejected(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, recent_3d_drop_prices(), 160.0, held_symbols=set())

        self.assertFalse(result.passed)
        self.assertIn("3-day", result.reason)

    def test_negative_volume_spike_buy_candidate_is_rejected(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, negative_volume_spike_prices(), 160.0, held_symbols=set())

        self.assertFalse(result.passed)
        self.assertIn("volume spike", result.reason)

    def test_relative_strength_vs_qqq_adds_score_when_positive(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        qqq_metrics = scanner.calculate_metrics(slow_but_healthy_prices(), 103.0)
        result = scanner.evaluate_buy_candidate(
            item,
            rising_prices(),
            160.0,
            held_symbols=set(),
            benchmark_metrics=qqq_metrics,
        )

        self.assertTrue(result.passed, result.reason)
        self.assertTrue(result.metrics["relative_strength_positive"])
        self.assertEqual(result.metrics["score_breakdown"]["relative_strength_vs_qqq"]["points"], 10)

    def test_relative_strength_vs_qqq_penalizes_when_not_positive(self):
        item = {"symbol": "AAPL", "exchange": "NASD", "name": "Apple Inc.", "asset_type": "STOCK"}
        qqq_metrics = scanner.calculate_metrics(rising_prices(start=100.0, step=2.0), 220.0)
        result = scanner.evaluate_buy_candidate(
            item,
            rising_prices(),
            160.0,
            held_symbols=set(),
            benchmark_metrics=qqq_metrics,
        )

        self.assertTrue(result.passed, result.reason)
        self.assertFalse(result.metrics["relative_strength_positive"])
        self.assertEqual(result.metrics["score_breakdown"]["relative_strength_vs_qqq"]["points"], -10)

    def test_manual_block_list_rejects_hon(self):
        item = {"symbol": "HON", "exchange": "NASD", "name": "Honeywell International Inc.", "asset_type": "STOCK"}
        result = scanner.evaluate_buy_candidate(item, rising_prices(), 160.0, held_symbols=set())

        self.assertFalse(result.passed)
        self.assertIn("manually blocked", result.reason)

    def test_buy_decision_uses_total_asset_target_ratio(self):
        selected = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=90,
            reason="passed",
            metrics={"current_price": 160.0},
        )

        decision = auto_trader._build_buy_decision(
            selected,
            cash_usd=100_000.0,
            total_asset_usd=100_000.0,
            positions=[],
            state={},
        )

        self.assertEqual(decision["action"], "DRY_RUN_BUY")
        self.assertEqual(decision["qty"], 59)
        self.assertAlmostEqual(decision["amount"], 9449.44, places=2)

    def test_first_share_exception_allows_small_account_buy(self):
        selected = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=90,
            reason="passed",
            metrics={"current_price": 160.0},
        )

        decision = auto_trader._build_buy_decision(
            selected,
            cash_usd=100_000.0,
            total_asset_usd=500.0,
            positions=[],
            state={},
        )

        self.assertEqual(decision["action"], "DRY_RUN_BUY")
        self.assertEqual(decision["qty"], 1)

    def test_small_account_allows_expensive_first_share(self):
        selected = scanner.ScanResult(
            symbol="QQQ",
            exchange="NASD",
            passed=True,
            score=80,
            reason="qqq fallback passed",
            metrics={"current_price": 720.0},
        )

        decision = auto_trader._build_buy_decision(
            selected,
            cash_usd=100_000.0,
            total_asset_usd=800.0,
            positions=[],
            state={},
        )

        self.assertEqual(decision["action"], "DRY_RUN_BUY")
        self.assertEqual(decision["qty"], 1)

    def test_small_account_rejects_first_share_using_most_cash(self):
        selected = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=90,
            reason="passed",
            metrics={"current_price": 590.0},
        )

        decision = auto_trader._build_buy_decision(
            selected,
            cash_usd=600.0,
            total_asset_usd=800.0,
            positions=[],
            state={},
        )

        self.assertEqual(decision["action"], "NO_BUY")
        self.assertIn("too much available cash", decision["reason"])

    def test_sizing_total_asset_override_limits_mock_buyable_cash(self):
        original = auto_trader.config.SIZING_TOTAL_ASSET_USD
        auto_trader.config.SIZING_TOTAL_ASSET_USD = 720.0
        try:
            self.assertEqual(auto_trader._sizing_total_asset_usd(0, 100_000.0), 720.0)
            self.assertEqual(auto_trader._sizing_total_asset_usd(2_000.0, 100_000.0), 2_000.0)
        finally:
            auto_trader.config.SIZING_TOTAL_ASSET_USD = original

    def test_grown_account_blocks_first_share_over_max_position_ratio(self):
        selected = scanner.ScanResult(
            symbol="QQQ",
            exchange="NASD",
            passed=True,
            score=80,
            reason="qqq fallback passed",
            metrics={"current_price": 720.0},
        )

        decision = auto_trader._build_buy_decision(
            selected,
            cash_usd=100_000.0,
            total_asset_usd=2_500.0,
            positions=[],
            state={},
        )

        self.assertEqual(decision["action"], "NO_BUY")
        self.assertIn("exceeds MAX_POSITION_RATIO", decision["reason"])

    def test_small_account_growth_mode_applies_until_2000_usd(self):
        self.assertTrue(auto_trader._is_small_account(2_000.0))
        self.assertFalse(auto_trader._is_small_account(2_000.01))
        self.assertEqual(auto_trader._max_positions_for_account(2_000.0), 3)
        self.assertEqual(auto_trader._max_positions_for_account(2_000.01), auto_trader.config.MAX_POSITIONS)
        self.assertEqual(auto_trader._target_position_ratio_for_account(2_000.0), 0.30)

    def test_weak_market_allows_high_score_relative_strength_stock(self):
        selected = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=95,
            reason="passed",
            metrics={
                "current_price": 110.0,
                "ma20": 100.0,
                "ma60": 90.0,
            },
        )
        qqq_metrics = {
            "current_price": 95.0,
            "ma20": 100.0,
            "ma60": 100.0,
        }

        decision = auto_trader._build_buy_decision(
            selected,
            cash_usd=100_000.0,
            total_asset_usd=100_000.0,
            positions=[],
            state={},
            qqq_metrics=qqq_metrics,
            market_state="weak",
        )

        self.assertEqual(decision["action"], "DRY_RUN_BUY")

    def test_weak_market_buy_count_requires_explicit_enablement(self):
        original = auto_trader.config.ALLOW_WEAK_MARKET_RELATIVE_STRENGTH_BUY
        auto_trader.config.ALLOW_WEAK_MARKET_RELATIVE_STRENGTH_BUY = False
        try:
            self.assertEqual(auto_trader._max_buy_count_for_market("weak"), 0)
        finally:
            auto_trader.config.ALLOW_WEAK_MARKET_RELATIVE_STRENGTH_BUY = original

    def test_pending_buy_counts_toward_daily_buy_limit(self):
        decision = {"action": "BUY_SUBMITTED_NOT_CONFIRMED", "amount": 200.0}

        self.assertTrue(auto_trader._counts_toward_buy_limit(decision))

    def test_unconfirmed_buy_remains_pending_for_later_cancel_sweep(self):
        selected = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=90,
            reason="passed",
            metrics={"current_price": 160.0},
        )
        decision = {
            "action": "BUY",
            "qty": 1,
            "price": 160.0,
            "amount": 160.0,
            "score": 90,
        }
        state = {}
        client = FakeUnconfirmedBuyClient()
        original_dry_run = auto_trader.config.DRY_RUN
        original_wait = auto_trader.config.ORDER_CONFIRM_WAIT_SECONDS
        original_retries = auto_trader.config.ORDER_CONFIRM_RETRIES
        auto_trader.config.DRY_RUN = False
        auto_trader.config.ORDER_CONFIRM_WAIT_SECONDS = 0
        auto_trader.config.ORDER_CONFIRM_RETRIES = 0
        try:
            result = auto_trader._execute_buy_decision(client, selected, decision, state, positions=[])
        finally:
            auto_trader.config.DRY_RUN = original_dry_run
            auto_trader.config.ORDER_CONFIRM_WAIT_SECONDS = original_wait
            auto_trader.config.ORDER_CONFIRM_RETRIES = original_retries

        self.assertEqual(result["action"], "BUY_SUBMITTED_NOT_CONFIRMED")
        self.assertEqual(len(client.cancel_orders), 0)
        today = state["orders_by_date"][auto_trader._today_key()]
        self.assertEqual(today["buy_attempts"], ["AAPL"])
        self.assertEqual(today["pending_buys"], ["AAPL"])

    def test_extract_order_ids_from_kis_order_response(self):
        response = {"output": {"KRX_FWDG_ORD_ORGNO": "001", "ODNO": "12345"}}

        self.assertEqual(
            kis_overseas.extract_order_ids(response),
            {"org_no": "001", "order_no": "12345"},
        )

    def test_extract_positions_from_present_balance_fields(self):
        balance = {
            "output1": [
                {
                    "pdno": "LIN",
                    "prdt_name": "Linde plc",
                    "cblc_qty13": "1",
                    "avg_unpr3": "519.69",
                    "frcr_pchs_amt": "519.690000",
                    "frcr_evlu_amt2": "518.940000",
                    "evlu_pfls_amt2": "-0.750000",
                    "evlu_pfls_rt1": "-0.14",
                }
            ]
        }

        positions = kis_overseas.extract_positions(balance)

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["symbol"], "LIN")
        self.assertEqual(positions[0]["qty"], 1.0)
        self.assertEqual(positions[0]["avg_price"], 519.69)
        self.assertEqual(positions[0]["market_value"], 518.94)
        self.assertEqual(positions[0]["profit_loss"], -0.75)

    def test_portfolio_snapshot_uses_exchange_balance_positions(self):
        balance = portfolio_exporter.get_portfolio_balance_snapshot(FakePortfolioBalanceClient())
        snapshot = portfolio_exporter.build_portfolio_snapshot(balance)

        self.assertEqual(len(snapshot["positions"]), 1)
        self.assertEqual(snapshot["positions"][0]["symbol"], "LIN")
        self.assertEqual(snapshot["positions"][0]["quantity"], 1.0)
        self.assertEqual(snapshot["positions"][0]["total_current_value"], 518.94)
        self.assertEqual(snapshot["account"]["cash_asset_usd"], 126.8)
        self.assertEqual(snapshot["account"]["stock_asset_usd"], 518.94)
        self.assertEqual(snapshot["account"]["total_asset_usd"], 645.74)
        self.assertEqual(snapshot["exchange_rate_usd_krw"], 1300.0)

    def test_portfolio_dashboard_uses_krw_and_daily_history(self):
        balance = portfolio_exporter.get_portfolio_balance_snapshot(FakePortfolioBalanceClient())
        snapshot = portfolio_exporter.build_portfolio_snapshot(balance)
        snapshot["updated_at"] = "2026-07-01T01:00:00+00:00"
        previous = {
            "asset_history": [
                {
                    "date": "2026-06-30",
                    "total_asset": 800000,
                    "stock_asset": 600000,
                    "cash_asset": 200000,
                }
            ]
        }

        dashboard = portfolio_exporter.build_portfolio_dashboard(snapshot, previous)

        self.assertEqual(dashboard["summary"]["currency"], "KRW")
        self.assertEqual(dashboard["summary"]["cash_asset"], 164840)
        self.assertEqual(dashboard["summary"]["stock_asset"], 674622)
        self.assertEqual(dashboard["summary"]["total_asset"], 839462)
        self.assertNotIn("total_cash_invested", dashboard["summary"])
        self.assertEqual(dashboard["summary"]["total_profit_loss"], -160538)
        self.assertEqual(dashboard["summary"]["total_profit_loss_rate"], -16.05)
        self.assertNotIn("cash_flows", dashboard)
        self.assertEqual(dashboard["portfolio_allocation"][0]["symbol"], "LIN")
        self.assertEqual(dashboard["portfolio_allocation"][0]["sector"], "소재")
        self.assertEqual(dashboard["portfolio_allocation"][0]["weight"], 100.0)
        self.assertEqual(dashboard["sector_allocation"][0]["sector"], "소재")
        self.assertEqual(len(dashboard["asset_history"]), 2)
        self.assertEqual(dashboard["asset_history"][0]["total_asset"], 1_000_000)
        self.assertEqual(dashboard["asset_history"][0]["daily_cash_flow"], 1_000_000)
        self.assertEqual(dashboard["asset_history"][0]["net_cash_invested"], 1_000_000)
        self.assertEqual(dashboard["asset_history"][-1]["daily_cash_flow"], 0)
        self.assertEqual(dashboard["asset_history"][-1]["net_cash_invested"], 1_000_000)
        self.assertEqual(dashboard["asset_history"][-1]["daily_profit_loss"], -160538)
        self.assertEqual(dashboard["asset_history"][-1]["cumulative_profit_loss"], -160538)

    def test_portfolio_dashboard_tracks_cash_flows_separately_from_returns(self):
        original_flows_json = portfolio_exporter.config.DASHBOARD_CASH_FLOWS_JSON
        try:
            portfolio_exporter.config.DASHBOARD_CASH_FLOWS_JSON = json.dumps(
                [
                    {
                        "date": "2026-06-30",
                        "type": "deposit",
                        "amount": 1_000_000,
                        "currency": "KRW",
                        "note": "initial deposit",
                    },
                    {
                        "date": "2026-07-02",
                        "type": "deposit",
                        "amount": 500_000,
                        "currency": "KRW",
                        "note": "extra deposit",
                    },
                    {
                        "date": "2026-07-03",
                        "type": "withdrawal",
                        "amount": 100_000,
                        "currency": "KRW",
                        "note": "withdrawal",
                    },
                ]
            )
            snapshot = {
                "updated_at": "2026-07-03T01:00:00+00:00",
                "exchange_rate_usd_krw": 1000,
                "positions": [],
                "account": {
                    "cash_asset_usd": 1450,
                    "stock_asset_usd": 0,
                    "total_asset_usd": 1450,
                },
            }
            previous = {
                "asset_history": [
                    {
                        "date": "2026-07-02",
                        "total_asset": 1_520_000,
                        "stock_asset": 0,
                        "cash_asset": 1_520_000,
                    }
                ]
            }

            dashboard = portfolio_exporter.build_portfolio_dashboard(snapshot, previous)

            self.assertNotIn("total_cash_invested", dashboard["summary"])
            self.assertNotIn("cash_flows", dashboard)
            rows = {row["date"]: row for row in dashboard["asset_history"]}
            self.assertEqual(rows["2026-07-02"]["daily_cash_flow"], 500_000)
            self.assertEqual(rows["2026-07-02"]["net_cash_invested"], 1_500_000)
            self.assertEqual(rows["2026-07-02"]["daily_profit_loss"], 20_000)
            self.assertEqual(rows["2026-07-02"]["cumulative_profit_loss"], 20_000)
            self.assertEqual(rows["2026-07-03"]["daily_cash_flow"], -100_000)
            self.assertEqual(rows["2026-07-03"]["net_cash_invested"], 1_400_000)
            self.assertEqual(rows["2026-07-03"]["daily_profit_loss"], 30_000)
            self.assertEqual(rows["2026-07-03"]["cumulative_profit_loss"], 50_000)
        finally:
            portfolio_exporter.config.DASHBOARD_CASH_FLOWS_JSON = original_flows_json

    def test_portfolio_dashboard_history_date_uses_korean_market_day(self):
        snapshot = {
            "updated_at": "2026-07-01T15:30:00+00:00",
            "exchange_rate_usd_krw": 1000,
            "positions": [],
            "account": {
                "cash_asset_usd": 1000,
                "stock_asset_usd": 0,
                "total_asset_usd": 1000,
            },
        }

        dashboard = portfolio_exporter.build_portfolio_dashboard(snapshot, previous_dashboard={})

        self.assertEqual(dashboard["asset_history"][-1]["date"], "2026-07-02")

    def test_extract_open_orders_from_kis_response(self):
        data = {
            "output": [
                {
                    "ovrs_excg_cd": "NASD",
                    "pdno": "AAPL",
                    "nccs_qty": "1",
                    "ovrs_ord_unpr": "160.12",
                    "krx_fwdg_ord_orgno": "001",
                    "odno": "12345",
                }
            ]
        }

        orders = kis_overseas.extract_open_orders(data)

        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["symbol"], "AAPL")
        self.assertEqual(orders[0]["exchange"], "NASD")
        self.assertEqual(orders[0]["qty"], 1.0)
        self.assertEqual(orders[0]["price"], 160.12)
        self.assertEqual(orders[0]["order_no"], "12345")

    def test_cancel_open_orders_cancels_all_found_orders(self):
        client = FakeOpenOrdersClient()
        original_dry_run = auto_trader.config.DRY_RUN
        auto_trader.config.DRY_RUN = False
        try:
            logs = auto_trader._cancel_open_orders(client, exchanges=("NASD", "NYSE"))
        finally:
            auto_trader.config.DRY_RUN = original_dry_run

        self.assertEqual(len(client.cancel_orders), 1)
        self.assertEqual(client.cancel_orders[0][0], "AAPL")
        self.assertEqual(client.cancel_orders[0][3], "NASD")
        self.assertTrue(any(row["action"] == "CANCEL_OPEN_ORDER_SUBMITTED" for row in logs))

    def test_rejected_buy_candidate_is_collected_with_reason(self):
        selected = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=95,
            reason="passed",
            metrics={"current_price": 160.0},
        )
        rejected = []

        candidate, decision = auto_trader._choose_affordable_candidate(
            client=FakeClient(buyable_amount=10.0),
            candidates=[selected],
            cash_usd=10.0,
            total_asset_usd=1000.0,
            positions=[],
            state={},
            qqq_metrics=None,
            market_state="normal",
            rejected_candidates=rejected,
        )

        self.assertIsNone(candidate)
        self.assertIsNone(decision)
        self.assertEqual(rejected[0][0].symbol, "AAPL")
        self.assertEqual(rejected[0][1]["action"], "NO_BUY")
        self.assertIn("cash", rejected[0][1])

    def test_weak_market_rejects_low_score_candidate(self):
        selected = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=80,
            reason="passed",
            metrics={
                "current_price": 110.0,
                "ma20": 100.0,
                "ma60": 90.0,
            },
        )
        qqq_metrics = {
            "current_price": 95.0,
            "ma20": 100.0,
            "ma60": 100.0,
        }

        decision = auto_trader._build_buy_decision(
            selected,
            cash_usd=100_000.0,
            total_asset_usd=100_000.0,
            positions=[],
            state={},
            qqq_metrics=qqq_metrics,
            market_state="weak",
        )

        self.assertEqual(decision["action"], "NO_BUY")
        self.assertIn("weak market requires high", decision["reason"])

    def test_weak_market_can_buy_high_score_candidate_without_relative_strength_by_default(self):
        selected = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=95,
            reason="passed",
            metrics={
                "current_price": 97.0,
                "ma20": 100.0,
                "ma60": 100.0,
            },
        )
        qqq_metrics = {
            "current_price": 98.0,
            "ma20": 100.0,
            "ma60": 100.0,
        }

        decision = auto_trader._build_buy_decision(
            selected,
            cash_usd=100_000.0,
            total_asset_usd=100_000.0,
            positions=[],
            state={},
            qqq_metrics=qqq_metrics,
            market_state="weak",
        )

        self.assertEqual(decision["action"], "DRY_RUN_BUY")

    def test_weak_market_rejects_candidate_without_relative_strength_when_required(self):
        selected = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=95,
            reason="passed",
            metrics={
                "current_price": 97.0,
                "ma20": 100.0,
                "ma60": 100.0,
            },
        )
        qqq_metrics = {
            "current_price": 98.0,
            "ma20": 100.0,
            "ma60": 100.0,
        }
        original = auto_trader.config.REQUIRE_WEAK_MARKET_RELATIVE_STRENGTH
        auto_trader.config.REQUIRE_WEAK_MARKET_RELATIVE_STRENGTH = True
        try:
            decision = auto_trader._build_buy_decision(
                selected,
                cash_usd=100_000.0,
                total_asset_usd=100_000.0,
                positions=[],
                state={},
                qqq_metrics=qqq_metrics,
                market_state="weak",
            )
        finally:
            auto_trader.config.REQUIRE_WEAK_MARKET_RELATIVE_STRENGTH = original

        self.assertEqual(decision["action"], "NO_BUY")
        self.assertIn("relative strength", decision["reason"])

    def test_reentry_after_stale_position_requires_higher_score(self):
        state = {
            "positions": {},
            "orders_by_date": {},
            "last_sells": {
                "AAPL": {
                    "date": "2026-06-01",
                    "reason": "STALE_POSITION",
                    "action": "SELL_CONFIRMED",
                }
            },
            "token": {},
        }
        selected = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=88,
            reason="passed",
            metrics={"current_price": 160.0},
        )

        decision = auto_trader._build_buy_decision(
            selected,
            cash_usd=100_000.0,
            total_asset_usd=100_000.0,
            positions=[],
            state=state,
        )

        self.assertEqual(decision["action"], "NO_BUY")
        self.assertIn("reentry", decision["reason"])

    def test_stop_loss_sell_rule(self):
        position = {"symbol": "AAPL", "qty": 2, "avg_price": 120.0, "profit_rate": -9.0}
        prices = rising_prices(start=120.0, step=-1.0)

        decision = scanner.evaluate_sell_decision(position, prices, current_price=60.0)

        self.assertEqual(decision["action"], "SELL_ALL")
        self.assertEqual(decision["sell_reason_code"], "STOP_LOSS")

    def test_early_drawdown_sell_rule(self):
        position = {
            "symbol": "AAPL",
            "qty": 1,
            "avg_price": 183.2,
            "profit_rate": -4.9,
            "holding_days": 1,
        }

        decision = scanner.evaluate_sell_decision(position, flat_then_weak_prices(), current_price=94.0)

        self.assertEqual(decision["action"], "SELL_ALL")
        self.assertEqual(decision["sell_reason_code"], "EARLY_DRAWDOWN")

    def test_shallow_ma20_break_holds_when_trend_is_still_buyable(self):
        position = {"symbol": "AAPL", "qty": 2, "avg_price": 140.0, "profit_rate": 9.0}
        prices = shallow_ma20_break_prices()

        decision = scanner.evaluate_sell_decision(position, prices, current_price=153.0)

        self.assertEqual(decision["action"], "HOLD")
        self.assertEqual(decision["deferred_sell_reason_code"], "PROFIT_MA20_BREAK")
        self.assertIn("trend remains buyable", decision["reason"])

    def test_single_share_position_skips_half_sell(self):
        position = {"symbol": "AAPL", "qty": 1, "avg_price": 100.0, "profit_rate": 12.0}
        client = FakePriceClient(rising_prices(), 160.0)

        decision = auto_trader._handle_sell_decision(client, position, {})

        self.assertEqual(decision["action"], "HOLD")
        self.assertIn("single-share", decision["reason"])
        self.assertEqual(client.sell_orders, [])

    def test_deep_ma20_break_still_sells(self):
        position = {"symbol": "AAPL", "qty": 2, "avg_price": 130.0, "profit_rate": 8.0}
        prices = shallow_ma20_break_prices()

        decision = scanner.evaluate_sell_decision(position, prices, current_price=145.0)

        self.assertEqual(decision["action"], "SELL_ALL")
        self.assertEqual(decision["sell_reason_code"], "PROFIT_MA20_BREAK")

    def test_stale_position_sells_when_profit_is_weak_and_momentum_fades(self):
        position = {
            "symbol": "AAPL",
            "qty": 2,
            "avg_price": 100.0,
            "profit_rate": 4.0,
            "holding_days": 15,
        }

        decision = scanner.evaluate_sell_decision(position, flat_then_weak_prices(), current_price=98.5)

        self.assertEqual(decision["action"], "SELL_ALL")
        self.assertEqual(decision["sell_reason_code"], "STALE_POSITION")

    def test_stale_position_holds_when_slow_trend_is_still_healthy(self):
        position = {
            "symbol": "AAPL",
            "qty": 2,
            "avg_price": 100.0,
            "profit_rate": 4.0,
            "holding_days": 15,
        }

        decision = scanner.evaluate_sell_decision(position, slow_but_healthy_prices(), current_price=103.0)

        self.assertEqual(decision["action"], "HOLD")

    def test_cooldown_blocks_rebuy(self):
        state = {
            "positions": {},
            "orders_by_date": {},
            "last_sells": {
                "AAPL": {
                    "date": auto_trader._today_key(),
                    "reason": "STOP_LOSS",
                    "action": "SELL_CONFIRMED",
                }
            },
            "token": {},
        }

        selected = scanner.ScanResult(
            symbol="AAPL",
            exchange="NASD",
            passed=True,
            score=90,
            reason="passed",
            metrics={"current_price": 160.0},
        )

        decision = auto_trader._build_buy_decision(
            selected,
            cash_usd=100_000.0,
            total_asset_usd=100_000.0,
            positions=[],
            state=state,
        )

        self.assertEqual(decision["action"], "NO_BUY")
        self.assertTrue(decision["blocked"])

    def test_buy_attempt_is_not_treated_as_confirmed_buy(self):
        state = {
            "positions": {},
            "orders_by_date": {
                auto_trader._today_key(): {
                    "buys": ["AAPL"],
                    "orders": [
                        {
                            "symbol": "AAPL",
                            "action": "BUY_ATTEMPT",
                            "qty": 1,
                            "price": 200.0,
                            "amount": 200.0,
                        }
                    ],
                    "sells": [],
                }
            },
            "last_sells": {},
            "token": {},
        }

        normalized = auto_trader._normalize_state(state)

        today = normalized["orders_by_date"][auto_trader._today_key()]
        self.assertEqual(today["buys"], [])
        self.assertEqual(today["buy_attempts"], ["AAPL"])
        self.assertTrue(auto_trader._already_bought_today(normalized, "AAPL"))

    def test_json_decision_payload_merges_duplicate_symbol_rows(self):
        rows = [
            {"event": "MARKET_FILTER", "symbol": "QQQ", "reason": "weak"},
            {
                "event": "BUY_CANDIDATE_REJECTED",
                "symbol": "AMGN",
                "passed": True,
                "score": 100,
                "action": "NO_BUY",
                "reason": "cash",
                "current_price": 359.0,
            },
            {
                "event": "SCAN",
                "symbol": "AMGN",
                "passed": True,
                "score": 100,
                "reason": "passed",
                "ma20": 345.0,
                "score_breakdown": {"ma_alignment": {"points": 30}},
            },
        ]

        payload = auto_trader._json_log_payload(rows, "test_log")
        amgn_rows = [row for row in payload["rows"] if row.get("symbol") == "AMGN"]
        raw_amgn_rows = [row for row in payload["raw_rows"] if row.get("symbol") == "AMGN"]

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(len(amgn_rows), 1)
        self.assertEqual(len(raw_amgn_rows), 2)
        self.assertEqual(amgn_rows[0]["event"], "BUY_CANDIDATE_REJECTED")
        self.assertEqual(amgn_rows[0]["reason"], "cash")
        self.assertEqual(amgn_rows[0]["ma20"], 345.0)
        self.assertIn("score_breakdown", amgn_rows[0])

    def test_log_row_adds_korean_reason(self):
        row = auto_trader._log_row(
            "BUY_CHECK",
            "",
            decision={"action": "NO_BUY", "reason": "candidate is not affordable with available cash"},
        )

        self.assertEqual(row["reason_ko"], "사용 가능 현금으로 매수 불가")

    def test_position_state_uses_confirmed_buy_date_for_holding_days(self):
        state = {
            "positions": {},
            "orders_by_date": {
                "2026-06-01": {
                    "buys": ["AAPL"],
                    "buy_attempts": [],
                    "pending_buys": [],
                    "sells": [],
                    "orders": [
                        {
                            "symbol": "AAPL",
                            "action": "BUY_CONFIRMED",
                            "qty": 1,
                            "price": 100.0,
                            "amount": 100.0,
                        }
                    ],
                }
            },
            "last_sells": {},
            "token": {},
        }

        positions = auto_trader._merge_position_state(
            [{"symbol": "AAPL", "qty": 1, "avg_price": 100.0}],
            state,
        )

        self.assertEqual(positions[0]["entry_date"], "2026-06-01")
        self.assertIsInstance(positions[0]["holding_days"], int)

    def test_us_market_holidays_are_skipped(self):
        self.assertFalse(market_hours.is_us_market_day("2026-06-19"))
        self.assertFalse(market_hours.is_us_market_day("2026-07-03"))
        self.assertFalse(market_hours.is_us_market_day("2026-07-04"))
        self.assertTrue(market_hours.is_us_market_day("2026-06-18"))


if __name__ == "__main__":
    unittest.main()
