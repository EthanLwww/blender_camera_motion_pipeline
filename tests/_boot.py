"""Register the add-on package under its historical import name.

The package is name-agnostic -- every module inside it uses relative imports, so
the folder may be called anything -- but the scripts in this folder import it as
``blender_motion_pipeline``.  That name only resolves when something has registered
it, which is what ``../_bootstrap.py`` is for.

Importing this module does that registration, so a test module or probe can run
standalone (``blender -b -P tests/test_config.py``, ``python tests/probe_axes.py``)
instead of only through ``run_blender_tests.py``::

    sys.path.insert(0, HERE)
    import _boot  # noqa: F401  (the add-on folder may be called anything)

It is a no-op when the name already resolves (the folder has the expected name, or
the add-on is installed under it).
"""

from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BOOTSTRAP = os.path.join(os.path.dirname(_HERE), "_bootstrap.py")


def _register() -> str:
    if not os.path.isfile(_BOOTSTRAP):
        return ""
    spec = importlib.util.spec_from_file_location("_mpp_bootstrap", _BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.bootstrap(_BOOTSTRAP)


#: The real name of the package folder serving this test folder ("" when not found).
PACKAGE_NAME = _register()


def ensure() -> str:
    """Idempotent form, for callers that prefer an explicit call."""
    if PACKAGE_NAME:
        return PACKAGE_NAME
    return _register()


if __name__ == "__main__":
    print(f"package: {PACKAGE_NAME or '(not found)'}")
    print(f"bootstrap: {_BOOTSTRAP}")
