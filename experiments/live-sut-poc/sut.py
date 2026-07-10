"""
A tiny, deliberately buggy "system under test" for the live-SUT PoC. Mirrors the
pattern-detection-poc scenario for real: latency scales with input length (a
legitimate, boring cost model), except for inputs matching a naive input-sanitization
regex that is vulnerable to catastrophic backtracking - a genuine Python `re` bug
(Python's regex engine backtracks; this is not simulated).

Run with: uvicorn sut:app --port 8000

WARNING: /analyze contains a real exponential-time vulnerability. A run of N 'a'
characters followed by a non-'a' character takes roughly 4x longer per +2 to N
(measured on this machine: n=24 -> 1.6s, n=25 -> 3.6s, n=26 -> 6.6s, n=28 -> 26s).
Don't send longer repeated-character runs than the calibrated example used by
run_live.py, or a single request can hang for a very long time.
"""

import re
import time

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

# Naive "banned repeated pattern" check - a real catastrophic-backtracking pattern.
PATHOLOGICAL_PATTERN = re.compile(r"^(a+)+$")

BASE_LATENCY_S = 0.008
PER_CHAR_LATENCY_S = 0.0011


class AnalyzeRequest(BaseModel):
    text: str


@app.post("/analyze")
def analyze(payload: AnalyzeRequest):
    start = time.perf_counter()

    PATHOLOGICAL_PATTERN.match(payload.text)  # fast for normal text, catastrophic for pathological input

    time.sleep(BASE_LATENCY_S + PER_CHAR_LATENCY_S * len(payload.text))

    latency_ms = (time.perf_counter() - start) * 1000
    return {"char_count": len(payload.text), "latency_ms": round(latency_ms, 1)}
