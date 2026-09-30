"""Admin diagnostics endpoints for system monitoring and troubleshooting."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from api.routes import get_conn
from api.security import require_admin

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
