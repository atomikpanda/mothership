from __future__ import annotations
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mship.core.relay.enroll import RequestStore, PendingCapReached, validate_pubkey


class _EnrollBody(BaseModel):
    pubkey: str
    hostname: str = ""


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
        return {"id": rid, "status": "pending"}

    @app.get("/status/{rid}")
    def status(rid: str):
        return {"id": rid, "status": store.get(rid)}

    return app
