"""Django signals emitted by django-autopay.

Applications connect receivers to these signals to apply domain logic that
lives outside autopay (Invoice status, Customer block/unblock, JobLog, etc.)
on verified, deduplicated webhook events.
"""
from django.dispatch import Signal

# Emitted by Payments.process_webhook after signature verification, dedup,
# provider dispatch, and built-in action application (status_update on
# ChargeRecord, revoke_method on SavedPaymentMethod, refund on ChargeRecord).
#
# kwargs:
#   sender=variant     — the payment provider variant (e.g. "paypal")
#   event_type=str    — normalized event type from the gateway payload
#   payload=dict      — the parsed webhook body
#   result=WebhookEventResult — the result returned by the provider
#
# Use django.dispatch.receiver to connect. Receivers are called via send_robust,
# so a failing receiver does not break webhook processing.
webhook_processed = Signal()
