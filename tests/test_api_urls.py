"""Test for autopay/api/urls.py."""
import pytest

# Skip all tests if django_bolt is not available (Python < 3.12)
pytest.importorskip("django_bolt")

from unittest.mock import patch

from django.test import SimpleTestCase


class TestApiUrls(SimpleTestCase):
    """Tests for API URLs."""

    def test_urls_file_structure(self):
        """Verify urls.py file structure without importing BoltAPI."""
        # Read the file and verify the structure
        with open("autopay/api/urls.py", "r") as f:
            content = f.read()
        assert "app_name = \"autopay_api\"" in content
        assert "urlpatterns" in content
        assert "from .bolt import api" in content

    @patch("autopay.api.bolt.api")
    def test_urls_module_imports(self, mock_api):
        """Verify urls module is importable with mock."""
        mock_api.urls = ([], "autopay_api", None)
        import autopay.api.urls as urls_module
        assert hasattr(urls_module, "urlpatterns")
        assert urls_module.app_name == "autopay_api"