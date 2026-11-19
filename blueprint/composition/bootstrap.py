"""Assembly entrypoint: choose a profile, build the container, start a driver."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from blueprint.composition.container import Container
from blueprint.operations.adapters import SystemClock
from blueprint.operations.drivers import CliDriver


def build(profile: str = "memory") -> Container:
    if profile != "memory":
        raise ValueError(f"unsupported profile: {profile}")
    return Container()


def main(argv: Sequence[str] | None = None) -> int:
    container = build(os.getenv("LOGISTICS_PROFILE", "memory"))
    return CliDriver(container, SystemClock()).run(
        list(argv) if argv is not None else sys.argv[1:]
    )


if __name__ == "__main__":
    raise SystemExit(main())
