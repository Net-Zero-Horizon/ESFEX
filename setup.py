"""Custom build step: bundle the ``docs/`` tree into the installed package.

The in-app documentation viewer (``esfex.visualization.panels.doc_viewer``)
reads the Markdown docs from disk. In a source checkout they live at the repo
root (``docs/``), which is also what MkDocs / ReadTheDocs and the README links
use — so we keep them there. But a ``pip``-installed package has no repo
checkout, so the viewer would find nothing. To fix that we copy a snapshot of
``docs/`` into ``esfex/docs`` at build time; the viewer prefers that packaged
copy and falls back to the repo-root ``docs/`` for editable/dev installs.

All other project metadata lives in ``pyproject.toml``; this file only adds the
custom build command.
"""

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class _BuildPyWithDocs(build_py):
    def run(self):
        super().run()
        src = Path(__file__).parent / "docs"
        if src.exists():
            dst = Path(self.build_lib) / "esfex" / "docs"
            shutil.copytree(src, dst, dirs_exist_ok=True)

    def get_outputs(self, *args, **kwargs):
        # Make the copied docs part of the recorded build outputs so the wheel
        # includes them.
        outputs = list(super().get_outputs(*args, **kwargs))
        docs_root = Path(self.build_lib) / "esfex" / "docs"
        if docs_root.exists():
            outputs += [str(p) for p in docs_root.rglob("*") if p.is_file()]
        return outputs


setup(cmdclass={"build_py": _BuildPyWithDocs})
