import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from kis_overseas import extract_positions, extract_usd_cash

DEFAULT_SECTOR = "기타"
SECTOR_BY_SYMBOL = {
    "AAPL": "정보기술",
    "MSFT": "정보기술",
    "NVDA": "반도체",
    "AVGO": "반도체",
    "AMD": "반도체",
    "ASML": "반도체",
    "AMZN": "소비재",
    "META": "커뮤니케이션",
    "GOOG": "커뮤니케이션",
    "GOOGL": "커뮤니케이션",
    "NFLX": "커뮤니케이션",
    "TSLA": "자동차",
    "LIN": "소재",
    "QQQ": "ETF",
    "SPY": "ETF",
}


def export_portfolio_after_trade(client, trade_event):
    balance = get_portfolio_balance_snapshot(client)
    snapshot = build_portfolio_snapshot(balance)
    return push_portfolio_snapshot(snapshot, trade_event)


def export_portfolio_snapshot(client, reason="scheduled_snapshot"):
    balance = get_portfolio_balance_snapshot(client)
    snapshot = build_portfolio_snapshot(balance)
    return push_portfolio_snapshot(snapshot, {"action": reason, "symbol": "PORTFOLIO"})


def get_portfolio_balance_snapshot(client):
    balance = client.get_present_balance()
    merged = dict(balance)
    merged_positions = list(balance.get("output1", []) or [])
    seen_symbols = {
        position["symbol"]
        for position in extract_positions({"output1": merged_positions})
    }

    for exchange in ("NASD", "NYSE", "AMEX"):
        try:
            exchange_balance = client.get_balance(exchange=exchange, currency="USD")
        except Exception as error:
            print(f"[PORTFOLIO_BALANCE_LOOKUP_FAILED] {exchange}: {error}")
            continue
        for item in exchange_balance.get("output1", []) or []:
            if not isinstance(item, dict):
                continue
            symbol = (item.get("ovrs_pdno") or item.get("pdno") or item.get("trad_pdno") or "").upper()
            if not symbol or symbol in seen_symbols:
                continue
            _copy_present_balance_metadata(item, merged_positions, symbol)
            merged_positions.append(item)
            seen_symbols.add(symbol)

    merged["output1"] = merged_positions
    return merged


def build_portfolio_snapshot(balance):
    positions = []
    stock_asset_usd = 0.0

    for position in extract_positions(balance):
        symbol = position["symbol"]
        qty = float(position.get("qty") or 0)
        avg_price = float(position.get("avg_price") or 0)
        purchase_amount = position.get("purchase_amount")
        if purchase_amount is None:
            purchase_amount = qty * avg_price

        market_value = position.get("market_value")
        profit_loss = position.get("profit_loss")
        if profit_loss is None and market_value is not None:
            profit_loss = market_value - purchase_amount

        profit_loss_rate = position.get("profit_rate")
        if profit_loss_rate is None and purchase_amount:
            profit_loss_rate = (profit_loss / purchase_amount) * 100

        if market_value is not None:
            stock_asset_usd += market_value

        positions.append(
            {
                "symbol": symbol,
                "name": position.get("name", ""),
                "average_purchase_price": avg_price,
                "quantity": qty,
                "total_purchase_amount": purchase_amount,
                "total_current_value": market_value,
                "profit_loss": profit_loss,
                "profit_loss_rate": profit_loss_rate,
                "exchange_rate": position.get("exchange_rate"),
            }
        )

    cash_asset_usd = extract_usd_cash(balance)
    total_asset_usd = cash_asset_usd + stock_asset_usd

    snapshot = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "data_type": "portfolio_snapshot",
        "currency": "USD",
        "exchange_rate_usd_krw": _snapshot_exchange_rate(positions),
        "is_realtime": False,
        "snapshot_note": "Last account balance snapshot. Not real-time market data.",
        "account": {
            "cash_asset_usd": cash_asset_usd,
            "stock_asset_usd": stock_asset_usd,
            "total_asset_usd": total_asset_usd,
        },
        "positions": sorted(positions, key=lambda item: item["symbol"]),
    }

    for position in snapshot["positions"]:
        current_value = position.get("total_current_value") or 0
        position["portfolio_weight_percent"] = round(_percent(current_value, total_asset_usd), 2)

    return snapshot


