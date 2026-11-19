"""Execution engine and use-case shape. Contains no scenario knowledge."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from .model import Context, Error, Result

Req = TypeVar("Req")
Res = TypeVar("Res")


class UseCase(Protocol[Req, Res]):
    name: str

    def execute(self, request: Req, ctx: Context) -> Result[Res]: ...


class Interceptor(Protocol):
    def before(self, name: str, ctx: Context) -> None: ...

    def after(self, name: str, ctx: Context, result: Result[Any]) -> None: ...


class Runner:
    """Run any use case with interceptors and a technical error fallback."""

    def __init__(self, interceptors: list[Interceptor] | None = None) -> None:
        self._interceptors = list(interceptors or [])

    def run(self, usecase: UseCase[Req, Res], request: Req, ctx: Context) -> Result[Res]:
        for item in self._interceptors:
            item.before(usecase.name, ctx)
        try:
            result = usecase.execute(request, ctx)
        except Exception as exc:
            result = Result.failure(Error("unexpected", str(exc), "technical"))
        for item in self._interceptors:
            item.after(usecase.name, ctx, result)
        return result
