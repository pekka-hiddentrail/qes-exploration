"""Shared HTTP call to the SUT - identical implementation existed in every
prior experiment's run_live.py."""

import httpx


def call_sut_once(base_url: str, path: str, request_body: dict, timeout: float = 30.0) -> dict:
    with httpx.Client() as client:
        response = client.post(base_url + path, json=request_body, timeout=timeout)
        return {"status": response.status_code, "body": response.json()}
