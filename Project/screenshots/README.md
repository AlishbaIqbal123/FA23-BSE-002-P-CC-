# Screenshots & Visual Walkthrough

Captured directly from the running application on Windows 11.

The suite captures the complete lifecycle of remote execution—from server daemon discovery, client capability negotiation, active streaming encoding, to SHA-256 verified download and tensor computation.

---

## Gallery Index

| State | Preview File | Description |
|---|---|---|
| **01. Worker Daemon** | [`01-startup-server.png`](01-startup-server.png) | Headless/threaded worker daemon running on port 7575 with hardware detection |
| **02. Connected Client** | [`02-connected-capabilities.png`](02-connected-capabilities.png) | Modern PyQt6 client connected, latency probed (RTT/jitter), video asset loaded |
| **03. Job In Progress** | [`03-job-running.png`](03-job-running.png) | Live dual-stream progress: 100% upload, active remote FFmpeg transcode |
| **04. Job Complete** | [`04-job-complete.png`](04-job-complete.png) | 100% download received, SHA-256 integrity verified, output file saved |
| **05. Tensor Compute** | [`05-compute-tab.png`](05-compute-tab.png) | Matrix multiplication and tensor kernels offloaded via CUDA/NumPy |
| **06. Log Terminal** | [`06-log-console.png`](06-log-console.png) | Real-time, severity-highlighted diagnostics and protocol tracing |

---

## 1. Worker Daemon Initialization
Worker daemon running in terminal mode on `0.0.0.0:7575`. Hardware probes detect available CPU cores, FFmpeg build capabilities, hardware acceleration engines (NVENC, CUDA, PyTorch), and authorized subnets before launching the session accept loop.

![Worker Daemon Initialization](01-startup-server.png)

---

## 2. Client GUI & Capability Negotiation
The client interface immediately connects to the worker, performing handshake negotiation to fetch remote hardware profiles. Network ping and jitter are monitored in real time, and the video asset is analyzed with clip badges and duration metadata.

![Client GUI & Capability Negotiation](02-connected-capabilities.png)

---

## 3. Active Offload & Live Progress Streaming
During active transcoding, the client tracks both transmission and execution concurrently. Monotonic progress updates stream back from the daemon at regular intervals, displaying elapsed time, throughput, and encoding rate.

![Active Offload & Live Progress Streaming](03-job-running.png)

---

## 4. Completed Job & SHA-256 Verification
Once remote rendering finishes, framed chunks stream back to the client. The client reassembles the binary payload into local storage, computes an end-to-end SHA-256 checksum to ensure byte-perfect fidelity, and updates the status to Done.

![Completed Job & SHA-256 Verification](04-job-complete.png)

---

## 5. Tensor Compute Acceleration (CUDA / NumPy)
Dedicated numeric compute tab allowing parallel matrix multiplications (`matmul`), 2D convolutions (`conv2d`), and transformer feed-forward passes (`transformer_ffn`) to be offloaded and timed across network nodes.

![Tensor Compute Acceleration](05-compute-tab.png)

---

## 6. Real-Time Structured Log Console
Embedded log terminal providing ANSI-colored, level-coded diagnostic tracing (`OK`, `INFO`, `WARN`, `ERROR`) for connection handshakes, framed packet exchanges, and local socket state transitions.

![Real-Time Structured Log Console](06-log-console.png)
