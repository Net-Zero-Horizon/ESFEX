"""
pytest configuration for ESFEX tests (markers, Julia skip, Qt application).
"""
import pytest


def pytest_configure(config):
    """Register custom markers; keep tests off the network."""
    import os
    # Tests never download datasets from the data release (esfex.data_assets):
    # a missing raster stays missing unless a test points this at a mirror.
    os.environ.setdefault("ESFEX_ASSETS_URL", "file:///nonexistent/esfex-test-assets")
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


def pytest_sessionfinish(session, exitstatus):
    session.config._esfex_exitstatus = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    """On CI, end the process without the interpreter's shutdown.

    Since November 2026 the CI runners segfault after the test summary and
    the coverage report, while Python tears down Qt and the other extension
    modules (exit code 139 with every test passed, on 0.3.0's code too).
    Everything is written by then, so pytest's own exit status is returned.
    """
    import os
    import sys
    if not os.environ.get("CI") or not hasattr(config, "_esfex_exitstatus"):
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(config._esfex_exitstatus)


@pytest.fixture(autouse=True)
def _studio_language_back_to_english():
    """Every test starts in English: a test that switches the Studio's
    language (init_i18n("es")) must not leak it into the tests after it,
    whose messages now go through tr()."""
    yield
    import sys
    i18n = sys.modules.get("esfex.visualization.i18n")
    if i18n is not None and i18n.get_language() != "en":
        i18n.init_i18n("en")