def _snapshot_exchange_rate(positions):
    for position in positions:
        if position.get("exchange_rate"):
            return position["exchange_rate"]
    return None


def build_portfolio_dashboard(snapshot, previous_dashboard=None, cash_flows=None):
    exchange_rate = _extract_usd_krw_rate(snapshot) or 1.0
    today = _date_key(snapshot.get("updated_at"))
    positions = snapshot.get("positions", [])
    account = snapshot.get("account", {})

    cash_asset = _money_krw(account.get("cash_asset_usd", 0), exchange_rate)
    stock_asset = _money_krw(account.get("stock_asset_usd", 0), exchange_rate)
    total_asset = _money_krw(account.get("total_asset_usd", 0), exchange_rate)
    total_profit_loss = _money_krw(
        sum(position.get("profit_loss") or 0 for position in positions),
        exchange_rate,
    )
    total_purchase_amount = _money_krw(
        sum(position.get("total_purchase_amount") or 0 for position in positions),
        exchange_rate,
    )
    cash_flows = cash_flows if cash_flows is not None else _cash_flows(previous_dashboard=previous_dashboard)
    asset_history = _updated_asset_history(
        previous_dashboard,
        today,
        total_asset,
        stock_asset,
        cash_asset,
        cash_flows,
    )
    latest_history = asset_history[-1] if asset_history else {}
    if latest_history.get("net_cash_invested"):
        total_profit_loss = latest_history.get("cumulative_profit_loss", 0)
        total_profit_loss_rate = latest_history.get("cumulative_profit_loss_rate", 0)
    else:
        total_profit_loss_rate = round(_percent(total_profit_loss, total_purchase_amount), 2)

    dashboard = {
        "summary": {
            "currency": "KRW",
            "total_asset": total_asset,
            "stock_asset": stock_asset,
            "cash_asset": cash_asset,
            "total_profit_loss": total_profit_loss,
            "total_profit_loss_rate": total_profit_loss_rate,
            "updated_at": snapshot.get("updated_at", ""),
        },
        "asset_history": asset_history,
        "portfolio_allocation": _portfolio_allocation(positions, stock_asset, exchange_rate),
        "sector_allocation": [],
    }
    dashboard["sector_allocation"] = _sector_allocation(dashboard["portfolio_allocation"], stock_asset)
    return dashboard


def push_portfolio_snapshot(snapshot, trade_event=None):
    repo_dir = Path(config.PORTFOLIO_DATA_DIR)
    _ensure_repo(repo_dir)

    latest_path = repo_dir / "portfolio.json"
    dashboard_path = repo_dir / "portfolio_dashboard.json"
    cash_flows_path = repo_dir / "cash_flows.json"
    previous_dashboard = _read_json(dashboard_path)
    stored_cash_flows = _read_json(cash_flows_path)
    cash_flows = _cash_flows(
        previous_dashboard=previous_dashboard,
        stored_cash_flows=stored_cash_flows,
    )
    dashboard = build_portfolio_dashboard(snapshot, previous_dashboard, cash_flows)

    _write_json(latest_path, snapshot)
    _write_json(cash_flows_path, cash_flows)
    _write_json(dashboard_path, dashboard)

    _git(repo_dir, "add", "portfolio.json", "portfolio_dashboard.json", "cash_flows.json")
    if not _has_staged_changes(repo_dir):
        return {"pushed": False, "reason": "no portfolio changes"}

    event = trade_event or {}
    symbol = event.get("symbol", "UNKNOWN")
    action = event.get("action", "TRADE")
    _git(repo_dir, "commit", "-m", f"Update portfolio after {action} {symbol}")
    _git(repo_dir, "push", "-u", "origin", "HEAD")

    return {
        "pushed": True,
        "repo_dir": str(repo_dir),
        "latest_path": str(latest_path),
        "dashboard_path": str(dashboard_path),
    }


