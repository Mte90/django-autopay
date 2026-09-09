"""MkDocs hook: configure Django before griffe imports autopay modules."""
import os
import sys
from pathlib import Path

import django

ROOT = Path(__file__).resolve().parent.parent


def on_pre_build(config):
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
    django.setup()
