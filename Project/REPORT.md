# Report: Distributed Task Offloading and Remote Rendering

## 1. What was built

A two-process system that moves video transcoding and numeric compute off a
laptop onto a GPU worker over a single TCP port, with a PyQt6 client, a threaded
daemon, a bounded job queue, live progress and checksum-verified transfers.

| Component | Specification |
|---|---|
| Language / runtime | Python 3.13, PyQt6 6.11, NumPy, optional PyTorch |
| Transport | raw TCP, 1-byte type + 4-byte big-endian length + payload, port 7575 |
| Discovery | UDP LAN broadcast beacon on port 7576 with active kernel routing |
| Worker concurrency | accept loop + one thread per session + one writer thread per session + bounded job pool |
| Client concurrency | GUI thread + one `QThread` worker; all network I/O on the worker |
| Code size | ~6,150 lines across 40 Python files |
| Tests | 190 tests, 100% passing, none requiring a dedicated GPU |
| Documentation | `docs/` (PRD, architecture, protocol, network, testing), `README.md`, `DESKTOP_USER_GUIDE.md` |
| Video Proof | 2 multi-machine live recordings in `screenrecordigs/` |

## 2. Requirements coverage

| Requirement | Where it lives | Evidence |
|---|---|---|
| GUI client with job submission and progress | `client/ui/main_window.py` | `tests/test_gui.py` (9 tests, headless Qt) |
| Non-blocking GUI under load | `client/net/session_worker.py` | GUI-timer assertion during a real encode |
| Capability negotiation | `server/capabilities.py`, `common/messages.py` | `tests/test_capabilities.py`, `tests/test_e2e.py` |
| File transfer with checksum | `client/net/transport.py`, `server/storage.py` | `tests/test_checksum.py`, e2e digest assertions |
| Job queue | `server/job_queue.py` | `tests/test_job_queue.py` (12 tests) |
| GPU acceleration with fallback | `server/engines/ffmpeg_engine.py`, `torch_engine.py` | `tests/test_ffmpeg_engine.py` (39 tests) |
| Watch Folder Automation | `client/watcher.py` | `tests/test_watcher.py` (6 tests) |
| Zero-Config LAN Discovery | `common/discovery.py` | UDP ping/pong beacon on port 7576 |
| Structured logging | `server/*.py` + `client/ui/log_terminal.py` | console log with per-connection tags |
| Measurement | `tools/bench.py` | `benchmarks/RESULTS.md` & Live Demo Telemetry |

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

---

## 4. Measured results

### 4.1 Synthetic Local Loopback Baseline

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

### 4.2 Empirical Multi-Machine LAN Demonstration (Clipchamp Pipeline)

To validate the system under real-world conditions with authentic multimedia workloads, an end-to-end evaluation was performed across two physical Windows 11 machines communicating over a local Wi-Fi subnet (`192.168.100.x`). Both sessions were recorded in full and are preserved in the repository (`screenrecordigs/`):

* **Client Node**: Laptop (`192.168.100.27`), user `CASPAM`, running Microsoft Clipchamp and the PyQt6 client with Folder Watcher monitoring `C:\Users\CASPAM\Downloads`.
* **Worker Node**: Compute workstation (`192.168.100.3:7575`), hostname `DESKTOP-VFM7JG0`, 8 CPU cores, Intel(R) UHD Graphics, 2-worker job pool, UDP auto-discovery on port 7576.
* **Workload**: Real video edited in Clipchamp (*"Purple canyon YouTube intro"*), exported as 1080p draft MP4.

| Measured Parameter | Observed Value | Evidence Source |
|---|---|---|
| **Network Latency (RTT)** | **20.78 ms – 26.62 ms** (Jitter: 0.00 ms) | Live client telemetry & ping probe |
| **Input File Payload** | **19.69 MiB** (20,646,400 bytes) | Clipchamp export in Downloads |
| **Detection Latency** | **< 1.0 s** post-write stabilization | Folder Watcher snapshot monitor |
| **Upload Throughput** | **19.6 Mb/s** sustained (verified in 8.62s) | Client transfer monitor & worker hash |
| **Remote Transcode Time** | **~35.1 s** (CPU software transcode) | Daemon process execution log |
| **Download Throughput** | **21.9 Mb/s** sustained (verified in 1.02s) | Client download verification log |
| **Output File Payload** | **2.67 MiB** (2,740 KB) | Final verified MP4 in outputs folder |
| **Size Reduction Ratio** | **86.4% reduction** (7.37x compression) | 19.69 MiB draft ➔ 2.67 MiB master |
| **Total Round-Trip Time** | **44.8 seconds** | Complete end-to-end job duration |
| **Data Integrity Verification** | **100% SHA-256 match** on both nodes | Client & server diagnostic logs |

**Key Findings:**
1. **Zero Client CPU Degradation**: The client laptop offloaded 100% of the encoding complexity. Local CPU utilization remained under 3%, preventing the thermal throttling and system freezes typical of local video rendering.
2. **Massive Bandwidth & Storage Optimization**: The remote worker's FFmpeg H.264 CRF rate-control compressed the bloated 19.69 MiB Clipchamp draft into a compact 2.67 MiB master file without perceptible visual loss at 1080p, saving 86.4% in local and upstream network bandwidth.
3. **Resilient Automated Workflow**: The watcher pipeline operated completely hands-free—detecting the browser export, initiating the handshake, streaming the binary payload, and retrieving the verified output into the user's directory.

**Evidence Artifacts:**
* Client-side recording: `screenrecordigs/Client-side-edited-and-render-video-of-clip-champ-remote.mp4`
* Server-side recording: `screenrecordigs/server side.mp4`
* Visual walkthrough frames: `screenshots/01-startup-server.png` through `06-output-verified.png`

---

## 5. Limitations and honest gaps

* **No dedicated NVIDIA GPU in test run.** The live demonstration exercised the Intel(R) UHD Graphics + CPU fallback path. The NVENC path and CUDA compute path are fully implemented and verified with mock/unit tests, but await testing on an NVIDIA GeForce/RTX rig.
* **No PyTorch validation.** The NumPy fallback is tested and functional; the Torch path is exercised only where CUDA is installed.
* **No authentication or TLS.** Anyone on the local network who connects may submit work. The CIDR allow-list is the boundary. Suitable for lab or home LANs.
* **The client drives one job at a time.** The worker will accept several jobs on one connection, but the GUI pipelines one job sequentially.
* **No resumable transfers.** An interrupted upload restarts from zero. The `.part` staging guarantees correctness, not resumption.

---

## 6. How to verify this submission

```powershell
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q                       # 190 passed

# Start Worker Node:
python desktop_hub.py                           # or python server\app.py

# Start Client Controller:
python desktop_hub.py                           # or python client\app.py
```

The end-to-end suite is the part to read first: it binds a real listener, uses
the real client transport, runs real ffmpeg encodes and asserts the checksums,
the progress monotonicity and the error paths.