def load_trader_state():
    repo_dir = Path(config.PORTFOLIO_DATA_DIR)
    state_path = repo_dir / "trader_state.json"
    try:
        _ensure_repo(repo_dir)
        if not state_path.exists():
            return {}
        with state_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as error:
        print(f"[TRADER_STATE_LOAD_FAILED] {error}")
        return {}


def push_trader_state(state):
    repo_dir = Path(config.PORTFOLIO_DATA_DIR)
    _ensure_repo(repo_dir)
    state_path = repo_dir / "trader_state.json"
    _write_json(state_path, _public_trader_state(state))
    _git(repo_dir, "add", "trader_state.json")
    if not _has_staged_changes(repo_dir):
        return {"pushed": False, "reason": "no trader state changes"}
    _git(repo_dir, "commit", "-m", "Update trader state")
    _git(repo_dir, "push", "-u", "origin", "HEAD")
    return {"pushed": True, "state_path": str(state_path)}


def push_decision_log(decision_log, log_id):
    repo_dir = Path(config.PORTFOLIO_DATA_DIR)
    _ensure_repo(repo_dir)

    latest_path = repo_dir / "decision_log.json"
    history_dir = repo_dir / "decision_logs"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / f"{log_id}.json"

    _write_json(latest_path, decision_log)
    _write_json(history_path, decision_log)
    _git(repo_dir, "add", "decision_log.json", history_path.relative_to(repo_dir).as_posix())
    if not _has_staged_changes(repo_dir):
        return {"pushed": False, "reason": "no decision log changes"}

    _git(repo_dir, "commit", "-m", f"Update decision log {log_id}")
    _git(repo_dir, "push", "-u", "origin", "HEAD")
    return {
        "pushed": True,
        "latest_path": str(latest_path),
        "history_path": str(history_path),
    }


def _public_trader_state(state):
    public_state = dict(state)
    public_state.pop("token", None)
    return public_state


def should_push_for_decision(decision):
    action = decision.get("action", "")
    if action.startswith("DRY_RUN_"):
        return config.PUSH_PORTFOLIO_ON_DRY_RUN
    return action in {"BUY_CONFIRMED", "SELL_CONFIRMED"}


def make_trade_event(symbol, decision):
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "action": decision.get("action"),
        "quantity": decision.get("qty"),
        "price": decision.get("price"),
        "amount": decision.get("amount"),
        "reason": decision.get("reason"),
        "dry_run": str(decision.get("action", "")).startswith("DRY_RUN_"),
    }


def _ensure_repo(repo_dir):
    if (repo_dir / ".git").exists():
        _git(repo_dir, "pull", "--rebase")
        return

    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_dir.parent, "clone", config.PORTFOLIO_DATA_REPO_URL, repo_dir.name)


def _git(cwd, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {message}")
    return result.stdout.strip()


def _has_staged_changes(repo_dir):
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_dir),
        check=False,
    )
    return result.returncode == 1


