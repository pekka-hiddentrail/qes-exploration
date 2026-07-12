"""Shared HTTP call to the SUT - identical implementation existed in every
prior experiment's run_live.py."""

import httpx


def call_sut_once(
    base_url: str,
    path: str,
    request_body: dict,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
    method: str = "POST",
) -> dict:
    with httpx.Client(transport=transport) as client:
        response = client.request(method, base_url + path, json=request_body, timeout=timeout)
        try:
            body = response.json()
        except ValueError:
            # A non-JSON response (a crash page, an empty body, a proxy error)
            # shouldn't take down the whole run - surface it as data the Driver
            # can react to instead of an uncaught JSONDecodeError.
            body = {"error": "non-JSON response from SUT", "raw_text": response.text}
        return {"status": response.status_code, "body": body}
