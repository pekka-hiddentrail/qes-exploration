"""Deterministic regression checks against the ported token_purchase mock SUT -
no Anthropic calls, no live server. These guard against the port silently
altering validation order; they are not a claim that the underlying gaps are
fixed (they aren't - see engine/tools.py's note on inference_validity_check
and the token_purchase README for the two known, accepted findings)."""

from fastapi.testclient import TestClient

from engine.adapters.token_purchase.adapter import KNOWN_ACCOUNTS
from engine.adapters.token_purchase.sut import app

client = TestClient(app)


def test_invalid_card_number_is_unreachable_via_malformed_input():
    """Any card number not on file returns card_not_authorized, regardless of
    whether it's Luhn-valid, correct-length, or obviously malformed - the
    card-lookup check runs before the Luhn check, so invalid_card_number can
    never fire for external input."""
    account = KNOWN_ACCOUNTS[0]
    response = client.post("/purchase", json={
        "auth_token": account["auth_token"],
        "card_number": "1234567890",
        "expiry_month": 1,
        "expiry_year": 2099,
        "cvv": "000",
        "credit_count": 1,
    })
    body = response.json()
    assert body["status"] == "declined"
    assert body["decline_reason"] == "card_not_authorized"


def test_expired_card_is_unreachable_via_mismatched_past_expiry():
    """A submitted expiry that doesn't match what's on file returns
    expiry_mismatch even when the submitted date is chronologically past -
    the expiry-equality check runs before the expiry-age check, so
    expired_card can never fire (no seeded account has an on-file expiry in
    the past to trigger it directly either)."""
    account = KNOWN_ACCOUNTS[0]
    response = client.post("/purchase", json={
        "auth_token": account["auth_token"],
        "card_number": account["card_number"],
        "expiry_month": account["expiry_month"],
        "expiry_year": 2020,  # genuinely past, but mismatched vs. what's on file
        "cvv": account["cvv"],
        "credit_count": 1,
    })
    body = response.json()
    assert body["decline_reason"] == "expiry_mismatch"
    assert body["decline_reason"] != "expired_card"
