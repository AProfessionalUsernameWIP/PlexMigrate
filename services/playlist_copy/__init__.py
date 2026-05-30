"""Re-exports playlist copy adapter implementation as a facade for backwards compatibility."""
from services.playlist_copy.adapter.copy import *  # noqa: F401, F403
from services.playlist_copy.adapter import copy as _copy

# The star-import above also pulls in module-level names that collide with
# package submodules (log, adapter). Remove them so
# ``from services.playlist_copy import log`` resolves to the submodule, not
# the Logger attribute carried over from adapter.copy.
_SUBMODULE_RESERVED = {"log", "adapter"}
for _shadow in list(_SUBMODULE_RESERVED):
    if _shadow in globals():
        del globals()[_shadow]
del _shadow

# Mirror remaining module-level names (including underscore-prefixed private
# helpers tests reach for) so monkeypatch on services.playlist_copy.<name>
# affects the same binding the adapter.copy module's late-lookup callers see.
for _name in dir(_copy):
    if _name.startswith("__"):
        continue
    if _name in _SUBMODULE_RESERVED:
        continue
    globals().setdefault(_name, getattr(_copy, _name))
del _name
