"""Errors every model gateway shares, so the API can map them to one status."""

from __future__ import annotations


class ModelUnavailableError(RuntimeError):
    """The configured model could not produce a usable answer."""
