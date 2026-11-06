"""
pytest configuration for ESFEX tests (markers, Julia skip, Qt application).
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


@pytest.fixture(scope="session")
def qapp():
    """The process's QApplication, created on first use.

    Always a full QApplication: tests that build widgets reuse whatever
    application exists, and a widget on a bare QCoreApplication aborts the
    whole process. QtWebEngineWidgets is imported first because it must be
    loaded before the application object is created.
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        try:
            import PySide6.QtWebEngineWidgets  # noqa: F401
        except ImportError:
            pass
        app = QApplication([])
    elif not isinstance(app, QApplication):
        pytest.fail("A test created a QCoreApplication; use the qapp fixture instead.")
    return app
