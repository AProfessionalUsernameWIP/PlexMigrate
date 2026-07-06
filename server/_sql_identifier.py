"""Validator for SQL table / column identifiers embedded in f-strings.

SQLite cannot parameterize identifiers, so f-strings are the only path
when the table or column varies. This module makes that path explicit:
every f-string-built identifier passes through ``safe_identifier``,
which rejects anything not in the caller-provided allowlist. A future
refactor that accidentally accepts user-controlled input is then a
loud error at the validator instead of a latent injection.
"""
from __future__ import annotations

from typing import Iterable


class UnsafeIdentifierError(ValueError):
    """Raised when a candidate identifier is not in the allowlist."""


def safe_identifier(name: str, allowed: Iterable[str]) -> str:
    """Return ``name`` unchanged if it is in ``allowed``, else raise.

    Use at every f-string call site that interpolates a table or
    column name from outside its immediate function body:

        cols = safe_identifier(col, ALLOWED_COLS)
        cur.execute(f"SELECT {cols} FROM items WHERE id = ?", (id,))

    The allowlist must be a closed set of literal strings the caller
    controls. Passing a dynamic allowlist (one derived from PRAGMA or
    a user-controlled config) defeats the protection."""
    allowed_set = frozenset(allowed)
    if name not in allowed_set:
        raise UnsafeIdentifierError(
            f"identifier {name!r} not in allowlist {sorted(allowed_set)}"
        )
    return name
