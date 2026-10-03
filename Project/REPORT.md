# Report: Distributed Task Offloading and Remote Rendering

## 1. What was built

A two-process system that moves video transcoding and numeric compute off a
laptop onto a GPU worker over a single TCP port, with a PyQt6 client, a threaded
daemon, a bounded job queue, live progress and checksum-verified transfers.

| | |
|---|---|
| Language / runtime | Python 3.13, PyQt6 6.11, NumPy, optional PyTorch |
| Transport | raw TCP, 1-byte type + 4-byte big-endian length + payload, port 7575 |
| Worker concurrency | accept loop + one thread per session + one writer thread per session + bounded job pool |
| Client concurrency | GUI thread + one `QThread` worker; all network I/O on the worker |
| Code size | ~6,090 lines across 40 Python files |
| Tests | 184, all passing, none requiring a GPU |
| Documentation | `docs/` (PRD, architecture, protocol, network, testing), `README.md`, `benchmarks/RESULTS.md` |

## 2. Requirements coverage

| Requirement | Where it lives | Evidence |
|---|---|---|
| GUI client with job submission and progress | `client/ui/main_window.py` | `tests/test_gui.py` (9 tests, headless Qt) |
| Non-blocking GUI under load | `client/net/session_worker.py` | GUI-timer assertion during a real encode |
| Capability negotiation | `server/capabilities.py`, `common/messages.py` | `tests/test_capabilities.py`, `tests/test_e2e.py` |
| File transfer with checksum | `client/net/transport.py`, `server/storage.py` | `tests/test_checksum.py`, e2e digest assertions |
| Job queue | `server/job_queue.py` | `tests/test_job_queue.py` (12 tests) |
| GPU acceleration with fallback | `server/engines/ffmpeg_engine.py`, `torch_engine.py` | `tests/test_ffmpeg_engine.py` (39 tests) |
| Structured logging | `server/*.py` + `client/ui/log_terminal.py` | console log with per-connection tags |
| Measurement | `tools/bench.py` | `benchmarks/RESULTS.md` |

## 3. Design decisions and why

### 3.1 The accept loop owns nothing but `accept()`

Each connection gets its own thread, and inside it a single writer thread owns
all socket writes. The listener thread never runs a job, never writes to a client
socket and never blocks on a queue.

This is the one non-negotiable property: a client that stops reading, or a
twenty-minute transcode, must not stop the next client from being served. It is
also why progress and log frames are *queued* to the writer rather than written
directly - that removes the write lock and makes frame interleaving impossible.

### 3.2 Uploads stream raw bytes; downloads are framed

The upload payload between `UPLOAD_BEGIN` and `UPLOAD_END` is a raw byte run,
not a sequence of frames. There is nothing per-chunk to describe, so framing
would only add a header and a syscall. The worker consumes exactly `size` bytes
with `recv_exact`, which also means a truncated transfer surfaces as a short
read rather than a checksum mismatch at the very end.

Downloads go the other way and *are* framed, because the worker pushes
unsolicited: `STREAM_BEGIN`, then 256 KiB binary chunks, then `STREAM_END`.

Both directions stage through a `.part` file and hash while writing, promoting
to the final name only after the digest matches. Nothing partial is ever
mistaken for something complete.

### 3.3 Jobs are marked active when a thread takes them, not when queued

The first version marked a job active at submit time. A cancellation that
arrived while the job sat in the queue therefore raced with the worker thread
picking it up, and an ffmpeg process could start after the user had cancelled.
Now the flag is set inside the pool thread, and cancellation is re-checked there
before any work begins. `tests/test_job_queue.py` exercises the race directly.

### 3.4 The GUI never calls a worker method directly

`moveToThread` only affects queued signal/slot invocations. Writing
`worker.do_submit(...)` - which is what the code did at first - executes in the
GUI thread, because a plain Python call has no idea Qt exists. Every functional
test still passed; the window simply froze for the duration of the upload.

`MainWindow._call()` therefore emits `SessionWorker.invoke`, whose connection
into the worker's event loop is queued by construction, and `tests/test_gui.py`
proves the property by running a real encode and asserting that a 5 ms timer kept
firing. This is the defect most likely to be reintroduced, so it has a named
test.

### 3.5 The encoder selection follows the worker's capabilities

Capability probing greys out encoders the worker does not have - but a disabled
combo box entry can still be the *current* one. The first job submitted from a
fresh window on a CPU-only machine was therefore rejected with
`encoder h264_nvenc is not in this build of ffmpeg`. The client now moves the
selection to something usable as soon as the handshake lands, and submitting is
disabled until then, because the job spec depends on what the worker supports.

### 3.6 Old FFmpeg builds are supported, not rejected

