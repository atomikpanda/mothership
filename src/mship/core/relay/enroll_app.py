from __future__ import annotations
from typing import Annotated, Union

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from mship.core.relay import host_contract
from mship.core.relay.enroll import RequestStore, PendingCapReached, validate_pubkey
from mship.core.relay.fleet_token import FleetTokenStore
from mship.core.relay.host_directory import (
    ChallengeRefused,
    DuplicateIdentity,
    HostDirectory,
    InvalidHostId,
    InvalidPublicUrl,
    SignatureRefused,
    VerificationBusy,
)
from mship.core.relay.tls_ask import tls_ask_allowed


class _EnrollBody(BaseModel):
    # Bound the body: this endpoint is public, so cap the payload before we
    # read+hash+store it. 1024 covers any real ssh key; 253 is the DNS hostname
    # max. Over-length input is rejected by pydantic with a 422.
    pubkey: str = Field(max_length=1024)
    hostname: str = Field(default="", max_length=253)


# `/hosts/register` is public too (the signature, not the transport, is the
# gate), and its payload is verified as the bytes the client sent — so it is
# accepted as a bounded dict rather than a field-by-field model: a model with
# defaults would re-serialize keys the client never sent and every signature
# would fail. `capabilities`/`runner` are the one nested level the contract
# defines, so a value is a scalar or a flat map of scalars.
_MAX_STR = 512
_MAX_KEY = 128
_MAX_PAYLOAD_FIELDS = 64
_MAX_NESTED_FIELDS = 32

_Key = Annotated[str, Field(max_length=_MAX_KEY)]
_Scalar = Union[Annotated[str, Field(max_length=_MAX_STR)], bool, int, float, None]
_PayloadValue = Union[
    _Scalar, Annotated[dict[_Key, _Scalar], Field(max_length=_MAX_NESTED_FIELDS)]
]


class _ChallengeBody(BaseModel):
    key_fingerprint: str = Field(max_length=_MAX_STR)


class _RegisterBody(BaseModel):
    nonce: str = Field(max_length=128)
    signature: str = Field(max_length=8192)  # an armored SSHSIG, ~600B for ed25519
    payload: dict[_Key, _PayloadValue] = Field(max_length=_MAX_PAYLOAD_FIELDS)


class _RevokeBody(BaseModel):
    key_fingerprint: str = Field(max_length=_MAX_STR)
    nonce: str = Field(max_length=128)
    signature: str = Field(max_length=8192)


# The directory is a PUBLISHED surface: a field added to a stored entry must not
# auto-appear on every paired phone. `refresh` is here deliberately — fetching it
# is why the phone reads this route at all — and appears on no other response.
_DIRECTORY_FIELDS = (
    "host_id",
    "state",
    "label",
    "instance_id",
    "key_fingerprint",
    "machine_fingerprint",
    "subdomain",
    "public_url",
    "mship_version",
    "capabilities",
    "runner",
    "refresh",
    "first_seen",
    "last_seen",
    "previous_instance_id",
    "request_id",
    "created_at",
)


def build_enroll_app(
    store: RequestStore,
    *,
    relay_domain: str,
    host_directory: HostDirectory,
    fleet_tokens: FleetTokenStore,
) -> FastAPI:
    app = FastAPI(title="mship relay enroll")

    @app.post(host_contract.ENROLL_PATH)
    def enroll(body: _EnrollBody):
        if not validate_pubkey(body.pubkey):
            raise HTTPException(status_code=400, detail="invalid ssh public key")
        try:
            rid = store.create(body.pubkey, body.hostname)
        except PendingCapReached:
            raise HTTPException(
                status_code=429, detail="too many pending requests; try later"
            )
        except ValueError:
            # Store self-validates (belt-and-suspenders); surface its rejection
            # as a clean 400 rather than letting it bubble up as a 500.
            raise HTTPException(status_code=400, detail="invalid ssh public key")
        return {"id": rid, "status": store.get(rid), "expires_in": store.ttl_seconds}

    @app.get("/status/{rid}")
    def status(rid: str):
        return {"id": rid, "status": store.get(rid)}

    @app.get("/tls-check")
    def tls_check(domain: str = Query(..., max_length=253)):
        # Caddy on-demand TLS ask endpoint: 2xx => issue a cert for `domain`.
        if not tls_ask_allowed(domain, relay_domain):
            raise HTTPException(status_code=403, detail="host not allowed")
        return Response(status_code=200)

    @app.post(host_contract.CHALLENGE_PATH)
    def hosts_challenge(body: _ChallengeBody):
        try:
            challenge = host_directory.issue_challenge(body.key_fingerprint)
        except SignatureRefused as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        return {"nonce": challenge["nonce"], "expires_at": challenge["expires_at"]}

    @app.post(host_contract.REVOKE_PATH)
    def hosts_revoke(body: _RevokeBody):
        try:
            host_directory.revoke_key(
                body.key_fingerprint,
                nonce=body.nonce,
                signature=body.signature,
            )
        except (ChallengeRefused, SignatureRefused) as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except VerificationBusy:
            raise HTTPException(
                status_code=429,
                detail="signature verification busy; try later",
                headers={"Retry-After": "1"},
            )
        return {"status": "revoked"}

    @app.post(host_contract.REGISTER_PATH)
    def hosts_register(body: _RegisterBody):
        try:
            entry = host_directory.register(
                body.payload, nonce=body.nonce, signature=body.signature
            )
        except InvalidHostId:
            raise HTTPException(status_code=400, detail="malformed host_id")
        except InvalidPublicUrl as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ValueError:
            raise HTTPException(
                status_code=400, detail="malformed registration payload"
            )
        except (ChallengeRefused, SignatureRefused) as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except VerificationBusy:
            raise HTTPException(
                status_code=429,
                detail="signature verification busy; try later",
                headers={"Retry-After": "1"},
            )
        except DuplicateIdentity as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        # Echoes identity only: the entry carries the host's refresh credential,
        # which is published on `GET /hosts` and nowhere else.
        return {"status": "registered", "host_id": entry["host_id"]}

    @app.get(host_contract.LIST_PATH)
    def hosts_list(request: Request):
        presented = request.headers.get(host_contract.FLEET_TOKEN_HEADER, "")
        if fleet_tokens.verify(presented) is None:
            raise HTTPException(
                status_code=401, detail="invalid or revoked fleet token"
            )
        entries = host_directory.list_hosts(store.list_pending())
        return {
            "hosts": [
                {f: entry[f] for f in _DIRECTORY_FIELDS if f in entry}
                for entry in entries
            ]
        }

    return app
