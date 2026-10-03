# Testing

```powershell
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q
```

```
184 passed in 70.31s
```

The suite runs on a machine with **no GPU, no CUDA and no PyTorch**, and with
the 2013-era FFmpeg build that ships inside some Python wheels. Anything that
needs a real binary skips cleanly instead of failing, so a green run on a
laptop means the same thing as a green run in the lab.

## What each file covers

| File | Tests | Focus |
|---|---|---|
| `tests/test_protocol.py` | 20 | framing: header packing, JSON vs binary dispatch, oversized frames, truncated reads, socket pairs |
| `tests/test_messages.py` | 45 | `JobSpec` / `Capabilities` validation, filename sanitisation, serialisation round-trips |
| `tests/test_checksum.py` | 18 | streaming SHA-256 against known vectors, empty and large inputs, part-file promotion |
| `tests/test_ffmpeg_engine.py` | 39 | command construction, encoder fallback, scaling arithmetic, progress parsing, old-build quirks |
| `tests/test_capabilities.py` | 17 | encoder allow-list, ffmpeg/ffprobe discovery, GPU and PyTorch probing, legend filtering |
| `tests/test_job_queue.py` | 12 | FIFO order, bounded concurrency, cancellation races, pool shutdown |
| `tests/test_e2e.py` | 24 | real daemon + real socket + real ffmpeg: handshake, upload, job, progress, download, error paths |
| `tests/test_gui.py` | 9 | headless Qt: construction, validation, worker-thread dispatch, GUI responsiveness under load |

## The tests that are worth reading first

**`test_gui.py::test_the_gui_event_loop_keeps_running_during_a_job`** is the one
that catches the bug this project is most likely to regress on. It connects a
real window to a real daemon, submits a real encode, and asserts a 5 ms Qt timer
kept firing throughout. Calling a `moveToThread`-ed worker's method directly -
the obvious way to write that code - passes every functional test in the suite
and still freezes the interface for the whole upload, because a plain Python call
ignores Qt thread affinity. This test fails when that mistake is made.

**`test_gui.py::test_connecting_uses_the_address_typed_into_the_window`** guards a
defect that was live for a while: the worker updated its own `host`/`port` but the
transport kept the address it was constructed with, so the address typed into the
GUI was silently ignored and every connection went to the default.

That same end-to-end test also asserts the encoder selection, because on a
CPU-only worker the NVENC entry is greyed out and a greyed-out combo box entry
can still be the *current* one - so the first job from a fresh window would ask
for `h264_nvenc` and be rejected. Submitting is also disabled until the handshake
completes, since the spec depends on the worker's capabilities.

**`test_job_queue.py`** covers the cancellation race: a job cancelled while it is
still queued must never start. The fix that makes this pass is to mark a job
active only when a pool thread actually takes it and re-check the cancellation
flag at that point.

**`test_e2e.py`** is the integration suite. It binds a real listener on an
ephemeral loopback port with isolated storage, drives it with the real client
transport, and runs real ffmpeg encodes. It asserts the whole chain: checksum on
upload, monotonic progress frames, verified download, and the failure paths
(bad handshake, version mismatch, refused encoder, unknown job, interrupted
transfer).

## Running a subset

```powershell
python -m pytest tests\test_protocol.py -q          # no external binaries needed
python -m pytest tests\test_e2e.py -q              # needs ffmpeg
python -m pytest tests -q -k "not gui"              # skip the Qt tests
python -m pytest tests\test_gui.py -q -s            # show the GUI log output
```

The Qt tests force `QT_QPA_PLATFORM=offscreen` in `tests/conftest.py`, so they
run over SSH and in CI without a desktop session.

## Determinism

* Loopback ports are ephemeral (`port=0`), so parallel or repeated runs never
  collide.
* Storage is redirected to `tmp_path` per test, and the original value is
  restored afterwards; no test touches `server/data`.
* Timings are never asserted to a tight bound. The GUI test asserts that a timer
  fired *at all* during a job, not how fast - a loaded CI box must not produce a
  red build for being slow.
* Clip generation goes through `make_clip`, which probes the available codecs and
  falls back (`libx264` -> `mpeg4`, `aac` -> `libvo_aacenc`), so the suite works
  on minimal FFmpeg builds.

## Manual verification

The automated suite cannot judge usability, so before submission:

1. `python server\daemon.py` on one machine, `python client\app.py` on another.
2. Connect, check the capability panel matches the worker's banner.
3. Measure latency, then submit a clip; watch progress and the log pane.
4. Open the output folder and play the file.
5. Submit a compute job; confirm the JSON metrics artifact downloads.
6. Cancel a long job mid-flight; confirm the worker keeps serving.
7. Kill the daemon mid-upload; confirm no partial asset survives a restart with
   `--prune-on-start`.
