"""Synchronous relay calls with an owned, whole-request async deadline."""

import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys

import anyio
import httpx


class _ResolverLoop(asyncio.SelectorEventLoop):
    """Request-local loop: native DNS has a killable owner, not an executor."""

    async def getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
        if isinstance(host, bytes):
            host = host.decode("ascii")  # AnyIO supplies IDNA-encoded hostnames.
        query = json.dumps([host, port, family, type, proto, flags]).encode()
        try:
            result = await anyio.run_process(
                [sys.executable, str(Path(__file__).with_name("_resolver.py"))],
                input=query,
            )
        except subprocess.CalledProcessError as error:
            raise OSError("relay resolver process failed") from error
        reply = json.loads(result.stdout)
        if "error" in reply:
            raise socket.gaierror(*reply["error"])
        return [
            (af, kind, protocol, canonical, tuple(address))
            for af, kind, protocol, canonical, address in reply["addresses"]
        ]


def request(method: str, url: str, *, timeout: float, **kwargs) -> httpx.Response:
    """Read the complete response before returning, or close it on timeout.

    Called from synchronous CLI code or a daemon offload worker. The worker
    owns this short-lived asyncio run until its request and client are closed;
    there is no background request thread to abandon after a deadline expires.
    Native DNS runs in a request-owned subprocess: cancellation kills and reaps
    it before returning, including while the resolver is starting up. HTTPX and
    AnyIO still own address fallback, Host headers, SNI and TLS verification.
    """
    async def perform() -> httpx.Response:
        try:
            with anyio.fail_after(timeout):
                async with httpx.AsyncClient() as client:
                    return await client.request(method, url, timeout=timeout, **kwargs)
        except TimeoutError as error:
            raise httpx.TimeoutException(
                f"relay request exceeded {timeout:g}s whole-call deadline"
            ) from error

    return anyio.run(
        perform, backend="asyncio", backend_options={"loop_factory": _ResolverLoop}
    )
