import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

KST = timezone(timedelta(hours=9))
TOKEN_STATE_PATH = Path(config.TOKEN_STATE_PATH)


def load_token_state(path=TOKEN_STATE_PATH):
    path = Path(path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_token_state(token_state, path=TOKEN_STATE_PATH):
    path = Path(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(token_state, f, indent=2, sort_keys=True)
        f.write("\n")


def prepare_access_token(client, token_state=None):
    token_state = token_state or load_token_state()
    token = valid_cached_token(token_state, client)
    if token:
        client.set_access_token(token)
        print("[TOKEN] Reusing cached KIS token")
        return token_state

    print("[TOKEN] Issuing new KIS token...")
    token_data = client.issue_token()
    store_token(token_state, token_data, client)
    save_token_state(token_state)
    client.set_access_token(token_state["token"]["access_token"])
    print("[TOKEN] KIS token issued and cached")
    return token_state


def require_cached_token(client, token_state=None):
    token_state = token_state or load_token_state()
    token = valid_cached_token(token_state, client)
    if not token:
        raise RuntimeError("valid KIS token is missing. Run prepare_token.py first.")
    client.set_access_token(token)
    print("[TOKEN] Reusing cached KIS token")
    return token_state


def valid_cached_token(token_state, client=None):
    token = token_state.get("token", {}) if isinstance(token_state, dict) else {}
    access_token = token.get("access_token")
    expires_at = _parse_iso_datetime(token.get("expires_at"))
    if not access_token or not expires_at:
        return None
    if client and not _scope_matches(token.get("scope"), _token_scope(client)):
        return None

    buffer_time = datetime.now(timezone.utc) + timedelta(minutes=config.TOKEN_EXPIRY_BUFFER_MINUTES)
    if expires_at <= buffer_time:
        return None
    return access_token


def store_token(token_state, token_data, client=None):
    token_state["token"] = {
        "access_token": token_data.get("access_token"),
        "expires_at": _token_expires_at(token_data).isoformat(),
        "token_type": token_data.get("token_type", "Bearer"),
    }
    if client:
        token_state["token"]["scope"] = _token_scope(client)


def _token_scope(client):
    return {
        "base_url": getattr(client, "base_url", "").rstrip("/"),
        "app_key": getattr(client, "app_key", ""),
    }


def _scope_matches(cached_scope, current_scope):
    if not isinstance(cached_scope, dict):
        return False
    return all(cached_scope.get(key) == value for key, value in current_scope.items())


def _token_expires_at(token_data):
    explicit = token_data.get("access_token_token_expired")
    if explicit:
        try:
            parsed = datetime.strptime(explicit, "%Y-%m-%d %H:%M:%S")
            return parsed.replace(tzinfo=KST).astimezone(timezone.utc)
        except ValueError:
            pass
    expires_in = int(token_data.get("expires_in") or 86400)
    return datetime.now(timezone.utc) + timedelta(seconds=expires_in)


def _parse_iso_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