The binary available during development was a 2013 build
(`N-55702-g920046a`) with no `-hide_banner`, no NVENC, no `-2` scale argument, and
the AAC encoder named `libvo_aacenc`. Rather than demanding a modern build, the
code adapts: capability probing retries without `-hide_banner`, output
dimensions come from ffprobe and are computed explicitly (aspect preserving,
even, never upscaled), and the audio encoder falls back when the strict
experimental path is unavailable. A machine with nothing but `mpeg4` still
transcodes.

### 3.7 Untrusted client input never reaches the filesystem unchecked

Client-supplied `asset_id` is honoured only when it is at most 64 characters of
lowercase hex; anything else is replaced with a fresh uuid, so `../../etc/passwd`
cannot escape the data root. Filenames go through `safe_filename()`, which keeps
the basename, replaces unsafe characters, strips leading dots and truncates.
Uploads above 8 GiB are refused before any byte is accepted.

### 3.8 The benchmark reports three numbers, not one

`tools/bench.py` measures the same encode locally and remotely and separates the
remote time into upload, compute and download. It reports end-to-end speedup,
compute-only speedup and network overhead. Quoting the compute-only figure alone
would hide the transfer cost that the whole design pays, so the report leads
with the end-to-end number.

## 4. Measured results

Full table, environment and analysis: `benchmarks/RESULTS.md`. Raw data:
`benchmarks/results-cpu-loopback.json`.

Environment: Windows 11, 8 cores, **no GPU**, FFmpeg `N-55702-g920046a` (CPU
encoders only), no PyTorch, client and worker on the same machine over loopback
(RTT 1.16 ms average, 0.39 ms jitter).

| Figure | Value |
|---|---|
| Mean end-to-end speedup | **0.83x** |
| Mean compute-only speedup | 0.85x |
| Mean network overhead | 3.0% |
| Jobs completed | 12 of 12 |
| Checksums verified | 12 of 12 |
| Progress frames observed | 147, monotonic in every run (3-28 per job) |

**Interpretation.** Offloading is a *loss* in this configuration, and that is the
correct result rather than a defect: the worker has no GPU, so the remote encode
is a second copy of the same software encode on the same eight cores. There is
no faster machine to move the work to, and the queue, the protocol and the disk
write are pure additions.

Run-to-run variance is comparable to the effect being measured - the same 4K
clip takes 14.28 s and 15.10 s locally, and the short clips vary proportionally
more - because client and worker are competing ffmpeg processes for the same
cores. Only the 4K clip is long enough for that noise to average out, and even it
reaches just 1.06x while running the *same* software encoder. This is why every
figure comes from repeated runs and aggregates rather than a single timing.

Network overhead scales with payload as expected: 7.0% for the 57 MiB clip versus
under 2% for sub-MiB clips. Loopback never leaves the kernel, so these are lower
bounds; the same clip would spend roughly 0.9 s per direction on 1 Gbit/s and
about 9 s on 100 Mbit/s.

**Not measured.** No NVENC or CUDA figure is reported. No NVIDIA GPU and no
modern FFmpeg build were available, and estimating the number would defeat the
purpose of measuring it. `benchmarks/RESULTS.md` contains the exact commands and
an empty table for the lab run, plus the shape the result is expected to take.

## 5. Limitations and honest gaps

* **No GPU validation.** The NVENC path, the CUDA compute path and the GPU
  benchmarks are implemented and unit-tested at the command-construction level,
  but have not been executed against real hardware. This is the largest gap.
* **No PyTorch validation.** The NumPy fallback is tested; the Torch path is
  exercised only where it can be imported.
* **No authentication or TLS.** Anyone who may connect may submit work. The
  allow-list is the only boundary. Fine for a lab LAN, not for anything else.
* **The client drives one job at a time.** The worker will accept several jobs on
  one connection and every `RESULT` carries its `job_id`, but results can arrive
  out of submission order when jobs finish in a different order, so a client that
  pipelines jobs must demultiplex on `job_id`. The GUI deliberately does not.
* **No resumable transfers.** An interrupted upload restarts from zero. The
  `.part` staging guarantees correctness, not resumption.
* **Benchmarks used synthetic clips.** They are noise-injected test patterns,
  not real footage. Absolute times will differ with real content; the ratios are
  the meaningful part.
* **Single measurement machine.** All numbers come from one Windows box, so they
  carry that machine's scheduler noise.

## 6. How to verify this submission

```powershell
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q                       # 184 passed

python server\daemon.py                         # worker
python client\app.py --host 127.0.0.1           # client, in another shell

python tools\make_sample_media.py --all
python tools\bench.py --host 127.0.0.1 --all --repeat 2
```

The end-to-end suite is the part to read first: it binds a real listener, uses
the real client transport, runs real ffmpeg encodes and asserts the checksums,
the progress monotonicity and the error paths.
