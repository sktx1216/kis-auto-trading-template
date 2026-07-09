import math
import time
from datetime import datetime

import requests

import config


EXCHANGE_TO_KIS_PRICE = {
    "NASD": "NAS",
    "NYSE": "NYS",
    "AMEX": "AMS",
}


class KisApiError(Exception):
    pass


class KisOverseasClient:
    def __init__(self):
        self.base_url = config.BASE_URL.rstrip("/")
        self.app_key = config.APP_KEY
        self.app_secret = config.APP_SECRET
        self.cano = config.CANO
        self.acnt_prdt_cd = config.ACNT_PRDT_CD
        self.access_token = None

    @property
    def is_paper_trading(self):
        return "openapivts" in self.base_url

    def issue_token(self):
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        data = _request_json("post", url, headers={"content-type": "application/json"}, json=body)
        self.access_token = data["access_token"]
        return data

    def set_access_token(self, access_token):
        self.access_token = access_token

    def get_hashkey(self, body):
        url = f"{self.base_url}/uapi/hashkey"
        headers = {
            "content-type": "application/json",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        return _request_json("post", url, headers=headers, json=body)["HASH"]

    def get_current_price(self, symbol, exchange="NASD"):
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        headers = self._headers("HHDFS00000300")
        params = {
            "AUTH": "",
            "EXCD": _price_exchange(exchange),
            "SYMB": symbol,
        }
        data = _request_json(
            "get",
            url,
            headers=headers,
            params=params,
            max_retries=config.KIS_QUOTE_MAX_RETRIES,
            retry_delay=config.KIS_QUOTE_RETRY_DELAY_SECONDS,
            timeout=config.KIS_QUOTE_TIMEOUT_SECONDS,
        )
        output = data.get("output", {})
        return _to_float(output.get("last"))

    def get_daily_prices(self, symbol, exchange="NASD", days=100):
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/dailyprice"
        headers = self._headers("HHDFS76240000")
        params = {
            "AUTH": "",
            "EXCD": _price_exchange(exchange),
            "SYMB": symbol,
            "GUBN": "0",
            "BYMD": datetime.now().strftime("%Y%m%d"),
            "MODP": "1",
        }
        data = _request_json(
            "get",
            url,
            headers=headers,
            params=params,
            max_retries=config.KIS_QUOTE_MAX_RETRIES,
            retry_delay=config.KIS_QUOTE_RETRY_DELAY_SECONDS,
            timeout=config.KIS_QUOTE_TIMEOUT_SECONDS,
        )
        rows = []
        for item in data.get("output2", [])[:days]:
            rows.append(
                {
                    "date": item.get("xymd"),
                    "open": _to_float(item.get("open")),
                    "high": _to_float(item.get("high")),
                    "low": _to_float(item.get("low")),
                    "close": _to_float(item.get("clos")),
                    "volume": _to_float(item.get("tvol")),
                }
            )
        return list(reversed([row for row in rows if row["close"] is not None]))

    def get_present_balance(self):
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
        headers = self._headers("VTRP6504R" if self.is_paper_trading else "CTRP6504R")
        params = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "WCRC_FRCR_DVSN_CD": "02",
            "NATN_CD": "840",
            "TR_MKET_CD": "00",
            "INQR_DVSN_CD": "00",
        }
        try:
            return _request_json("get", url, headers=headers, params=params)
        except KisApiError as error:
            if self.is_paper_trading and "EGW02006" in str(error):
                return self.get_balance(exchange="NASD", currency="USD")
            raise

    def get_balance(self, exchange="NASD", currency="USD"):
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        headers = self._headers("VTTT3012R" if self.is_paper_trading else "TTTS3012R")
        params = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "OVRS_EXCG_CD": exchange,
            "TR_CRCY_CD": currency,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        return _request_json("get", url, headers=headers, params=params)

    def get_buyable_amount(self, symbol, price, exchange="NASD"):
        attempts = self.get_buyable_amount_attempts(symbol, price, exchange)
        for attempt in attempts:
            if attempt["ok"]:
                output = attempt["data"].get("output", {})
                amount = _first_float(
                    output,
                    "ovrs_ord_psbl_amt",
                    "ord_psbl_frcr_amt",
                    "max_ord_psbl_amt",
                    "frcr_ord_psbl_amt",
                )
                if amount is not None:
                    return amount
        errors = "; ".join(f"{attempt['tr_id']}: {attempt.get('error')}" for attempt in attempts)
        raise KisApiError(f"buyable amount lookup failed: {errors}")

    def get_buyable_amount_attempts(self, symbol, price, exchange="NASD"):
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
        params = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "OVRS_EXCG_CD": exchange,
            "OVRS_ORD_UNPR": f"{float(price):.2f}",
            "ITEM_CD": symbol,
        }
        attempts = []
        for tr_id in self._buyable_amount_tr_ids():
            try:
                data = _request_json(
                    "get",
                    url,
                    headers=self._headers(tr_id),
                    params=params,
                    max_retries=0,
                    timeout=config.KIS_API_TIMEOUT_SECONDS,
                )
                attempts.append({"tr_id": tr_id, "ok": True, "data": data})
            except KisApiError as error:
                attempts.append({"tr_id": tr_id, "ok": False, "error": str(error)})
        return attempts

    def buy_limit_order(self, symbol, qty, price, exchange="NASD"):
        return self._order("buy", symbol, qty, price, exchange)

    def sell_limit_order(self, symbol, qty, price, exchange="NASD"):
        return self._order("sell", symbol, qty, price, exchange)

    def get_open_orders(self, exchange="NASD"):
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
        params = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "OVRS_EXCG_CD": exchange,
            "SORT_SQN": "DS",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        return _request_json(
            "get",
            url,
            headers=self._headers(self._open_orders_tr_id()),
            params=params,
        )

    def cancel_order(self, symbol, qty, price, exchange, order_response):
        order_ids = extract_order_ids(order_response)
        order_no = order_ids.get("order_no")
        if not order_no:
            raise KisApiError("order number is missing; cannot cancel unconfirmed order")

        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order-rvsecncl"
        body = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORGNO": order_ids.get("org_no", ""),
            "ODNO": order_no,
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(int(qty)),
            "OVRS_ORD_UNPR": f"{float(price):.2f}",
            "ORD_SVR_DVSN_CD": "0",
        }
        headers = self._headers(self._cancel_order_tr_id())
        headers["hashkey"] = self.get_hashkey(body)
        return _request_json("post", url, headers=headers, json=body)

    def _order(self, side, symbol, qty, price, exchange):
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        body = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORD_QTY": str(int(qty)),
            "OVRS_ORD_UNPR": f"{float(price):.2f}",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",
        }
        headers = self._headers(self._order_tr_id(side))
        headers["hashkey"] = self.get_hashkey(body)
        return _request_json("post", url, headers=headers, json=body)

    def _headers(self, tr_id):
        if not self.access_token:
            raise KisApiError("access token is missing. call issue_token() first.")
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _order_tr_id(self, side):
        if self.is_paper_trading:
            return "VTTT1002U" if side == "buy" else "VTTT1001U"
        return "TTTT1002U" if side == "buy" else "TTTT1006U"

    def _cancel_order_tr_id(self):
        return "VTTT1004U" if self.is_paper_trading else "TTTT1004U"

    def _open_orders_tr_id(self):
        return "VTTS3018R" if self.is_paper_trading else "TTTS3018R"

    def _buyable_amount_tr_ids(self):
        if self.is_paper_trading:
            return ("VTTS3007R",)
        return ("TTTS3007R", "JTTT3007R")