def _write_json(path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def _read_json(path):
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as error:
        print(f"[JSON_READ_FAILED] {path}: {error}")
        return None


def _copy_present_balance_metadata(target, present_rows, symbol):
    for row in present_rows:
        if not isinstance(row, dict):
            continue
        row_symbol = (row.get("ovrs_pdno") or row.get("pdno") or row.get("trad_pdno") or "").upper()
        if row_symbol != symbol:
            continue
        for key in ("bass_exrt", "frst_bltn_exrt", "exrt"):
            if key in row and key not in target:
                target[key] = row[key]


def _extract_usd_krw_rate(snapshot):
    if snapshot.get("exchange_rate_usd_krw"):
        return snapshot["exchange_rate_usd_krw"]
    rates = []
    for position in snapshot.get("positions", []):
        rate = position.get("exchange_rate")
        if rate:
            rates.append(rate)
    return rates[0] if rates else None


def _cash_flows(previous_dashboard=None, stored_cash_flows=None):
    if config.DASHBOARD_CASH_FLOWS_JSON:
        try:
            raw_flows = json.loads(config.DASHBOARD_CASH_FLOWS_JSON)
            return _normalize_cash_flows(raw_flows)
        except Exception as error:
            print(f"[DASHBOARD_CASH_FLOWS_JSON_INVALID] {error}")

    stored_flows = _extract_cash_flows(stored_cash_flows)
    if stored_flows:
        return stored_flows

    previous_flows = _extract_cash_flows(previous_dashboard)
    if previous_flows:
        return previous_flows

    if not config.DASHBOARD_INITIAL_CAPITAL_KRW or not config.DASHBOARD_INITIAL_CAPITAL_DATE:
        return []
    return _normalize_cash_flows(
        [
            {
                "date": config.DASHBOARD_INITIAL_CAPITAL_DATE,
                "type": "deposit",
                "amount": config.DASHBOARD_INITIAL_CAPITAL_KRW,
                "currency": "KRW",
                "note": config.DASHBOARD_INITIAL_CAPITAL_NOTE,
            }
        ]
    )


def _extract_cash_flows(source):
    if isinstance(source, list):
        return _normalize_cash_flows(source)
    if isinstance(source, dict):
        return _normalize_cash_flows(source.get("cash_flows") or [])
    return []


def _normalize_cash_flows(raw_flows):
    flows = []
    if not isinstance(raw_flows, list):
        return flows

    for flow in raw_flows:
        if not isinstance(flow, dict):
            continue
        date = str(flow.get("date") or "")[:10]
        if not date:
            continue
        amount = _float(flow.get("amount"))
        if amount <= 0:
            continue
        flow_type = str(flow.get("type") or "deposit").lower()
        if flow_type not in {"deposit", "withdrawal"}:
            flow_type = "deposit"
        flows.append(
            {
                "date": date,
                "type": flow_type,
                "amount": round(amount),
                "currency": flow.get("currency") or "KRW",
                "note": flow.get("note") or "",
            }
        )

    return sorted(flows, key=lambda item: (item["date"], item["type"], item["amount"]))


def _cash_flow_amount(flow):
    amount = flow.get("amount") or 0
    if flow.get("type") == "withdrawal":
        return -amount
    return amount


def _cash_flow_by_date(cash_flows):
    by_date = {}
    for flow in cash_flows:
        date = flow.get("date")
        if not date:
            continue
        by_date[date] = by_date.get(date, 0) + _cash_flow_amount(flow)
    return by_date


def _cash_invested_by_date(cash_flows, dates):
    flow_by_date = _cash_flow_by_date(cash_flows)
    invested_by_date = {}
    running_total = 0
    for date in sorted(set(dates) | set(flow_by_date)):
        running_total += flow_by_date.get(date, 0)
        invested_by_date[date] = round(running_total)
    return invested_by_date


def _initial_capital_row(cash_flows):
    deposits = [flow for flow in cash_flows if flow.get("type") == "deposit"]
    if not deposits:
        return None
    first_date = min(flow["date"] for flow in deposits)
    initial_amount = sum(
        flow.get("amount") or 0
        for flow in deposits
        if flow.get("date") == first_date
    )
    if not initial_amount:
        return None
    return {
        "date": first_date,
        "total_asset": round(initial_amount),
        "stock_asset": 0,
        "cash_asset": round(initial_amount),
    }


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


KST = timezone(timedelta(hours=9))


def _date_key(value):
    if not value:
        return datetime.now(KST).date().isoformat()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(KST).date().isoformat()
    except ValueError:
        return str(value)[:10]


def _updated_asset_history(previous_dashboard, today, total_asset, stock_asset, cash_asset, cash_flows):
    previous_rows = []
    if isinstance(previous_dashboard, dict):
        previous_rows = list(previous_dashboard.get("asset_history", []) or [])

    rows_by_date = {
        row.get("date"): dict(row)
        for row in previous_rows
        if isinstance(row, dict) and row.get("date")
    }
    initial_row = _initial_capital_row(cash_flows)
    if initial_row:
        rows_by_date[initial_row["date"]] = initial_row
    rows_by_date[today] = {
        "date": today,
        "total_asset": total_asset,
        "stock_asset": stock_asset,
        "cash_asset": cash_asset,
    }

    sorted_dates = sorted(rows_by_date)
    invested_by_date = _cash_invested_by_date(cash_flows, sorted_dates)
    flow_by_date = _cash_flow_by_date(cash_flows)
    rows = [rows_by_date[key] for key in sorted_dates]
    previous_total = None
    for row in rows:
        date = row.get("date")
        current_total = row.get("total_asset", 0)
        daily_cash_flow = flow_by_date.get(date, 0)
        net_cash_invested = invested_by_date.get(date, 0)
        if previous_total is None:
            daily_profit_loss = current_total - daily_cash_flow
            daily_profit_loss_rate = 0
        else:
            daily_profit_loss = current_total - previous_total - daily_cash_flow
            daily_profit_loss_rate = _percent(daily_profit_loss, previous_total)
        cumulative_profit_loss = current_total - net_cash_invested
        row["daily_cash_flow"] = round(daily_cash_flow)
        row["net_cash_invested"] = round(net_cash_invested)
        row["daily_profit_loss"] = round(daily_profit_loss)
        row["daily_profit_loss_rate"] = round(daily_profit_loss_rate, 2)
        row["cumulative_profit_loss"] = round(cumulative_profit_loss)
        row["cumulative_profit_loss_rate"] = round(_percent(cumulative_profit_loss, net_cash_invested), 2)
        previous_total = current_total
    return rows


def _portfolio_allocation(positions, stock_asset, exchange_rate):
    rows = []
    for position in positions:
        symbol = position.get("symbol", "")
        current_value = _money_krw(position.get("total_current_value", 0), exchange_rate)
        quantity = float(position.get("quantity") or 0)
        rows.append(
            {
                "symbol": symbol,
                "name": position.get("name", ""),
                "sector": SECTOR_BY_SYMBOL.get(symbol, DEFAULT_SECTOR),
                "market": "US",
                "country": "US",
                "current_value": current_value,
                "weight": round(_percent(current_value, stock_asset), 2),
                "quantity": quantity,
                "current_price": _money_krw(_per_share(position.get("total_current_value"), quantity), exchange_rate),
                "avg_purchase_price": _money_krw(position.get("average_purchase_price", 0), exchange_rate),
                "profit_loss": _money_krw(position.get("profit_loss", 0), exchange_rate),
                "profit_loss_rate": round(float(position.get("profit_loss_rate") or 0), 2),
            }
        )
    return sorted(rows, key=lambda item: item["current_value"], reverse=True)


def _sector_allocation(portfolio_allocation, stock_asset):
    totals = {}
    for item in portfolio_allocation:
        sector = item.get("sector") or DEFAULT_SECTOR
        totals[sector] = totals.get(sector, 0) + (item.get("current_value") or 0)
    return [
        {
            "sector": sector,
            "current_value": round(value),
            "weight": round(_percent(value, stock_asset), 2),
        }
        for sector, value in sorted(totals.items(), key=lambda item: item[1], reverse=True)
    ]


def _money_krw(value, exchange_rate):
    return round(float(value or 0) * exchange_rate)


def _per_share(total_value, quantity):
    if not quantity:
        return 0
    return float(total_value or 0) / quantity


def _percent(value, base):
    if not base:
        return 0.0
    return (value / base) * 100
