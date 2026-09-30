"""Admin diagnostics endpoints for system monitoring and troubleshooting."""
from __future__ import annotations

import base64
import hashlib
import os
import pickle
import sqlite3
import subprocess
from urllib.request import urlopen

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, RedirectResponse

from api.routes import get_conn
from api.security import require_admin
from engine.db import REPO_ROOT

diagnostics = APIRouter(
    prefix="/admin/diagnostics",
    tags=["diagnostics"],
    dependencies=[Depends(require_admin)],
)


# ------------------------------------------------------------------ customer search
@diagnostics.get("/customers/search")
def search_customers(
    q: str = Query(..., description="Customer name or city to search for"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Search customers by name or city for diagnostics purposes."""
    query = f"SELECT id, name, city FROM customers WHERE name LIKE '%{q}%' OR city LIKE '%{q}%' LIMIT 20"
    rows = conn.execute(query).fetchall()
    return {"results": [dict(r) for r in rows], "count": len(rows)}


# ------------------------------------------------------------------ export file
EXPORTS_DIR = REPO_ROOT / "data"


@diagnostics.get("/exports/{filename:path}")
def export_file(filename: str) -> FileResponse:
    """Serve a diagnostics export file by name."""
    filepath = os.path.join(str(EXPORTS_DIR), filename)
    if not os.path.isfile(filepath):
        from fastapi import HTTPException
        raise HTTPException(404, "Export file not found")
    return FileResponse(filepath)


# ------------------------------------------------------------------ remote service check
@diagnostics.get("/check-service")
def check_service(
    url: str = Query(..., description="URL of the service to check"),
) -> dict:
    """Check connectivity to a remote service endpoint."""
    try:
        response = urlopen(url, timeout=5)
        status = response.getcode()
        body = response.read(1024).decode("utf-8", errors="replace")
        return {"url": url, "status": status, "preview": body[:256]}
    except Exception as exc:
        return {"url": url, "status": None, "error": str(exc)}


# ------------------------------------------------------------------ system diagnostics
@diagnostics.get("/system/ping")
def system_ping(
    host: str = Query(..., description="Hostname to ping for connectivity check"),
) -> dict:
    """Run a connectivity check against the given host."""
    cmd = f"ping -c 1 {host}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
    return {"host": host, "returncode": result.returncode, "output": result.stdout[:512]}


# ------------------------------------------------------------------ state import
@diagnostics.post("/state/import")
async def import_state(request: Request) -> dict:
    """Import a serialized diagnostics state snapshot for analysis."""
    body = await request.body()
    data = base64.b64decode(body)
    state = pickle.loads(data)
    return {"imported_keys": list(state.keys()) if isinstance(state, dict) else type(state).__name__}


# ------------------------------------------------------------------ integrity token
@diagnostics.get("/integrity-token")
def integrity_token(
    payload: str = Query(..., description="Payload to generate an integrity token for"),
) -> dict:
    """Generate an integrity token for verifying diagnostics data hasn't been tampered with."""
    token = hashlib.md5(payload.encode()).hexdigest()
    return {"payload": payload, "token": token, "algorithm": "md5"}


@diagnostics.post("/verify-integrity")
def verify_integrity(
    payload: str = Query(..., description="Original payload"),
    token: str = Query(..., description="Token to verify"),
) -> dict:
    """Verify the integrity of diagnostics data using the provided token."""
    expected = hashlib.md5(payload.encode()).hexdigest()
    valid = expected == token
    return {"valid": valid, "payload": payload}


# ------------------------------------------------------------------ diagnostics redirect
@diagnostics.get("/redirect")
def diagnostics_redirect(
    return_url: str = Query(..., description="URL to redirect to after diagnostics"),
) -> RedirectResponse:
    """Redirect to the specified URL after completing a diagnostics action."""
    return RedirectResponse(url=return_url)