def extract_positions(balance):
    positions = []
    for item in balance.get("output1", []) or []:
        symbol = item.get("ovrs_pdno") or item.get("pdno") or item.get("trad_pdno")
        qty = _first_float(
            item,
            "ovrs_cblc_qty",
            "hldg_qty",
            "cblc_qty",
            "cblc_qty13",
            "ord_psbl_qty1",
        )
        if not symbol or not qty:
            continue
        positions.append(
            {
                "symbol": symbol.upper(),
                "name": item.get("ovrs_item_name") or item.get("prdt_name") or "",
                "qty": qty,
                "avg_price": _first_float(item, "pchs_avg_pric", "avg_unpr3", "frcr_pchs_amt1"),
                "purchase_amount": _to_float(
                    item.get("frcr_pchs_amt")
                    or item.get("frcr_pchs_amt1")
                    or item.get("ovrs_stck_pchs_amt")
                    or item.get("pchs_amt")
                ),
                "market_value": _to_float(
                    item.get("frcr_evlu_amt2")
                    or item.get("ovrs_stck_evlu_amt")
                    or item.get("evlu_amt")
                    or item.get("evlu_amt_smtl")
                ),
                "profit_loss": _to_float(
                    item.get("frcr_evlu_pfls_amt")
                    or item.get("evlu_pfls_amt2")
                    or item.get("ovrs_evlu_pfls_amt")
                    or item.get("evlu_pfls_amt")
                ),
                "profit_rate": _to_float(
                    item.get("evlu_pfls_rt")
                    or item.get("evlu_pfls_rt1")
                    or item.get("evlu_erng_rt")
                ),
                "exchange_rate": _first_float(item, "bass_exrt", "frst_bltn_exrt", "exrt"),
                "already_half_sold": False,
            }
        )
    return positions


