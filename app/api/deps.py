"""FastAPI dependency wiring — currently just the DB session.

Kept as its own module (rather than importing app.db.session.get_db
directly in every router) so routers depend on app.api, not app.db,
matching the layering app/db/session.py's own docstring anticipates.
"""

from __future__ import annotations

from app.db.session import get_db

__all__ = ["get_db"]
