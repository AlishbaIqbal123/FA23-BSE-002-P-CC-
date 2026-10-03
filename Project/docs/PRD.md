# Product requirements

## Problem

Video encoding and numeric compute are the two workloads most likely to outlive a
laptop: a 4K transcode runs for minutes, and a neural-network-shaped matrix job
wants a GPU that a portable machine does not have. Both are also perfectly
divisible - they can be described as a job plus its input, sent somewhere else,
and collected when they finish.

The goal is a system that does exactly that over a plain LAN, with a graphical
client, and without pretending that offloading is free.

## Goals

| # | Goal | How it is verified |
|---|---|---|
| G1 | Offload a transcode to a remote worker over TCP | `tests/test_e2e.py` runs a real encode end to end |
| G2 | Report progress while it happens | monotonic `PROGRESS` frames asserted in the e2e suite and the benchmark |
| G3 | Return the result and prove it is intact | SHA-256 verified on both sides, asserted per job |
| G4 | Degrade gracefully when there is no GPU | CPU worker falls back to `libx264`; every job still succeeds |
| G5 | Stay responsive under load | bounded worker pool; GUI thread never blocks (asserted by `tests/test_gui.py`) |
| G6 | Measure honestly | `tools/bench.py` reports end-to-end *and* compute-only speedup |
| G7 | Be operable by someone who did not write it | one command per side, banner showing capabilities, `docs/NETWORK.md` |

## Non-goals

* **Not a cluster scheduler.** One worker, one bounded pool. No work stealing, no
  replication, no failover.
* **Not a media player or editor.** The client hands a file to a remote encoder
  and shows the result; it does not preview or trim.
* **Not a distributed filesystem.** Job directories live on the worker; the
  client keeps only what it downloads.
* **No invented hardware numbers.** Results that were not measured are marked
  pending, not estimated.

## Users

| User | Need |
|---|---|
| Student, single laptop | submit a clip from a GUI, watch it encode on the lab GPU box |
| Lab demonstrator | show the queue, the capability negotiation and the byte counts working |
| Assessor | read `REPORT.md`, run `pytest`, and see the measured table |

## Functional requirements

### FR1 Connection and capability negotiation

* The client connects to `host:port` (default port 7575) and performs a `HELLO`
  handshake before anything else.
* A version mismatch is refused with a message naming both versions.
* After a successful handshake the client displays the worker's FFmpeg build,
  GPU, NVENC/CUDA availability, PyTorch availability, encoder list, engine list
  and CPU count.
* The encoder selector must never leave an unusable encoder selected; if the
  worker offers no hardware encoder, the selection moves to `libx264`
  automatically.

### FR2 Job submission

* The user picks a file (file dialog or drag and drop) and the settings for it:
  encoder, preset, output resolution, CRF *or* target bitrate, audio codec and
  bitrate, output name.
* The spec is validated client-side before anything is sent, with readable
  messages ("NVENC preset must be one of ...").
* A missing or unreadable input file is reported before any network traffic.
* Submitting is impossible until a handshake has completed, because the spec
  depends on what the worker supports.

### FR3 Upload

* The file is streamed to the worker with SHA-256 computed on both sides.
* Progress is reported continuously; the UI shows a determinate progress bar.
* An interrupted transfer leaves no usable partial asset.

### FR4 Execution

* The worker queues the job and, when a worker thread is free, runs it.
* Progress is streamed at most every 120 ms and never moves backwards.
* Engine selection: `h264_nvenc` when available, otherwise `libx264`, otherwise
  `mpeg4`; if none exists the job is rejected with a clear message rather than
  silently substituting an encoder.

### FR5 Compute jobs

* Four operations - `matmul`, `conv2d`, `transformer_ffn`, `elementwise` - with a
  configurable matrix size and iteration count.
* Torch with CUDA when available, NumPy otherwise; the fallback is automatic and
  visible in the result (`device`, `backend`).
* Results are written as a downloadable JSON metrics artifact, not just logged.

### FR6 Cancellation

* The client can cancel the running job; the subprocess is terminated and the
  queue slot released.
* A job cancelled while still queued never starts.

### FR7 Download

* The result is streamed back in 256 KiB chunks, hashed while written, and only
  promoted from `.part` to its final name after the digest matches.
* The client can open the output folder directly.

### FR8 Latency measurement

* The client can measure RTT (min/avg/max, jitter) before submitting, so a user
  can see whether offloading is worth it for the job at hand.

### FR9 Benchmarking

* `tools/bench.py` runs the same encode locally and remotely and reports
  end-to-end speedup, compute-only speedup and network overhead separately.

## Non-functional requirements

| # | Requirement | Result |
|---|---|---|
| N1 | A stalled or half-open client must not wedge the worker | accept loop is independent; 90 s stall timeout |
| N2 | Memory must stay bounded regardless of file size | 256 KiB chunks, 1 MiB frame cap, no whole-file reads |
| N3 | The GUI must stay responsive during a multi-hundred-MB transfer | worker thread + asserted in `tests/test_gui.py` |
| N4 | Work on a machine with no GPU, no CUDA and no PyTorch | NumPy and CPU encoder fallbacks |
| N5 | Work with an old FFmpeg build | no `-hide_banner`, no `-2` scale, `aac`/`libvo_aacenc` handling |
| N6 | Untrusted client input must not escape the data directory | identifier and filename sanitisation |
| N7 | Restartable | `.part` staging, `meta.json` per job, `--prune-on-start`, `--keep-jobs` |

## Acceptance criteria

The submission is complete when:

1. `python -m pytest tests -q` passes on a machine with no GPU (currently 184
   tests).
2. `python server/daemon.py` followed by `python client/app.py` lets a user
   connect, submit a clip and receive a verified output without touching the
   console.
3. `python tools/bench.py --all` produces a table of measured numbers.
4. Every document in `docs/` describes the system as built, and results that
   were not measured are labelled pending.
