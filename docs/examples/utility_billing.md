# Utility Billing Example

Implement usage-based billing for utilities (electricity, water, etc.).

## Model

```python
from django.db import models

class UsageRecord(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    meter_reading = models.DecimalField(max_digits=10, decimal_places=2)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    amount_due = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=[
        ("unbilled", "Unbilled"),
        ("billed", "Billed"),
        ("paid", "Paid"),
    ])
```

## Monthly Invoice

```python
from autopay.core.facade import Payments
from autopay.core.models import SavedPaymentMethod, ChargeRecord
from autopay.core.result import ChargeStatus

def generate_invoice(user, period_end):
    """Generate invoice for user's usage."""
    # Calculate usage
    records = UsageRecord.objects.filter(
        user=user,
        period_end__lte=period_end,
        status="unbilled"
    )
    
    total = sum(r.amount_due for r in records)
    
    # Charge saved method
    method = SavedPaymentMethod.objects.filter(user=user, is_active=True).first()
    if not method:
        raise ValueError("No payment method on file")
    
    result = Payments.charge_off_session(
        saved_method=method,
        amount=total,
        currency="EUR",
        description=f"Utility bill {period_end.date()}",
        idempotency_key=f"bill_{user.id}_{period_end.date()}",
    )
    
    # Mark records as billed
    records.update(status="billed")
    
    return result
```

## Payment Reminder

```python
def send_payment_reminder(user):
    """Send reminder if payment failed."""
    from autopay.core.result import ChargeStatus
    
    unpaid = UsageRecord.objects.filter(
        user=user,
        status="billed"
    ).exclude(
        pk__in=ChargeRecord.objects.filter(status=ChargeStatus.CONFIRMED.value).values("saved_method_id")
    )
    
    if unpaid.exists():
        send_email(
            user.email,
            "Payment Required",
            f"Your utility bill of {unpaid[0].amount_due} EUR is due."
        )
```

## Auto-Top-Up

```python
from autopay.core.facade import Payments
from autopay.core.result import ChargeStatus

class Account(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    auto_top_up = models.BooleanField(default=False)
    top_up_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=10)
    top_up_amount = models.DecimalField(max_digits=10, decimal_places=2, default=50)

def check_auto_top_up(account):
    """Auto top-up when balance is low."""
    if not account.auto_top_up:
        return
    
    if account.balance < account.top_up_threshold:
        method = SavedPaymentMethod.objects.filter(
            user=account.user, is_active=True
        ).first()
        
        if method:
            result = Payments.charge_off_session(
                saved_method=method,
                amount=account.top_up_amount,
                currency="EUR",
                description="Account top-up",
            )
            
            if result.status == ChargeStatus.CONFIRMED:
                account.balance += account.top_up_amount
                account.save()
```