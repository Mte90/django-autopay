"""Dummy provider for testing and development.

Simulates charge off-session, refresh, and webhook events without network calls.
"""

from .provider import DummyAutopayProvider

__all__ = ["DummyAutopayProvider"]