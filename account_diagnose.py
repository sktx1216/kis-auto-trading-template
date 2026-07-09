import json

import config
from kis_overseas import KisApiError, KisOverseasClient, _request_json, _to_float, summarize_balance_response
from token_manager import require_cached_token


def main():
    client = KisOverseasClient()
    require_cached_token(client)
    print("[ACCOUNT_DIAG] start")
    print(f"[ACCOUNT_DIAG] base_url_type={'paper' if client.is_paper_trading else 'live'}")
    print(f"[ACCOUNT_DIAG] account_product={client.acnt_prdt_cd}")

    checks = []
    checks.extend(_present_balance_checks(client))
    checks.extend(_balance_checks(client))
    checks.extend(_buyable_amount_checks(client))

    for check in checks:
        print("[ACCOUNT_DIAG] " + json.dumps(check, ensure_ascii=False, sort_keys=True))

    print("[ACCOUNT_DIAG] done")


def _present_balance_checks(client):
    checks = []
    url = f"{client.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
    tr_id = "VTRP6504R" if client.is_paper_trading else "CTRP6504R"
    for wcrc in ("01", "02"):
        for natn in ("000", "840"):
            for market in ("00", "01"):
                params = {
                    "CANO": client.cano,
                    "ACNT_PRDT_CD": client.acnt_prdt_cd,
                    "WCRC_FRCR_DVSN_CD": wcrc,
                    "NATN_CD": natn,
                    "TR_MKET_CD": market,
                    "INQR_DVSN_CD": "00",
                }
                checks.append(
                    _safe_get(
                        client,
                        "present_balance",
                        url,
                        tr_id,
                        params,
                        summary_fn=summarize_balance_response,
                    )
                )
    return checks


def _balance_checks(client):
    checks = []
    url = f"{client.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
    tr_id = "VTTT3012R" if client.is_paper_trading else "TTTS3012R"
    for exchange in ("NASD", "NYSE", "AMEX"):
        for currency in ("USD",):
            params = {
                "CANO": client.cano,
                "ACNT_PRDT_CD": client.acnt_prdt_cd,
                "OVRS_EXCG_CD": exchange,
                "TR_CRCY_CD": currency,
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            }
            checks.append(_safe_get(client, "balance", url, tr_id, params))
    return checks


def _buyable_amount_checks(client):
    checks = []
    url = f"{client.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
    tr_ids = ("VTTS3007R",) if client.is_paper_trading else ("TTTS3007R", "JTTT3007R")
    cases = (
        ("QQQ", "NASD", "740.00"),
        ("AAPL", "NASD", "200.00"),
        ("SPY", "AMEX", "550.00"),
    )
    for symbol, exchange, price in cases:
        for tr_id in tr_ids:
            params = {
                "CANO": client.cano,
                "ACNT_PRDT_CD": client.acnt_prdt_cd,
                "OVRS_EXCG_CD": exchange,
                "OVRS_ORD_UNPR": price,
                "ITEM_CD": symbol,
            }
            checks.append(_safe_get(client, "buyable_amount", url, tr_id, params))
    return checks


def _safe_get(client, name, url, tr_id, params, summary_fn=None):
    identity = {"name": name, "tr_id": tr_id, "params": _public_params(params)}
    try:
        data = _request_json(
            "get",
            url,
            headers=client._headers(tr_id),
            params=params,
            max_retries=0,
            timeout=10,
        )
        summary = summary_fn(data) if summary_fn else _summarize_response(data)
        return {**identity, "ok": True, "summary": summary}
    except KisApiError as error:
        return {**identity, "ok": False, "error": str(error)}


def _public_params(params):
    public = dict(params)
    if public.get("CANO"):
        public["CANO"] = f"****{str(public['CANO'])[-4:]}"
    return public


def _summarize_response(data):
    return {
        "rt_cd": data.get("rt_cd"),
        "msg_cd": data.get("msg_cd"),
        "msg1": data.get("msg1"),
        "sections": _sections(data),
        "amount_candidates": _amount_candidates(data),
    }


def _sections(data):
    sections = {}
    for key, value in data.items():
        if isinstance(value, list):
            rows = value
            row_count = len(value)
        elif isinstance(value, dict):
            rows = [value]
            row_count = 1
        else:
            continue
        sections[key] = {
            "type": type(value).__name__,
            "rows": row_count,
            "sample_keys": sorted(rows[0].keys())[:40] if rows else [],
        }
    return sections


def _amount_candidates(data):
    candidates = {}
    for path, value in _walk(data):
        numeric = _to_float(value)
        if numeric is None:
            continue
        key = ".".join(path)
        lower = key.lower()
        if any(token in lower for token in ("amt", "cash", "dncl", "frcr", "asst", "evlu", "ord_psbl")):
            candidates[key] = value
    return candidates


def _walk(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value[:3]):
            yield from _walk(item, (*path, str(index)))
    else:
        yield path, value


if __name__ == "__main__":
    main()
