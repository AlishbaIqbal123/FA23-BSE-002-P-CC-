# Architecture

## The shape of the system

```
        CLIENT  (laptop)                         WORKER  (GPU box)
  +---------------------------+            +---------------------------+
  | PyQt6 GUI                 |            | server/daemon.py          |
  |   MainWindow              |            |   Listener (accept loop)  |
  |     |                     |   TCP      |     |                     |
  |     | emits SessionWorker |  length-   |     v                     |
  |     | .invoke  (queued)   |  prefixed  |   Session (thread)        |
  |     v                     |  frames    |     |                     |
  | SessionWorker  (QThread)  | ---------> |     v                     |
  |   Transport               |            |   JobQueue -> JobPool     |
  |     upload / submit /     | <--------  |     |                     |
  |     await / download      |  progress  |     v                     |
  +---------------------------+  + logs    |   Engine                  |
                                        +   |     ffmpeg | compute     |
                                        +   +---------------------------+
                                        |             |
                                        +----------->  storage
                                                   data/jobs/<job_id>/
```

Two processes, one TCP port, no shared filesystem and no message broker. Every
byte the worker needs arrives over the socket.

## Processes and entry points

| Command | Role |
|---|---|
| `python server/daemon.py` | the worker: binds, accepts, queues, executes |
| `python client/app.py` | the client GUI (PyQt6, dark themed) |
| `python tools/bench.py` | local-versus-remote measurement harness |
| `python tools/probe_network.py` | latency / throughput probe between two machines |
| `python tools/make_sample_media.py` | builds the synthetic benchmark clips |
| `powershell -File tools/configure_network.ps1` | static IPs for a direct cable |

## Threading on the worker

Thread-per-connection, plus a fixed worker pool. The reasoning matters:

* **The accept loop owns nothing but `accept()`.** It never runs a job, never
  writes to a client socket and never waits on a queue. A client that stops
  reading, or a twenty-minute transcode, therefore cannot stop the next client
  from being served. This is the one hard requirement of the design.
* **Each session gets its own thread**, and inside it a **single writer thread**
  owns all socket writes. Progress and log frames are queued from the job thread
  and written by that one owner, which removes any need for a write lock and
  removes the possibility of two frames interleaving mid-header.
* **The job pool is bounded.** `--workers N` means at most N jobs execute at
  once, regardless of how many clients are connected. A job is marked active when
  a pool thread actually picks it up, not when it is queued, and cancellation is
  re-checked at that moment - so a job cancelled while queued never starts.
* **Shutdown is ordered**: stop accepting, close every session, stop the writer,
  join the pool with a timeout, then prune old job directories.

## Threading on the client

The GUI thread must never block, so every network call is a slot on a
`SessionWorker` that has been `moveToThread`-ed onto a `QThread`.

This is the sharp edge of the whole client, and it is worth stating plainly: a
plain Python call like `worker.do_submit(...)` executes **in the calling thread**.
`moveToThread` only affects queued signal/slot invocations, so the naive version
looks correct, passes every functional test, and freezes the window for the
whole upload. `MainWindow._call()` therefore never calls a worker method
directly - it emits `SessionWorker.invoke`, whose connection into the worker's
event loop is queued by construction. `tests/test_gui.py` proves the property by
running a real job and asserting that a 5 ms GUI timer keeps firing throughout.

## Module map

| Module | Responsibility |
|---|---|
| `common/protocol.py` | framing, frame types, send/receive, timeouts |
| `common/messages.py` | `JobSpec`, `Progress`, `JobResult`, `Capabilities`, validation |
| `common/checksum.py` | streaming SHA-256 |
| `common/paths.py` | shared constants and the data-root locations |
| `server/listener.py` | accept loop, connection accounting |
| `server/session.py` | per-connection state machine, upload, job, download |
| `server/job_queue.py` | queue, pool, cancellation, stats |
| `server/storage.py` | `.part` staging, checksum promotion, metadata, pruning |
| `server/capabilities.py` | ffmpeg/ffprobe/GPU/PyTorch probing |
| `server/engines/ffmpeg_engine.py` | transcode command construction and progress parsing |
| `server/engines/torch_engine.py` | Torch/NumPy compute jobs and JSON metrics output |
| `client/net/transport.py` | synchronous client: handshake, upload, submit, download |
| `client/net/session_worker.py` | Qt wrapper: blocking calls in, signals out |
| `client/ui/main_window.py` | the window, its widgets and the thread hand-off |
| `client/ui/log_terminal.py` | colour-coded, filterable log pane |
| `client/ui/theme.py` | dark stylesheet |

`PROTOCOL_VERSION` lives in `common/paths.py` even though it is a protocol
concern: both `common/messages.py` and `common/protocol.py` need it, and moving
it into either would create an import cycle between them.

## The upload and download paths

