"""Charts draw Japanese text: a CJK font backs up matplotlib's default font."""
import warnings

import pytest

matplotlib = pytest.importorskip("matplotlib")


def test_cjk_text_renders_without_missing_glyphs():
    from esfex.visualization.plot_fonts import configure_cjk_fallback
    font = configure_cjk_fallback()
    if font is None:
        pytest.skip("no CJK font installed on this machine")
    assert matplotlib.rcParams["font.family"][0] != font      # Latin look kept
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    ax.set_title("需要予測 — Demand (MW)")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fig.canvas.draw()
    plt.close(fig)
    assert not [w for w in caught if "missing from font" in str(w.message)]
