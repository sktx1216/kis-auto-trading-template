import csv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
NASDAQ100_CSV = DATA_DIR / "nasdaq100.csv"
SP500_CSV = DATA_DIR / "sp500.csv"

UNIVERSE_FIELDS = ["symbol", "exchange", "name", "index", "asset_type"]

ETF_UNIVERSE = [
    {"symbol": "QQQ", "exchange": "NASD", "asset_type": "ETF"},
    {"symbol": "SPY", "exchange": "AMEX", "asset_type": "ETF"},
]


def load_universe_from_csv(path):
    with Path(path).open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return [_normalize_row(row) for row in rows]


def save_universe_to_csv(universe, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=UNIVERSE_FIELDS)
        writer.writeheader()
        for row in universe:
            writer.writerow(_normalize_row(row))


def load_nasdaq100_universe(path=NASDAQ100_CSV):
    return load_universe_from_csv(path)


def load_sp500_universe(path=SP500_CSV):
    path = Path(path)
    if not path.exists():
        return []
    return load_universe_from_csv(path)


def update_universe(index_name, rows, path=None):
    normalized_index = index_name.upper().replace("-", "")
    target = Path(path) if path else _default_path_for_index(normalized_index)
    universe = []
    for row in rows:
        item = dict(row)
        item.setdefault("exchange", "NASD")
        item.setdefault("index", normalized_index)
        item.setdefault("asset_type", "STOCK")
        universe.append(_normalize_row(item))
    save_universe_to_csv(universe, target)
    return universe


def _default_path_for_index(index_name):
    if index_name in {"NASDAQ100", "NDX"}:
        return NASDAQ100_CSV
    if index_name in {"SP500", "SPX"}:
        return SP500_CSV
    return DATA_DIR / f"{index_name.lower()}.csv"


def _normalize_row(row):
    symbol = (row.get("symbol") or row.get("Symbol") or "").strip().upper()
    name = (row.get("name") or row.get("Name") or "").strip()
    index_name = (row.get("index") or row.get("Index") or "").strip().upper()
    asset_type = (row.get("asset_type") or row.get("Asset Type") or "STOCK").strip().upper()
    exchange = (row.get("exchange") or row.get("Exchange") or "NASD").strip().upper()
    return {
        "symbol": symbol,
        "exchange": exchange,
        "name": name,
        "index": index_name,
        "asset_type": asset_type,
    }

