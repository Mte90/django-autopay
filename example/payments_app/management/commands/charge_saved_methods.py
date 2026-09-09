"""Management command to charge all saved payment methods."""
from decimal import Decimal

from django.core.management.base import BaseCommand

from autopay.core.facade import Payments
from autopay.core.models import SavedPaymentMethod
from autopay.core.result import ChargeStatus


class Command(BaseCommand):
    """Charge all active saved payment methods.

    Usage:
        python manage.py charge_saved_methods --amount 10.00 --currency EUR

    This command is designed to be run from a scheduled job (e.g., Celery beat).
    It iterates over all active saved methods and attempts an off-session charge.
    """

    help = "Charge all active saved payment methods"

    def add_arguments(self, parser):
        parser.add_argument(
            "--amount",
            type=Decimal,
            default=Decimal("10.00"),
            help="Amount to charge (default: 10.00)",
        )
        parser.add_argument(
            "--currency",
            type=str,
            default="EUR",
            help="Currency code (default: EUR)",
        )
        parser.add_argument(
            "--description",
            type=str,
            default="Recurring charge",
            help="Charge description (default: Recurring charge)",
        )

    def handle(self, *args, **options):
        amount = options["amount"]
        currency = options["currency"]
        description = options["description"]

        methods = SavedPaymentMethod.objects.filter(is_active=True)
        count = methods.count()

        if count == 0:
            self.stdout.write(self.style.NOTICE("No active saved methods found."))
            return

        self.stdout.write(f"Processing {count} active saved method(s)...")

        success_count = 0
        failed_count = 0

        for method in methods:
            try:
                result = Payments.charge_off_session(
                    saved_method=method,
                    amount=amount,
                    currency=currency,
                    description=description,
                )

                if result.status == ChargeStatus.CONFIRMED:
                    success_count += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"✓ {method.display_label}: {result.transaction_id}"
                        )
                    )
                else:
                    failed_count += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"✗ {method.display_label}: {result.status.value} - {result.message}"
                        )
                    )

            except ValueError as e:
                failed_count += 1
                self.stdout.write(
                    self.style.ERROR(f"✗ {method.display_label}: {e!s}")
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nCompleted: {success_count} succeeded, {failed_count} failed"
            )
        )