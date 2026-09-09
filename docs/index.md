# django-autopay Documentation

## Overview

django-autopay extends [django-payments](https://django-payments.readthedocs.io/en/latest/) with recurring/off-session charge capabilities. Save a payment method once, then charge it later without the user present — across PayPal, Mollie, and Stripe with one uniform API.

## Contents

### Getting Started
- [Quick Start](quickstart.md) - Install and make your first charge
- [Configuration](configuration.md) - Settings and PAYMENT_VARIANTS
- [Demo App](quickstart.md#run-the-demo-app) - Runnable end-to-end example shipped with the repository

### Providers
- [PayPal Provider](providers/paypal.md) - OAuth2, vault handling, refresh
- [Stripe Provider](providers/stripe.md) - PaymentIntents, zero-decimal currencies
- [Mollie Provider](providers/mollie.md) - Payment methods, IP allowlist
- [Extending Providers](providers/extending.md) - Add a new gateway provider

### Features
- [Refresh Flow](refresh.md) - Token renewal and SCA handling
- [Webhooks](webhooks.md) - Deduplication, composite keys, endpoint wiring
- [API Reference](api.md) - REST layer with django-bolt
- [Models & Results](models.md) - Database models and result types reference

### Examples
- [SaaS Subscription](examples/saas_subscription.md)
- [Utility Billing](examples/utility_billing.md)
- [Pay As You Go](examples/pay_as_you_go.md)

## Development

See the [Quick Start Guide](quickstart.md) for installation with the `[dev]` extra and setup instructions.
