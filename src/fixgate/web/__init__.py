"""The local web page: approvals, tasks, traces and benchmark reports."""

from .app import create_app, serve

__all__ = ["create_app", "serve"]
