# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The daemon's RPC method registry.

Methods are registered once, in one table, so that a single definition provides
the dispatch entry, the runtime parameter validation, and the material for the
generated protocol reference. Writing the method table by hand in the docs would
guarantee it drifts from the implementation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from pb_core.jsonrpc import ErrorCode, JsonRpcError

if TYPE_CHECKING:
    from pbd.session import Session

logger = logging.getLogger(__name__)

__all__ = ["METHODS", "Method", "MethodFunc", "method", "require"]

MethodFunc = Callable[["Session", dict[str, Any]], Coroutine[Any, Any, Any]]


@dataclass(frozen=True, slots=True)
class Method:
    """One callable RPC method."""

    name: str
    func: MethodFunc
    summary: str
    since: int = 1
    needs_hello: bool = True
    """Whether the caller must have completed the handshake first."""

    params: dict[str, str] = field(default_factory=dict[str, str])
    """Parameter name to description, used by the generated reference."""

    returns: str = ""


METHODS: Final[dict[str, Method]] = {}


def method(
    name: str,
    summary: str,
    *,
    since: int = 1,
    needs_hello: bool = True,
    params: dict[str, str] | None = None,
    returns: str = "",
) -> Callable[[MethodFunc], MethodFunc]:
    """Register an RPC method.

    Args:
        name: Wire name, for example ``daemon.info``.
        summary: One line describing what it does, used in the reference.
        since: Protocol version that introduced it.
        needs_hello: Whether the handshake must have completed first.
        params: Parameter descriptions for the reference.
        returns: Description of the result for the reference.

    Returns:
        A decorator that registers the function and returns it unchanged.
    """

    def register(func: MethodFunc) -> MethodFunc:
        if name in METHODS:
            raise RuntimeError(f"duplicate RPC method: {name}")
        METHODS[name] = Method(
            name=name,
            func=func,
            summary=summary,
            since=since,
            needs_hello=needs_hello,
            params=params or {},
            returns=returns,
        )
        return func

    return register


def require(params: dict[str, Any], key: str, kind: type[Any]) -> Any:
    """Read a required parameter, or raise an invalid-params error.

    The daemon does not trust its input: a client is another process, and over
    TCP potentially another machine.

    Args:
        params: The request's parameters.
        key: Parameter name.
        kind: Expected type.

    Returns:
        The parameter value.

    Raises:
        JsonRpcError: If the parameter is missing or the wrong type.
    """
    if key not in params:
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, f"missing parameter: {key}")
    value = params[key]
    if not isinstance(value, kind):
        raise JsonRpcError(
            ErrorCode.INVALID_PARAMS,
            f"parameter {key} must be {kind.__name__}, got {type(value).__name__}",
        )
    return value
