from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx


class RazorpayMCPError(RuntimeError): pass


class RazorpayMCPClient:
    endpoint = "https://mcp.razorpay.com/mcp"

    def __init__(self, key_id: str | None = None, key_secret: str | None = None) -> None:
        key_id, key_secret = key_id or os.getenv("RAZORPAY_KEY_ID"), key_secret or os.getenv("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise ValueError("Razorpay credentials are missing")
        token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json"}
        self.session_id: str | None = None
        self._request_id = 0

    @staticmethod
    def _decode(response: httpx.Response) -> dict | None:
        if "text/event-stream" in response.headers.get("content-type", ""):
            lines = [x[5:].strip() for x in response.text.splitlines() if x.startswith("data:")]
            return json.loads(lines[-1]) if lines else None
        return response.json() if response.content else None

    async def _rpc(self, method: str, params: dict | None = None, notification: bool = False) -> dict | None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            self._request_id += 1; payload["id"] = self._request_id
        if params is not None: payload["params"] = params
        headers = dict(self.headers)
        if self.session_id: headers["Mcp-Session-Id"] = self.session_id
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(self.endpoint, headers=headers, json=payload)
        response.raise_for_status()
        self.session_id = self.session_id or response.headers.get("mcp-session-id")
        body = self._decode(response)
        if body and body.get("error"): raise RazorpayMCPError(str(body["error"]))
        return body

    async def connect(self) -> None:
        await self._rpc("initialize", {"protocolVersion":"2025-03-26","capabilities":{},
          "clientInfo":{"name":"cartisan-backend","version":"0.1.0"}})
        await self._rpc("notifications/initialized", notification=True)

    async def call_tool(self, name: str, arguments: dict) -> dict:
        if not self.session_id: await self.connect()
        response = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        result = response["result"]
        if result.get("isError"): raise RazorpayMCPError(result["content"][0].get("text", "tool failed"))
        text = result["content"][0].get("text", "{}")
        return json.loads(text)

    async def create_payment_link(self, *, amount: int, reference_id: str, description: str) -> dict:
        """The link for this internal order, creating it only if it does not exist.

        `reference_id` is the internal order id, and Razorpay enforces that it is
        unique — but by *rejecting* the second create, not by returning the first
        link. So a redelivered outbox message (a timeout, a crash between the
        provider call and our write) would otherwise fail forever and dead-letter,
        leaving an order with a live link at the provider and none recorded here.
        Reading the existing link back is what makes the handoff genuinely
        idempotent, which is what ADR 0011 asks the interface to guarantee.
        """
        reference_id = reference_id[:40]
        try:
            return await self.call_tool("create_payment_link", {"amount": amount, "currency":"INR",
              "reference_id": reference_id, "description": description,
              "notes":{"source":"cartisan"}})
        except RazorpayMCPError as exc:
            if "already exists" not in str(exc):
                raise
            existing = await self.find_payment_link(reference_id)
            if existing is None:
                raise
            return existing

    async def find_payment_link(self, reference_id: str) -> dict | None:
        """The link already created for this reference, or None."""
        response = await self.call_tool("fetch_all_payment_links",
                                        {"reference_id": reference_id})
        links = response.get("payment_links") or response.get("items") or []
        for link in links:
            if link.get("reference_id") == reference_id:
                return link
        return links[0] if len(links) == 1 else None