def extract_order_ids(order_response):
    output = order_response.get("output") if isinstance(order_response, dict) else {}
    if not isinstance(output, dict):
        output = {}
    candidates = {**order_response, **output} if isinstance(order_response, dict) else output
    return {
        "org_no": _first_str(
            candidates,
            "KRX_FWDG_ORD_ORGNO",
            "krx_fwdg_ord_orgno",
            "ORGNO",
            "orgno",
            "ord_orgno",
        ),
        "order_no": _first_str(candidates, "ODNO", "odno", "ORD_NO", "ord_no"),
    }


def extract_open_orders(data, default_exchange="NASD"):
    rows = []
    if isinstance(data, dict):
        for key in ("output", "output1", "output2"):
            value = data.get(key)
            if isinstance(value, list):
                rows.extend(value)
            elif isinstance(value, dict):
                rows.append(value)
    elif isinstance(data, list):
        rows = data

    orders = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = _first_str(row, "PDNO", "pdno", "OVRS_PDNO", "ovrs_pdno", "ITEM_CD", "item_cd")
        order_ids = extract_order_ids(row)
        qty = _first_float(
            row,
            "NCCS_QTY",
            "nccs_qty",
            "ORD_QTY",
            "ord_qty",
            "FT_ORD_QTY",
            "ft_ord_qty",
            "OVRS_ORD_QTY",
            "ovrs_ord_qty",
        )
        price = _first_float(
            row,
            "OVRS_ORD_UNPR",
            "ovrs_ord_unpr",
            "ORD_UNPR",
            "ord_unpr",
            "OVRS_ORD_UNPR3",
            "ovrs_ord_unpr3",
        )
        if not symbol or not order_ids.get("order_no") or not qty:
            continue
        orders.append(
            {
                "symbol": symbol.upper(),
                "exchange": _first_str(row, "OVRS_EXCG_CD", "ovrs_excg_cd") or default_exchange,
                "qty": qty,
                "price": price or 0,
                "org_no": order_ids.get("org_no", ""),
                "order_no": order_ids["order_no"],
                "raw": row,
            }
        )
    return orders


def extract_usd_cash(balance):
    preferred_keys = (
        "frcr_drwg_psbl_amt_1",
        "ord_psbl_frcr_amt",
        "ovrs_ord_psbl_amt",
        "dnca_tot_amt",
        "cash",
        "frcr_dncl_amt_2",
    )
    output2 = balance.get("output2", [])
    if isinstance(output2, dict):
        output2 = [output2]
    for item in output2 or []:
        if item.get("crcy_cd") and item.get("crcy_cd") != "USD":
            continue
        for key in preferred_keys:
            value = _to_float(item.get(key))
            if value is not None:
                return value
    return 0.0


