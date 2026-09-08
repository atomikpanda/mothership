"""Synchronous relay calls with an owned, whole-request async deadline."""

import anyio
import httpx


def request(method: str, url: str, *, timeout: float, **kwargs) -> httpx.Response:
    """Read the complete response before returning, or close it on timeout.

    Called from synchronous CLI code or a daemon offload worker. The worker
    owns this short-lived asyncio run until its request and client are closed;
    there is no background request thread to abandon after a deadline expires.
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

    return anyio.run(perform, backend="asyncio")
