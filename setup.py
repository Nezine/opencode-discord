"""CMake-driven build for the native engine extension.

Project metadata lives in pyproject.toml. This file exists only to teach
setuptools how to build ``bot/_engine`` through CMake, so that
``pip install -e .`` produces a working package rather than silently skipping
the C++ half.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_ext import build_ext
from setuptools.extension import Extension

ROOT = Path(__file__).parent
BUILD_TYPE = os.environ.get("ENGINE_BUILD_TYPE", "RelWithDebInfo")


class CMakeExtension(Extension):
    """A placeholder extension; the real artifact is produced by CMake."""

    def __init__(self, name: str) -> None:
        super().__init__(name, sources=[])


class CMakeBuildExt(build_ext):
    def build_extension(self, ext: Extension) -> None:
        build_dir = ROOT / "build"
        build_dir.mkdir(exist_ok=True)

        subprocess.run(
            [
                "cmake",
                "-S",
                str(ROOT),
                "-B",
                str(build_dir),
                f"-DCMAKE_BUILD_TYPE={BUILD_TYPE}",
                f"-DPython3_EXECUTABLE={sys.executable}",
            ],
            check=True,
        )
        subprocess.run(
            ["cmake", "--build", str(build_dir), "--config", BUILD_TYPE],
            check=True,
        )

    def copy_extensions_to_source(self) -> None:
        # CMake writes the module straight into bot/, so there is nothing to copy.
        return


setup(
    ext_modules=[CMakeExtension("bot._engine")],
    cmdclass={"build_ext": CMakeBuildExt},
)
