"""Tests for package version consistency."""

from importlib.metadata import version

import getpaid_core


def test_version_consistency():
    """Ensure __version__ matches package metadata."""
    pkg_version = version("python-getpaid-core")
    assert getpaid_core.__version__ == pkg_version
