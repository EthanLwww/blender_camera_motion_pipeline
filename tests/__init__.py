"""Test-suite scaffolding.

Two kinds of test live here:

* **pure tests** (``test_path_utils``, ``test_config``, ``test_motion_templates``)
  import nothing from Blender and can run under any Python 3.10+ interpreter;
* **Blender tests** (``test_blender_integration``, ``test_camera_validation``)
  need ``bpy`` and are driven by ``tests/run_blender_tests.py``.

``tests/harness.py`` provides a tiny assertion runner so the suite has no
third-party dependency (pytest is not available inside a stock Blender).
"""
