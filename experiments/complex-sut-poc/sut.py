"""
A tiny, deliberately buggy rate-limited "submission" API for the complex-SUT PoC.
Unlike live-sut-poc's single-request ReDoS bug, this one is a genuine TOCTOU
(check-then-act) race condition in per-client quota enforcement - not simulated.
FastAPI runs sync `def` handlers in a thread pool, and time.sleep() releases the
GIL, so concurrent requests for the same client_id really do interleave between
the quota check and the quota write below.

Run with: uvicorn sut:app --port 8000

WARNING: /submit's rate limiting is NOT safe under concurrent requests for the
same client_id. Sequential requests (one at a time, waiting for each response)
always enforce the limit correctly - the race only manifests when multiple
requests for the same client_id are in flight at the same moment. A burst of
concurrent requests against a fresh client_id can get more of them "accepted"
than RATE_LIMIT allows.
"""

import time

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

RATE_LIMIT = 5
# Long enough that a real investigation session (many Claude API round-trips,
# each taking real wall-clock seconds) can't spuriously cross a window boundary
# mid-session and make a correctly-persisting counter look like it "reset" -
# confirmed by running an actual investigation: two sequential single requests
# 11 seconds apart (a realistic gap between follow-up rounds) showed the window
# expiring at the old 10s value, producing a misleading "counter didn't
# increment" artifact that had nothing to do with the real bug.
WINDOW_SECONDS = 600.0

# The deliberate vulnerable gap: real work happens here, between reading the
# current usage and writing the incremented value back, with nothing preventing
# two concurrent requests from both reading the same "used" value before either
# one writes.
PROCESSING_DELAY_S = 0.05

# client_id -> (window_start_monotonic, requests_used_this_window). Plain dict,
# no lock - that's the bug.
quota_state: dict[str, tuple[float, int]] = {}


class SubmitRequest(BaseModel):
    client_id: str
    payload: str
    priority: str = "normal"


@app.post("/submit")
def submit(req: SubmitRequest):
    now = time.monotonic()
    window_start, used = quota_state.get(req.client_id, (now, 0))

    if now - window_start >= WINDOW_SECONDS:
        window_start, used = now, 0

    if used >= RATE_LIMIT:
        return {"status": "rate_limited", "used": used, "limit": RATE_LIMIT}

    time.sleep(PROCESSING_DELAY_S)  # simulated processing - the vulnerable gap
    used += 1
    quota_state[req.client_id] = (window_start, used)

    return {"status": "accepted", "used": used, "limit": RATE_LIMIT}
