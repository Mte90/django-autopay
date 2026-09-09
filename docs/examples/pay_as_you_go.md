# Pay As You Go Example

Implement on-demand charging for usage-based services.

## Model

```python
from django.db import models

class CreditPurchase(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=[
        ("pending", "Pending"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ])
    created_at = models.DateTimeField(auto_now_add=True)
```

## On-Demand Purchase

```python
from autopay.core.facade import Payments
from autopay.core.models import SavedPaymentMethod
from autopay.core.result import ChargeStatus

def purchase_credits(user, amount):
    """User purchases credits on-demand."""
    method = SavedPaymentMethod.objects.filter(user=user, is_active=True).first()
    
    if not method:
        # No payment method, redirect to onboarding
        return {"error": "No payment method on file"}
    
    result = Payments.charge_off_session(
        saved_method=method,
        amount=amount,
        currency="EUR",
        description=f"Credit purchase",
        idempotency_key=f"credits_{user.id}_{int(time.time())}",
    )
    
    purchase = CreditPurchase.objects.create(
        user=user,
        amount=amount,
        status="completed" if result.status == ChargeStatus.CONFIRMED else "failed",
    )
    
    if result.status == ChargeStatus.CONFIRMED:
        user.credits += amount
        user.save()
    
    return {"status": result.status.value, "transaction_id": result.transaction_id}
```

## One-Click Purchase

```python
from autopay.core.result import ChargeStatus

def one_click_purchase(user, amount):
    """Fast purchase with saved method."""
    methods = Payments.list_saved_methods(user)
    
    if not methods:
        return {"error": "No payment method saved"}
    
    # Use first active method
    method = methods[0]
    
    result = Payments.charge_off_session(
        saved_method=method,
        amount=amount,
        currency="EUR",
        description="Quick purchase",
    )
    
    return {
        "success": result.status == ChargeStatus.CONFIRMED,
        "message": result.message,
    }
```

## Batch Charges

```python
from autopay.core.models import SavedPaymentMethod
from autopay.core.result import ChargeStatus

def process_batch_charges(items):
    """Charge multiple items in one transaction."""
    user = items[0].user
    total = sum(item.price for item in items)
    
    method = SavedPaymentMethod.objects.filter(user=user, is_active=True).first()
    
    result = Payments.charge_off_session(
        saved_method=method,
        amount=total,
        currency="EUR",
        description=f"Batch purchase ({len(items)} items)",
        idempotency_key=f"batch_{user.id}_{int(time.time())}",
    )
    
    if result.status == ChargeStatus.CONFIRMED:
        # Mark all items as purchased
        for item in items:
            item.purchased = True
            item.save()
    
    return result
```

## Retry Failed Purchases

```python
from autopay.core.models import SavedPaymentMethod
from autopay.core.result import ChargeStatus

def retry_failed_purchase(purchase):
    """Retry a failed credit purchase."""
    method = SavedPaymentMethod.objects.get(pk=purchase.user.saved_method_id)
    
    result = Payments.charge_off_session(
        saved_method=method,
        amount=purchase.amount,
        currency="EUR",
        description=f"Retry: Credit purchase",
        idempotency_key=f"credits_{purchase.user.id}_{purchase.id}",
    )
    
    purchase.status = "completed" if result.status == ChargeStatus.CONFIRMED else "failed"
    purchase.save()
    
    return result
```