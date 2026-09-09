"""Demo app: re-exports the core model. No demo-specific models."""

from autopay.core.models import SavedPaymentMethod

__all__ = ["SavedPaymentMethod"]
