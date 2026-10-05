"""Engine interface: everything the worker needs to run one job and report on it."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from common.messages import JobSpec

LogFn = Callable[[str, str], None]


@dataclass
class EngineResult:
    status: str = "ok"
    encoder: str = ""
    device: str = ""
    command: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    stderr_tail: str = ""
    error: str = ""


@dataclass
class JobContext:
    """Everything an engine is allowed to touch.

    Engines never write to the network. They receive a progress callback and a
    cancellation predicate, and the session thread is what turns those into
    ``PROGRESS`` frames on the wire. That separation is what keeps a slow encode
    from blocking the connection reader.
    """

    job_id: str
    spec: JobSpec
    input_path: Path
    output_path: Path
    progress: Callable[..., None]
    log: LogFn
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()

    def report(self, pct: float, stage: str, detail: str = "", **extra) -> None:
        self.progress(pct, stage, detail, **extra)


class Engine(ABC):
    """A pluggable execution backend."""

    name: str = "base"
    description: str = ""

    @abstractmethod
    def available(self) -> bool:
        """True when this engine can run right now on this machine."""

    @abstractmethod
    def run(self, ctx: JobContext) -> EngineResult:
        """Execute the job. Must honour ``ctx.cancel()`` promptly."""


__all__ = ["Engine", "EngineResult", "JobContext", "LogFn"]