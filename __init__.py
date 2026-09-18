"""Blender Motion Pipeline - batch scene loading, camera motion sequence
generation and headless video rendering.

The add-on is intentionally split into UI-free core modules and a thin Blender
UI layer so that every stage can also run through ``blender -b`` on a remote
machine.  Consequently this module must stay importable *without* registering
anything, and must not import ``bpy`` at import time: the CLI and the render
script load the sub-packages directly.
"""

bl_info = {
    "name": "Motion Pipeline",
    "author": "Blender Motion Pipeline",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Motion Pipeline",
    "description": (
        "Batch-load .blend scenes, generate camera motion sequences from JSON "
        "templates with validation/auto-correction, and render them headlessly."
    ),
    "warning": "",
    "doc_url": "",
    "category": "Render",
}


def register():
    """Register every operator, property group and panel.

    Imported lazily so that ``import blender_motion_pipeline`` from a plain
    Python interpreter (tests, tooling) never touches Blender-only modules.
    """
    from . import registration

    registration.register_all()


def unregister():
    from . import registration

    registration.unregister_all()