def summarize_balance_response(balance):
    summary = {
        "rt_cd": balance.get("rt_cd"),
        "msg_cd": balance.get("msg_cd"),
        "msg1": balance.get("msg1"),
        "sections": {},
        "cash_candidates": [],
        "position_candidates": [],
    }

    for section in ("output1", "output2", "output3"):
        value = balance.get(section)
        if isinstance(value, list):
            rows = value
            row_count = len(value)
        elif isinstance(value, dict):
            rows = [value]
            row_count = 1
        else:
            rows = []
            row_count = 0
        summary["sections"][section] = {
            "type": type(value).__name__,
            "rows": row_count,
            "sample_keys": sorted(rows[0].keys())[:30] if rows else [],
        }

    output1 = balance.get("output1", [])
    if isinstance(output1, dict):
        output1 = [output1]
    for item in output1 or []:
        symbol = item.get("ovrs_pdno") or item.get("pdno") or item.get("trad_pdno")
        qty = _to_float(item.get("ovrs_cblc_qty") or item.get("hldg_qty") or item.get("cblc_qty"))
        if symbol or qty:
            summary["position_candidates"].append({"symbol": symbol, "qty": qty})

    output2 = balance.get("output2", [])
    if isinstance(output2, dict):
        output2 = [output2]
    cash_keys = (
        "crcy_cd",
        "frcr_dncl_amt_2",
        "frcr_drwg_psbl_amt_1",
        "dnca_tot_amt",
        "cash",
        "ord_psbl_cash",
        "frcr_buy_amt_smtl",
        "tot_asst_amt",
        "evlu_amt_smtl",
    )
    for item in output2 or []:
        candidate = {key: item.get(key) for key in cash_keys if key in item}
        if candidate:
            summary["cash_candidates"].append(candidate)

    output3 = balance.get("output3", {})
    if isinstance(output3, list):
        output3 = output3[0] if output3 else {}
    if isinstance(output3, dict):
        total_keys = (
            "dncl_amt",
            "tot_dncl_amt",
            "wdrw_psbl_tot_amt",
            "frcr_use_psbl_amt",
            "tot_frcr_cblc_smtl",
            "frcr_evlu_tota",
            "tot_asst_amt",
            "evlu_amt_smtl",
            "pchs_amt_smtl",
        )
        totals = {key: output3.get(key) for key in total_keys if key in output3}
        if totals:
            summary["account_total_candidates"] = totals

    return summary


def calculate_order_qty(order_budget_usd, current_price):
    if current_price <= 0 or order_budget_usd < config.MIN_ORDER_AMOUNT_USD:
        return 0
    return math.floor(order_budget_usd / current_price)


def _checked_json(res):
    try:
        data = res.json()
    except ValueError as error:
        raise KisApiError(f"KIS API error ({res.status_code}): {res.text}") from error
    if res.ok and data.get("rt_cd") in (None, "0"):
        return data
    code = data.get("msg_cd")
    message = data.get("msg1") or res.text
    if code:
        message = f"{code}: {message}"
    raise KisApiError(f"KIS API error ({res.status_code}): {message}")


def _request_json(method, url, max_retries=None, retry_delay=None, **kwargs):
    last_error = None
    retries = config.KIS_API_MAX_RETRIES if max_retries is None else max_retries
    delay = config.KIS_API_RETRY_DELAY_SECONDS if retry_delay is None else retry_delay
    for attempt in range(retries + 1):
        try:
            kwargs.setdefault("timeout", config.KIS_API_TIMEOUT_SECONDS)
            response = requests.request(method, url, **kwargs)
            return _checked_json(response)
        except (requests.RequestException, KisApiError) as error:
            last_error = error
            if attempt >= retries:
                break
            print(
                f"KIS API call failed. Retrying in {delay} seconds... "
                f"(attempt {attempt + 1}/{retries + 1}, {error})"
            )
            time.sleep(delay)
    if isinstance(last_error, KisApiError):
        raise last_error
    raise KisApiError(f"KIS API call failed after retries: {last_error}") from last_error


def _price_exchange(exchange):
    return EXCHANGE_TO_KIS_PRICE.get(exchange, exchange)


def _to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _first_float(data, *keys):
    for key in keys:
        value = _to_float(data.get(key))
        if value is not None:
            return value
    return None


def _first_str(data, *keys):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return ""
