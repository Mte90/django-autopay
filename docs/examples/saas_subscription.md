# SaaS Subscription Example

Implement recurring billing for a SaaS application.

## Model

```python
from django.db import models

class Subscription(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    plan = models.ForeignKey("Plan", on_delete=models.PROTECT)
    saved_method = models.OneToOneField(
        "autopay.SavedPaymentMethod",
        on_delete=models.SET_NULL,
        null=True,
    )
    status = models.CharField(max_length=20, choices=[
        ("active", "Active"),
        ("past_due", "Past Due"),
        ("canceled", "Canceled"),
    ])
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
```

## Onboarding Flow

```python
from autopay.core.facade import Payments

def create_subscription(user, plan, payment):
    """Create subscription after user saves payment method."""
    # 1. Save payment method
    saved_method = Payments.save_method(payment)
    
    # 2. Create subscription (store the value object's ID on the FK)
    subscription = Subscription.objects.create(
        user=user,
        plan=plan,
        saved_method_id=saved_method.id,
        status="active",
        current_period_start=timezone.now(),
        current_period_end=timezone.now() + timedelta(days=30),
    )
    
    return subscription
```

## Monthly Billing

```python
from autopay.core.facade import Payments
from autopay.core.result import ChargeStatus

def process_monthly_billing(subscription):
    """Charge subscription for current period."""
    result = Payments.charge_off_session(
        saved_method=subscription.saved_method,
        amount=subscription.plan.price,
        currency="EUR",
        description=f"Subscription {subscription.id}",
        idempotency_key=f"sub_{subscription.id}_{subscription.current_period_start}",
    )
    
    if result.status == ChargeStatus.CONFIRMED:
        subscription.status = "active"
        subscription.current_period_start = subscription.current_period_end
        subscription.current_period_end += timedelta(days=30)
        subscription.save()
        return True
    
    elif result.status == ChargeStatus.REJECTED:
        # Token revoked, mark for refresh
        subscription.status = "past_due"
        subscription.save()
        send_refresh_email(subscription.user)
        return False
    
    else:
        # Failed or pending
        subscription.status = "past_due"
        subscription.save()
        return False
```

## Handling Refresh

```python
from autopay.core.facade import Payments

def handle_refresh_request(user):
    """User clicked 'update payment method'."""
    # List their saved methods
    methods = Payments.list_saved_methods(user)
    
    for method in methods:
        # Start refresh flow
        setup = Payments.refresh_method(method, reason="manual")
        
        # Return setup data to frontend
        # setup.return_url already embeds the attempt ID and confirmation route
        return {
            "setup_id": setup.setup_id,
            "frontend_sdk": setup.frontend_sdk,
            "frontend_payload": setup.frontend_payload,
            "return_url": setup.return_url,
        }
```

## Webhook Handler

```python
from autopay.core.facade import Payments
from autopay.core.result import ChargeStatus

def webhook_handler(variant, raw_body, headers):
    """Process webhook events.
    
    Note: django-autopay ships with a ready-made webhook view at
    POST /payments/webhook/<variant>/ (wired via include("autopay.core.urls")).
    This handler is for users with custom routing needs.
    
    Args:
        variant: Payment provider variant (e.g., 'stripe', 'paypal')
        raw_body: Raw request body as bytes (NOT a parsed dict)
        headers: Request headers dict
    """
    result = Payments.process_webhook(variant, raw_body, headers=headers)
    
    if result.action == "status_update":
        # Update subscription status based on charge confirmation
        # For status_update, transaction_id is the charge's transaction ID
        from autopay.core.models import ChargeRecord
        charge = ChargeRecord.objects.get(transaction_id=result.transaction_id)
        subscription = charge.subscription  # or however you link the charge to a subscription
        if result.new_status == ChargeStatus.CONFIRMED:
            subscription.status = "active"
        subscription.save()
    
    elif result.action == "revoke_method":
        # Mark method as inactive - must save the saved_method object itself
        subscription = Subscription.objects.get(
            saved_method__gateway_method_id=result.transaction_id
        )
        subscription.saved_method.is_active = False
        subscription.saved_method.save()  # Persist the is_active change
        subscription.status = "past_due"
        subscription.save()
        send_payment_required_email(subscription.user)
    
    return {"status": "ok"}
```

## Scheduled Job

```python
from autopay.core.facade import Payments
from autopay.core.result import ChargeStatus
from decimal import Decimal

def daily_billing_job():
    """Run daily to process failed payments."""
    from .models import Subscription
    
    # Charge all past_due subscriptions
    for subscription in Subscription.objects.filter(status="past_due"):
        if not subscription.saved_method:
            continue
            
        result = Payments.charge_off_session(
            saved_method=subscription.saved_method,  # Pass the SavedPaymentMethod instance
            amount=subscription.plan.price,
            currency="EUR",
            description=f"Retry billing for subscription {subscription.id}",
            idempotency_key=f"retry_{subscription.id}_{subscription.current_period_start}",
        )
        
        if result.status == ChargeStatus.CONFIRMED:
            subscription.status = "active"
            subscription.save()
        elif result.status == ChargeStatus.REJECTED:
            # Token permanently revoked
            send_payment_required_email(subscription.user)
```