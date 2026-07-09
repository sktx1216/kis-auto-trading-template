from datetime import datetime, timedelta, timezone
import unittest

import token_manager


class DummyClient:
    def __init__(self, base_url, app_key="app", cano="12345678", acnt_prdt_cd="01"):
        self.base_url = base_url
        self.app_key = app_key
        self.cano = cano
        self.acnt_prdt_cd = acnt_prdt_cd


class TokenManagerTests(unittest.TestCase):
    def test_cached_token_is_scoped_to_client_environment(self):
        live_client = DummyClient("https://openapi.koreainvestment.com:9443")
        paper_client = DummyClient("https://openapivts.koreainvestment.com:29443")
        token_state = {}
        token_manager.store_token(
            token_state,
            {
                "access_token": "live-token",
                "expires_in": 86400,
            },
            live_client,
        )

        self.assertEqual(token_manager.valid_cached_token(token_state, live_client), "live-token")
        self.assertIsNone(token_manager.valid_cached_token(token_state, paper_client))

    def test_cached_token_survives_account_secret_correction(self):
        first_client = DummyClient("https://openapi.koreainvestment.com:9443", cano="12345678")
        corrected_client = DummyClient("https://openapi.koreainvestment.com:9443", cano="1234567801")
        token_state = {}
        token_manager.store_token(
            token_state,
            {
                "access_token": "live-token",
                "expires_in": 86400,
            },
            first_client,
        )

        self.assertEqual(token_manager.valid_cached_token(token_state, corrected_client), "live-token")

    def test_unscoped_legacy_token_is_not_reused_for_client(self):
        client = DummyClient("https://openapi.koreainvestment.com:9443")
        token_state = {
            "token": {
                "access_token": "legacy-token",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "token_type": "Bearer",
            }
        }

        self.assertIsNone(token_manager.valid_cached_token(token_state, client))


if __name__ == "__main__":
    unittest.main()
