"""
pytest configuration for ESFEX tests (markers, Julia skip).
"""
import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "julia: marks tests as requiring Julia (skip if Julia unavailable)"
    )


def pytest_collection_modifyitems(config, items):
    """Skip Julia tests if juliacall is not available."""
    try:
        import juliacall
        julia_available = True
    except ImportError:
        julia_available = False

    if not julia_available:
        skip_julia = pytest.mark.skip(reason="Julia/juliacall not available")
        for item in items:
            if "julia" in item.keywords:
                item.add_marker(skip_julia)
