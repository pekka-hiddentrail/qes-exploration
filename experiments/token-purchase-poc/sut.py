"""
Third live-SUT PoC: a credit-purchase API with a realistic mock payment backend -
auth token + card ownership authorization, Luhn card validation, expiry checking,
CVV matching, tiered bulk pricing, and a per-card spending capacity that is
deliberately never revealed directly in any response (realistic: real payment
gateways don't tell the merchant/caller a card's exact available balance either,
for the same fraud-prevention reason).

Unlike live-sut-poc and complex-sut-poc, NO bug is deliberately planted here. This
was written with ordinary care, not audited for correctness before running the
harness against it - whether a real bug exists at all, and if so what it is, is
genuinely unknown going in.

Run with: uvicorn sut:app --port 8000

Balance updates are protected by a lock around the check-then-commit sequence,
deliberately avoiding the same unsynchronized-shared-state race complex-sut-poc's
rate limiter had.
"""

import itertools
import threading
from datetime import date

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

# --- Seed data: 3 real, valid accounts. All fields here are considered "on file" -
# what a real card issuer would actually have stored - not something the Driver is
# assumed to know unless it's disclosed to it separately by the harness.
USERS = {
    "tok_live_9f2c8a41": {"user_id": "user-aria-84", "card_number": "4111104332181963"},
    "tok_live_7d51e6b0": {"user_id": "user-boone-19", "card_number": "4111001338908383"},
    "tok_live_c3a9f204": {"user_id": "user-cleo-52", "card_number": "4111637940265421"},
}

CARDS = {
    "4111104332181963": {"expiry_month": 11, "expiry_year": 2027, "cvv": "482", "owner": "user-aria-84"},
    "4111001338908383": {"expiry_month": 3, "expiry_year": 2028, "cvv": "915", "owner": "user-boone-19"},
    "4111637940265421": {"expiry_month": 8, "expiry_year": 2028, "cvv": "067", "owner": "user-cleo-52"},
}

# Bulk pricing: (min_credits_for_this_tier, cents_per_credit). Checked in order,
# first match wins - buying 100+ credits prices the WHOLE order at the cheaper
# rate, not just the credits past the threshold.
PRICING_TIERS = [
    (1000, 1.5),
    (100, 1.8),
    (1, 2.0),
]


def price_cents_for_credits(credit_count: int) -> int:
    for threshold, cents_per_credit in PRICING_TIERS:
        if credit_count >= threshold:
            return round(credit_count * cents_per_credit)
    return round(credit_count * PRICING_TIERS[-1][1])


def derive_capacity_cents(card_number: str) -> int:
    """The card's total available spending capacity. Deliberately never returned
    in any response - a real payment gateway doesn't reveal a card's exact
    available balance to the merchant either. Purely a function of the card
    number, not separately stored anywhere."""
    digit_sum = sum(int(d) for d in card_number)
    last4 = int(card_number[-4:])
    raw = (digit_sum * 907 + last4 * 3) % 40000
    return 5000 + raw


def luhn_valid(card_number: str) -> bool:
    if not card_number.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(card_number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class PurchaseRequest(BaseModel):
    auth_token: str
    card_number: str
    expiry_month: int
    expiry_year: int
    cvv: str
    credit_count: int


_ledger_lock = threading.Lock()
_spent_cents_by_card: dict[str, int] = {}
_credit_balance_by_user: dict[str, int] = {}
_transaction_counter = itertools.count(1)


def _declined(reason: str, user_id: str | None) -> dict:
    balance = _credit_balance_by_user.get(user_id, 0) if user_id else None
    return {
        "status": "declined",
        "decline_reason": reason,
        "credits_purchased": 0,
        "total_charged": 0.0,
        "new_credit_balance": balance,
        "transaction_id": None,
    }


@app.post("/purchase")
def purchase(req: PurchaseRequest):
    user = USERS.get(req.auth_token)
    if user is None:
        return _declined("invalid_auth_token", None)
    user_id = user["user_id"]

    card = CARDS.get(req.card_number)
    if card is None or card["owner"] != user_id:
        return _declined("card_not_authorized", user_id)

    if not luhn_valid(req.card_number):
        return _declined("invalid_card_number", user_id)

    if req.expiry_month != card["expiry_month"] or req.expiry_year != card["expiry_year"]:
        return _declined("expiry_mismatch", user_id)

    today = date.today()
    if (today.year, today.month) > (req.expiry_year, req.expiry_month):
        return _declined("expired_card", user_id)

    if req.cvv != card["cvv"]:
        return _declined("invalid_cvv", user_id)

    if req.credit_count <= 0:
        return _declined("invalid_credit_count", user_id)

    price_cents = price_cents_for_credits(req.credit_count)

    with _ledger_lock:
        capacity_cents = derive_capacity_cents(req.card_number)
        already_spent = _spent_cents_by_card.get(req.card_number, 0)
        remaining_capacity = capacity_cents - already_spent

        if price_cents > remaining_capacity:
            return _declined("insufficient_funds", user_id)

        _spent_cents_by_card[req.card_number] = already_spent + price_cents
        new_balance = _credit_balance_by_user.get(user_id, 0) + req.credit_count
        _credit_balance_by_user[user_id] = new_balance
        txn_id = f"txn_{next(_transaction_counter):06d}"

    return {
        "status": "approved",
        "decline_reason": None,
        "credits_purchased": req.credit_count,
        "total_charged": price_cents / 100,
        "new_credit_balance": new_balance,
        "transaction_id": txn_id,
    }
