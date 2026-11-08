"""Package-local application entry point.

This alias keeps both ``uvicorn main:app`` and
``uvicorn backend.main:app`` launch commands working.
"""

from main import app

__all__ = ["app"]
