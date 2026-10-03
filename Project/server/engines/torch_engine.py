"""Tensor-compute engine: real CUDA work when PyTorch is present, NumPy otherwise.

This is the second half of Task 2. The transcode engine exercises NVENC (a fixed
function block); this one exercises general compute across thousands of CUDA
cores, which is what makes the client-vs-remote speedup argument in the report
far more dramatic than a codec comparison.

PyTorch is an *optional* dependency. When it is missing the same workload runs on
NumPy so the feature remains demonstrable, and the reported device changes from
``cuda`` to ``cpu`` so nobody mistakes one for the other.

Both paths write their metrics to ``ctx.output_path`` as JSON. A compute job has no
video to hand back, so the artifact *is* the result: without a file on disk the
worker would report a successful job with nothing to download.
"""

from __future__ import annotations

import json
import time

from .base import Engine, EngineResult, JobContext


def _torch():
    try:
        import torch  # type: ignore

        return torch
    except Exception:
        return None


class TorchComputeEngine(Engine):
    name = "compute"
    description = "Matrix / convolution / transformer-block benchmark on CUDA (or NumPy)"

    def __init__(self) -> None:
        torch = _torch()
        self._has_torch = torch is not None
        if torch is None:
            self.device_name = "numpy (CPU fallback)"
        elif torch.cuda.is_available():
            self.device_name = f"cuda: {torch.cuda.get_device_name(0)}"
        else:
            self.device_name = "cpu (torch installed, CUDA unavailable)"

    def available(self) -> bool:
        return True

    def _write_metrics(self, ctx: JobContext, metrics: dict) -> None:
        """Persist the measurements as the job's downloadable output."""
        path = ctx.output_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "engine": self.name,
            "device": self.device_name,
            "operation": ctx.spec.compute_op,
            "size": ctx.spec.compute_size,
            "iterations": ctx.spec.compute_iters,
            "metrics": metrics,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def run(self, ctx: JobContext) -> EngineResult:
        torch = _torch()
        if torch is None:
            return self._run_numpy(ctx)

        spec = ctx.spec
        cuda = torch.cuda.is_available()
        device = torch.device("cuda" if cuda else "cpu")
        result = EngineResult(device=str(self.device_name))
        ctx.log("info", f"compute device: {self.device_name}")

        if cuda:
            torch.cuda.init()
            torch.backends.cudnn.benchmark = True

        try:
            timings = self._dispatch(ctx, torch, device, cuda)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            result.status, result.error = "error", f"{type(exc).__name__}: {exc}"
            return result

        result.status = "ok"
        result.metrics = timings
        if cuda:
            result.metrics["peak_gpu_mem_mb"] = round(
                torch.cuda.max_memory_allocated() / (1024 * 1024), 2
            )
        total = sum(timings.get(k, 0.0) for k in ("warmup_s", "compute_s"))
        if total > 0:
            result.metrics["effective_tflops"] = round(
                self._flop_estimate(spec) / total / 1e12, 3
            )
        try:
            self._write_metrics(ctx, result.metrics)
        except OSError as exc:
            result.status, result.error = "error", f"cannot write metrics: {exc}"
        return result

    def _flop_estimate(self, spec) -> float:
        n = spec.compute_size
        if spec.compute_op == "matmul":
            return 2.0 * n**3 * spec.compute_iters
        if spec.compute_op == "conv2d":
            return 2.0 * (n * 32 * 32 * 64 * 64) * spec.compute_iters
        if spec.compute_op == "transformer_ffn":
            return 4.0 * n**2 * (4 * n) * spec.compute_iters
        return float(n * n * spec.compute_iters)

    def _dispatch(self, ctx: JobContext, torch, device, cuda: bool) -> dict:
        spec = ctx.spec
        n = spec.compute_size
        iters = spec.compute_iters

        if cuda:
            generator = torch.Generator(device=device).manual_seed(1234)
        else:
            generator = torch.Generator().manual_seed(1234)

        def rand(*shape):
            return torch.randn(*shape, device=device, generator=generator)

        ctx.report(5.0, "allocating", f"{n}x{n} tensors on {device}")
        if spec.compute_op == "matmul":
            a, b = rand(n, n), rand(n, n)
            c = torch.empty((n, n), device=device)
            work = lambda: torch.mm(a, b, out=c)
        elif spec.compute_op == "conv2d":
            x = rand(1, 3, n, n)
            w = rand(64, 3, 3, 3)
            work = lambda: torch.nn.functional.conv2d(x, w, padding=1)
        elif spec.compute_op == "transformer_ffn":
            x = rand(n, n)
            w1, w2 = rand(n, 4 * n), rand(4 * n, n)
            work = lambda: torch.matmul(torch.relu(torch.matmul(x, w1)), w2)
        else:
            x, y = rand(n, n), rand(n, n)
            work = lambda: torch.add(torch.mul(x, y), x)

        ctx.report(12.0, "warmup", "one untimed iteration")
        if cuda:
            torch.cuda.synchronize()
        work()
        if cuda:
            torch.cuda.synchronize()

        timings: dict[str, float] = {"warmup_s": 0.0}
        ctx.report(20.0, "computing", f"{iters} timed iterations")
        start = time.perf_counter()
        for index in range(iters):
            work()
            if cuda and index % 4 == 3:
                ctx.report(20.0 + 70.0 * (index + 1) / iters, "computing",
                           f"iteration {index + 1}/{iters}")
        if cuda:
            torch.cuda.synchronize()
        timings["compute_s"] = round(time.perf_counter() - start, 4)
        timings["iters"] = iters
        timings["size"] = n
        ctx.report(96.0, "computing", f"{timings['compute_s']:.3f}s over {iters} iterations")

        digest_source = c if spec.compute_op == "matmul" else work()
        checksum = float(digest_source.double().sum().item()) if cuda else float(
            digest_source.astype("float64").sum()
        )
        timings["checksum"] = round(checksum, 4)
        ctx.log("info", f"compute checksum {checksum:.4f} will be written to "
                         f"{ctx.output_path.name}")
        return timings

    def _run_numpy(self, ctx: JobContext) -> EngineResult:
        import numpy as np

        spec = ctx.spec
        result = EngineResult(device=self.device_name)
        n = spec.compute_size
        ctx.log("warn", "PyTorch is not installed - running the same workload on NumPy/CPU")
        ctx.report(10.0, "allocating", f"{n}x{n} float32 matrices")
        rng = np.random.default_rng(1234)
        a = rng.standard_normal((n, n), dtype=np.float32)
        b = rng.standard_normal((n, n), dtype=np.float32)

        ctx.report(20.0, "warmup", "one untimed iteration")
        a @ b

        ctx.report(30.0, "computing", f"{spec.compute_iters} timed iterations")
        start = time.perf_counter()
        checksum = 0.0
        for index in range(spec.compute_iters):
            product = a @ b
            checksum += float(product[:: max(1, n // 8), :: max(1, n // 8)].sum())
            if index % 2 == 1:
                ctx.report(30.0 + 65.0 * (index + 1) / spec.compute_iters, "computing",
                           f"iteration {index + 1}/{spec.compute_iters}")
        elapsed = time.perf_counter() - start

        result.status = "ok"
        result.metrics = {
            "compute_s": round(elapsed, 4),
            "iters": spec.compute_iters,
            "size": n,
            "checksum": round(checksum, 4),
            "effective_tflops": round(self._flop_estimate(spec) / max(elapsed, 1e-9) / 1e12, 3),
        }
        ctx.report(100.0, "computing", f"{elapsed:.3f}s")
        try:
            self._write_metrics(ctx, result.metrics)
        except OSError as exc:
            result.status, result.error = "error", f"cannot write metrics: {exc}"
        return result


__all__ = ["TorchComputeEngine"]