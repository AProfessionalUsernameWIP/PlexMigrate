"""Restore job package (Plex-native + adapter engines).

LOAD-BEARING INVARIANT for contributors editing this package:
NEVER ``from services.state import _lib_successes`` or ``_lib_failures``
at module load time. These names are ContextVar-backed proxies via
``state.__getattr__`` (PEP 562). A ``from`` import evaluates the proxy
ONCE at module load and freezes the resolved value (typically None
at boot time) into the importing module's namespace forever, after
which subsequent ``reset_run_state`` writes are invisible to that
module. The result is silently corrupted run-state tracking: the
dashboard reads zeros while the engine has accumulated real counts.

Always access them indirectly at use sites: ``state._lib_successes``,
``state._lib_failures``. The same rule applies to any other state
attribute documented as ContextVar-backed in services/state.py.
"""
