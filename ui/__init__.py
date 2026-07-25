"""Presentation layer for the Streamlit dashboard (design.md).

This package consumes ``src/`` and is never imported by it (decision D-8). That one-way
dependency is what keeps the framework choice reversible: swapping Streamlit for a React
frontend behind FastAPI would replace this package and touch nothing else.
"""

from __future__ import annotations

__all__ = ["styles", "components"]
