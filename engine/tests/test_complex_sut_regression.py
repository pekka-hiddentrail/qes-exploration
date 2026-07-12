"""Regression checks against the ported complex_sut mock SUT - no Anthropic
calls. Unlike token_purchase's regression tests, these run against a REAL
uvicorn subprocess rather than FastAPI's in-process TestClient: the bug here
is a genuine TOCTOU race that depends on Starlette actually dispatching sync
handlers across its thread pool under real concurrent load, and time.sleep()
genuinely releasing the GIL between the quota check and the quota write -
something an in-process test client isn't guaranteed to reproduce faithfully.

The sequential check is fully deterministic - a race is structurally
impossible when each request waits for the previous response. The
concurrent check is inherently probabilistic (it's a race, not a clean
branch), but the margin here (20 concurrent requests against a limit of 5,
each holding the vulnerable window open for 50ms) makes a false negative
exceedingly unlikely - this is the same mechanism the original experiment
used to repeatedly, reliably reproduce this exact bug across many live runs.
"""

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

TEST_PORT = 8791
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"
RATE_LIMIT = 5  # must match engine/adapters/complex_sut/sut.py's RATE_LIMIT


@pytest.fixture(scope="module")
def running_server():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "engine.adapters.complex_sut.sut:app", "--port", str(TEST_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{BASE_URL}/docs", timeout=1.0)
                break
            except httpx.TransportError:
                time.sleep(0.2)
        else:
            proc.terminate()
            raise RuntimeError("complex_sut test server did not start in time")
        yield BASE_URL
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _submit(base_url, client_id, payload="x"):
    with httpx.Client() as client:
        response = client.post(f"{base_url}/submit", json={"client_id": client_id, "payload": payload}, timeout=30.0)
        return response.json()


def test_sequential_requests_never_exceed_the_rate_limit(running_server):
    client_id = "regression-sequential"
    responses = [_submit(running_server, client_id) for _ in range(RATE_LIMIT + 3)]
    accepted_count = sum(1 for r in responses if r["status"] == "accepted")
    assert accepted_count == RATE_LIMIT


def test_concurrent_burst_overcounts_past_the_rate_limit(running_server):
    client_id = "regression-concurrent"
    burst_size = 20
    with ThreadPoolExecutor(max_workers=burst_size) as pool:
        responses = list(pool.map(lambda _: _submit(running_server, client_id), range(burst_size)))
    accepted_count = sum(1 for r in responses if r["status"] == "accepted")
    assert accepted_count > RATE_LIMIT
