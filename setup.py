"""Build hook: the wheel carries the shipped subset of examples/esp32-s3-board as polybench/examples/esp32-s3.

Everything else is in pyproject.toml. The subset rule is `is_shipped` in src/polybench/example_cmd.py, loaded by
path because the package is not importable while it is being built.
"""
import importlib.util
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent


def _example_cmd():
    spec = importlib.util.spec_from_file_location("_pb_example_cmd", ROOT / "src" / "polybench" / "example_cmd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class build_py_with_examples(build_py):
    def run(self):
        super().run()
        mod = _example_cmd()
        for name, meta in mod.EXAMPLES.items():
            src = ROOT / "examples" / meta["repo_dir"]
            for rel in mod.shipped_files(src):
                dst = Path(self.build_lib) / "polybench" / "examples" / name / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / rel, dst)


setup(cmdclass={"build_py": build_py_with_examples})
