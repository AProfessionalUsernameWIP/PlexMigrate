"""Per-route-group APIRouter modules wired into ``server.app.create_app``.

See ``server/app.py`` for the include order; each submodule exports a
single ``router`` symbol carrying its own prefix + tags.
"""
