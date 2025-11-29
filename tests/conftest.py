"""Pytest configuration for Lab Tracker tests."""

import os

import django
from django.conf import settings


def pytest_configure():
    """Configure Django settings for pytest."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lab_tracker.settings")
    django.setup()
