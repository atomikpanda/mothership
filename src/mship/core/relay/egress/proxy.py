from __future__ import annotations

import httpx
from fastapi import FastAPI, Request, Response

from mship.core.relay.grants import Grant, GrantStore, Scope
from mship.core.relay.run_token import verify_run_token
from mship.core.relay.egress.credential import AttachmentHostError
from mship.core.relay.egress.enforce import EnforcementError
from mship.core.relay.egress.provider import ProviderError
from mship.core.relay.egress.request import parse_egress_request, UnmappablePathError
from mship.core.relay.egress.routes import RouteTable, UnknownHostError

# Headers we must never pass upstream: the worker's placeholder token, the
# worker-facing Host, and length/encoding httpx recomputes for the new body.
_STRIP = {"mship-run-token", "host", "content-length", "authorization",
          "transfer-encoding", "connection"}


def build_egress_app(
    *, grant_store: GrantStore, run_token_dir, routes: RouteTable | None,
    client: httpx.Client | None = None, upstream_scheme: str = "https",
) -> FastAPI:
    """The egress-proxy role. `routes=None` (App creds absent) => fail closed:
    every request 503, never forward — mirrors serve's refuse-on-unreadable-key."""
    app = FastAPI(title="mship egress proxy")
    http = client or httpx.Client(timeout=60)

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def egress(full_path: str, request: Request) -> Response:
        if routes is None:
            return Response("egress: no credential provider configured (App creds "
                            "absent) — refusing to forward", status_code=503)

        presented = request.headers.get("Mship-Run-Token")
        if not presented:
            return Response("missing run token", status_code=401)
        rt = verify_run_token(run_token_dir, presented)
        if rt is None:
            return Response("invalid or expired run token", status_code=401)

        body = await request.body()
        try:
            egress_req = parse_egress_request(
                method=request.method, path=request.url.path,
                query=request.url.query, headers=dict(request.headers), body=body,
            )
        except UnmappablePathError:
            return Response("unmapped path", status_code=404)

        # Ceiling: the enrollment's typed grant for this provider.
        ceiling = next((g for g in grant_store.get_grants(rt.enrollment_id)
                        if g.provider == "github-app"), None)
        if ceiling is None:
            return Response("no grant for enrollment", status_code=403)
        # Per-run scope must be within the ceiling.
        if not ceiling.scope.covers(rt.scope):
            return Response("run scope exceeds enrollment grant", status_code=403)
        effective = Grant("github-app", rt.scope)

        try:
            route = routes.resolve(egress_req.upstream_host)
            route.enforcer.check(egress_req, effective)
            cred = route.provider.resolve(
                identity=rt.enrollment_id, grant=effective, request=egress_req,
            )
        except UnknownHostError:
            return Response("no route for host", status_code=404)
        except EnforcementError as e:
            return Response(f"rejected: {e}", status_code=403)
        except ProviderError as e:
            return Response(f"provider error: {e}", status_code=502)

        upstream_headers = {k: v for k, v in egress_req.headers.items()
                            if k.lower() not in _STRIP}
        try:
            cred.attach.apply(upstream_headers, host=egress_req.upstream_host, value=cred.value)
        except AttachmentHostError as e:
            return Response(f"attach refused: {e}", status_code=500)

        url = f"{upstream_scheme}://{egress_req.upstream_host}{egress_req.upstream_path}"
        if egress_req.query:
            url = f"{url}?{egress_req.query}"
        upstream = http.request(request.method, url, headers=upstream_headers, content=body)
        # Pass the upstream response back verbatim (drop hop-by-hop headers).
        resp_headers = {k: v for k, v in upstream.headers.items()
                        if k.lower() not in ("content-length", "transfer-encoding", "connection")}
        return Response(content=upstream.content, status_code=upstream.status_code,
                        headers=resp_headers)

    return app
