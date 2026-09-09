# django-autopay

Recurring / off-session charge extension for [django-payments](https://django-payments.readthedocs.io/en/latest/): save a payment method once, then charge it later without the user present — across PayPal, Mollie and Stripe with one uniform API.

Documentation: https://django-autopay.readthedocs.io/en/latest/

Example: [Run the interactive demo app](https://django-autopay.readthedocs.io/en/latest/quickstart.html#run-the-demo-app)

**Status: work in progress (0.1.0).** APIs are not stable yet.

## Quick Start

### Installation

**Supported versions:** Python >= 3.10, Django >= 5.2 (tested with 5.2/6.1).

```bash
pip install "django-autopay[all]"
```

Or with uv:

```bash
uv add "django-autopay[all]"
```

**Note:** The `[api]` extra (django-bolt) requires Python >= 3.12. On Python 3.10/3.11, installing `[all]` will skip django-bolt.

### Install Extras

| Extra | Description | Python |
|-------|-------------|--------|
| `[stripe]` | Stripe provider only | >= 3.10 |
| `[paypal]` | PayPal provider only | >= 3.10 |
| `[mollie]` | Mollie provider only | >= 3.10 |
| `[api]` | REST API (django-bolt) | >= 3.12 ⚠️ |
| `[all]` | All providers + API | >= 3.12 ⚠️ |
| `[dev]` | Development dependencies | >= 3.10 |

### Install from GitHub

The package installs straight from the git repository — useful before the first PyPI release or to track `main`:

```bash
uv add "django-autopay @ git+https://github.com/Mte90/django-autopay"
```

With all providers and the REST API:

```bash
uv add "django-autopay[all] @ git+https://github.com/Mte90/django-autopay"
```

Pin a branch, tag or commit by appending `@<ref>`:

```bash
uv add "django-autopay @ git+https://github.com/Mte90/django-autopay@v0.1.0"
```
