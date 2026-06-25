from __future__ import annotations
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from mship.core.relay.enroll import RequestStore, PendingCapReached, validate_pubkey


class _EnrollBody(BaseModel):
    # Bound the body: this endpoint is public, so cap the payload before we
    # read+hash+store it. 1024 covers any real ssh key; 253 is the DNS hostname
    # max. Over-length input is rejected by pydantic with a 422.
    pubkey: str = Field(max_length=1024)
    hostname: str = Field(default="", max_length=253)


def build_enroll_app(store: RequestStore) -> FastAPI:
    app = FastAPI(title="mship relay enroll")

    @app.post("/enroll")
    def enroll(body: _EnrollBody):
        if not validate_pubkey(body.pubkey):
            raise HTTPException(status_code=400, detail="invalid ssh public key")
        try:
            rid = store.create(body.pubkey, body.hostname)
        except PendingCapReached:
            raise HTTPException(status_code=429, detail="too many pending requests; try later")
        except ValueError:
            # Store self-validates (belt-and-suspenders); surface its rejection
            # as a clean 400 rather than letting it bubble up as a 500.
            raise HTTPException(status_code=400, detail="invalid ssh public key")
        return {"id": rid, "status": "pending"}

    @app.get("/status/{rid}")
    def status(rid: str):
        return {"id": rid, "status": store.get(rid)}

    return app
