"""Backward-compatible wrapper for the release builder implementation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_release_module() -> ModuleType:
    """Load the release implementation module from its internal path.

    Returns:
        ModuleType: Loaded implementation module.

    Raises:
        RuntimeError: If the implementation module cannot be loaded.
    """
    implementation_path = (
        Path(__file__).resolve().parent / "release" / "build_release.py"
    )
    implementation_spec = importlib.util.spec_from_file_location(
        "scripts_release_build_release",
        implementation_path,
    )
    if implementation_spec is None or implementation_spec.loader is None:
        raise RuntimeError(
            f"Unable to load release implementation: {implementation_path}"
        )

    implementation_module = importlib.util.module_from_spec(implementation_spec)
    implementation_spec.loader.exec_module(implementation_module)
    return implementation_module


_release_module = _load_release_module()
__doc__ = getattr(_release_module, "__doc__", __doc__)

for exported_name in dir(_release_module):
    if exported_name.startswith("__") and exported_name != "__all__":
        continue
    globals()[exported_name] = getattr(_release_module, exported_name)


def main() -> int:
    """Invoke the release builder CLI entrypoint."""
    return _release_module.main()


if __name__ == "__main__":
    raise SystemExit(main())