Uploads stream **raw bytes** between two JSON frames. There is no length-prefixed
framing inside the upload because there is nothing to frame per chunk - the
reader consumes exactly `UPLOAD_BEGIN.size` bytes and then expects `UPLOAD_END`.
It halves the syscalls compared to framing every block, and it means a truncated
upload is detected by a short read rather than by a checksum mismatch at the end.

The worker streams into `data/jobs/upload-<asset_id>/<name>.part`, hashes as it
writes, and only then promotes the file into the job's `input/` directory and
sends `UPLOAD_ACK`. A crash mid-upload therefore leaves a `.part` file that the
next start-up prunes, never a half-written asset that a later job might pick up.
Client-supplied names and identifiers are treated as hostile input, since both
become path components - anything that is not plain lowercase hex is replaced
before it reaches the filesystem.

Each job then lives in its own directory:

```
data/jobs/<job_id>/
    meta.json        job id, spec, status, timings, digests
    input/           the uploaded source
    output/          the encoded result (or metrics.json for compute jobs)
```

Downloads go the other way: `STREAM_BEGIN` announces the size and digest,
`DOWNLOAD_CHUNK` frames carry 256 KiB of payload, and `STREAM_END` closes it. The
client hashes while it writes, compares against the announced digest, and only
then renames the `.part` file into place. A download that fails verification is
discarded rather than reported as a success.

## Engine selection

`capabilities.py` probes the worker once at start-up and reports what is actually
there: FFmpeg version, hardware encoders, GPU name, CUDA device, PyTorch. The
client renders that list verbatim and **moves the encoder selection off any entry
the worker does not have** - a greyed-out combo box entry can still be the
current one, and a spec built from it is rejected by the worker. That is why the
first job from a fresh window works on a CPU-only machine instead of failing with
`encoder h264_nvenc is not in this build of ffmpeg`.

The FFmpeg engine falls back in a fixed order (`h264_nvenc` -> `libx264` ->
`mpeg4`) and refuses a job outright if none of them exists, rather than silently
substituting something the user did not ask for.

## Failure handling

| Situation | Behaviour |
|---|---|
| Client disconnects mid-upload | session notices, deletes the `.part` file, closes |
| Job fails | `RESULT` with `status != ok` and the ffmpeg stderr tail; pool continues |
| Job cancelled | pool marks it cancelled, the subprocess is terminated |
| Worker stalls | heartbeat every 2 s; a session with no progress for 90 s is dropped |
| Oversized frame | rejected by `MAX_FRAME_PAYLOAD` before allocation |
| Version mismatch | `ERROR` frame explaining it, then the socket closes |
| ffmpeg missing | capability probe reports it, transcode jobs are rejected with a clear message |

A failure in one job never takes down the daemon: the session continues to serve
the next request on the same connection, and the pool thread returns to the queue.

## Watch Folder Automation Pipeline (`client/watcher.py`)

To eliminate manual file picking for content creators using video editing suites (such as Microsoft Clipchamp, Adobe Premiere Pro, or CapCut), the client includes an automated watch folder pipeline:

1. **Non-blocking Polling**: A `QTimer`-backed `FolderWatcher` inspects the designated watch directory (defaulting to `%USERPROFILE%\Downloads`) every 1.0 s on the GUI event loop without thread locking.
2. **Atomic Write Stabilization (Debounce)**: Video editors stream chunks directly to disk while rendering, causing file sizes to change continuously. The watcher monitors `(file_size, mtime)` across consecutive polling cycles. Only when the file size and modification timestamp remain completely stable across debounce intervals (and temporary `.part` or `.crdownload` extensions disappear) is the export deemed final and ready.
3. **Automatic Queue Dispatch**: The stabilized file path and byte size trigger `video_detected(path, size)`, feeding directly into the client's execution queue.
4. **End-to-End Hands-Off Cycle**:
   - Source export is detected in the watch folder.
   - Client computes streaming SHA-256 and streams the raw draft over LAN to the GPU worker.
   - Remote worker executes high-speed hardware transcode / compute with live stdout progress streaming.
   - Rendered master is downloaded back to the client's output directory with cryptographic checksum verification.

## Zero-Config LAN Discovery (`common/discovery.py`)

Rather than requiring manual IP discovery via `ipconfig` or router DHCP leases, the system includes UDP broadcast auto-discovery:

* **UDP Port 7576**: The daemon binds or listens on UDP `7576`.
* **Probe / Beacon Exchange**: The client emits a lightweight UDP broadcast packet (`RDO_DISCOVERY_PING_V1`) to `255.255.255.255:7576` and local subnet broadcast addresses.
* **Service Announcement**: Active workers respond with `RDO_DISCOVERY_PONG_V1:<json>` containing their hostname, listening IP, TCP service port (7575), and probed capabilities.
* **Fallback & Auto-Populate**: If auto-discovery encounters restrictive firewalls or isolated subnets, the GUI displays all local network interfaces and provides direct IP input with live RTT latency probing.
