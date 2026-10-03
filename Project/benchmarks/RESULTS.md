# Benchmark results

Everything in this file was produced by `tools/bench.py` on the machine
described below. The raw JSON is `benchmarks/results-cpu-loopback.json`, the
generated table is `benchmarks/results-cpu-loopback.md`, and the media is
reproducible with `tools/make_sample_media.py`.

## What is measured, and why it is split three ways

For each clip the harness runs the **same** encode twice - once as a plain local
ffmpeg invocation, once through the worker over TCP - and separates the remote
wall-clock time into its three phases:

```
t_remote_total = t_upload + t_compute + t_download
```

| Figure | Definition | What it tells you |
|---|---|---|
| Speedup (end-to-end) | `t_local / t_remote_total` | what a user actually feels |
| Compute-only speedup | `t_local / t_compute` | the accelerator, with the network removed |
| Network overhead | `100 x (t_upload + t_download) / t_remote_total` | the price of going remote |

Quoting only the compute-only figure is the dishonest option - it hides the
transfer cost that the whole design has to pay - so the report leads with the
end-to-end number.

## Environment

| | |
|---|---|
| Date | 2026-10-04 02:48 |
| Machine | DESKTOP-VFM7JG0, Windows 11, 8 logical cores |
| Client and worker | **the same machine** - loopback `127.0.0.1:7575` |
| GPU | none detected, `nvenc_available = false` |
| FFmpeg | `N-55702-g920046a` (2013 build, CPU encoders only: `libx264`, `mpeg4`, `aac`, `libopus`) |
| PyTorch | not installed - compute jobs ran on the NumPy fallback |
| Round-trip latency | 1.16 ms average, 0.39 ms jitter, 25 samples |

## Measured results - CPU worker on loopback

Encoder `libx264`, preset `veryfast`, CRF 23, audio `aac`, two runs per clip.

| Clip | Size | Local (s) | Upload (s) | Worker (s) | Download (s) | Remote total (s) | Speedup | Compute-only | Net overhead |
|---|---|---|---|---|---|---|---|---|---|
| sample-1080p-10s-hq #1 | 57.4 MiB | 14.57 | 0.62 | 16.25 | 0.61 | 17.48 | **0.83x** | 0.90x | 7.0% |
| sample-1080p-10s-hq #2 | 57.4 MiB | 13.16 | 0.43 | 17.02 | 0.69 | 18.14 | **0.73x** | 0.77x | 6.2% |
| sample-1080p-10s #1 | 0.4 MiB | 5.06 | 0.03 | 6.56 | 0.03 | 6.62 | **0.77x** | 0.77x | 0.9% |
| sample-1080p-10s #2 | 0.4 MiB | 5.05 | 0.03 | 4.51 | 0.02 | 4.56 | **1.11x** | 1.12x | 1.2% |
| sample-360p-10s #1 | 0.2 MiB | 0.94 | 0.05 | 1.08 | 0.02 | 1.15 | **0.81x** | 0.87x | 5.9% |
| sample-360p-10s #2 | 0.2 MiB | 0.92 | 0.02 | 1.09 | 0.01 | 1.12 | **0.82x** | 0.84x | 2.9% |
| sample-360p-4s #1 | 0.1 MiB | 0.75 | 0.03 | 1.06 | 0.01 | 1.11 | **0.68x** | 0.71x | 4.0% |
| sample-360p-4s #2 | 0.1 MiB | 0.56 | 0.01 | 0.95 | 0.01 | 0.98 | **0.58x** | 0.59x | 2.5% |
| sample-4k-10s #1 | 0.7 MiB | 14.28 | 0.03 | 13.34 | 0.04 | 13.41 | **1.06x** | 1.07x | 0.5% |
| sample-4k-10s #2 | 0.7 MiB | 15.10 | 0.05 | 14.52 | 0.03 | 14.60 | **1.03x** | 1.04x | 0.5% |
| sample-720p-10s #1 | 0.3 MiB | 2.49 | 0.04 | 2.90 | 0.03 | 2.96 | **0.84x** | 0.86x | 2.2% |
| sample-720p-10s #2 | 0.3 MiB | 2.14 | 0.03 | 3.15 | 0.03 | 3.20 | **0.67x** | 0.68x | 1.8% |

**Mean end-to-end speedup 0.83x, mean compute-only 0.85x, mean network overhead
3.0%.** All 12 jobs completed, every downloaded output matched the worker's
SHA-256, and all 147 `PROGRESS` frames observed were monotonic (3-28 per job,
scaling with clip length).

## What these numbers actually say

**1. Offloading is a loss in this configuration, and that is the correct result.**
The mean is clearly *below* 1.0x. The worker has no GPU, so "remote compute" is a
second copy of the same `libx264` software encode running on the same eight
cores. There is no faster machine to move the work to, and the queue, the
protocol and the disk write are pure additions. A speedup above 1.0x here would
mean the measurement was wrong.

**2. Run-to-run variance is comparable to the effect being measured.** The 57 MiB
clip encodes locally in 14.57 s and 13.16 s on consecutive runs, and the 4K clip
in 14.28 s and 15.10 s; the short clips vary proportionally more (0.75 s versus
0.56 s for a 4-second 360p clip). Both the client and the worker are ffmpeg
processes competing for eight cores, so each takes whatever is left. Only the 4K
clip - long enough for scheduler noise to average out - lands consistently above
1.0x (1.06x and 1.03x), and even that is the same encoder on the same machine
rather than an accelerator. This is why every claim here comes from repeated runs
and aggregates, never a single timing.

**3. Network overhead is small but it is not free, and it scales with size.**
The 57 MiB clip spends 1.23 s of a 17.48 s job (7.0%) moving bytes, against under
2% for sub-MiB clips. Loopback hides the real cost: it never leaves the kernel,
so these figures are a **lower bound**. The same clip over 1 Gbit/s Ethernet
would spend roughly 0.9 s in each direction; over 100 Mbit/s, about 9 s each way,
which would dominate the encode entirely.

**4. Progress reporting and integrity work over a real socket.** All 12 runs
reported monotonic `PROGRESS` frames (3-28 per job, scaling with clip length) and
a verified checksum on the downloaded output, so the streaming path is not just
theoretically correct.

## GPU measurements - to be completed on the lab machine

No NVIDIA GPU and no modern FFmpeg build were available on the measurement
machine, so **no NVENC or CUDA number is reported here**. Rather than estimate
one, the table is left for the lab run. Reproduce with:

```powershell
# on the GPU worker
python server/daemon.py --host 0.0.0.0 --port 7575 --workers 2

# on the client, once the two machines can see each other
python tools\make_sample_media.py --all
python tools\make_sample_media.py --preset 1080p --seconds 10 --hq
python tools\probe_network.py --host 192.168.1.1 --port 7600 --listen      # worker
python tools\probe_network.py --host 192.168.1.1 --port 7600              # client
python tools\bench.py --host 192.168.1.1 --port 7575 --all --repeat 3 `
    --out benchmarks\results-gpu-lan.json --markdown benchmarks\results-gpu-lan.md
```

| Clip | Size | Local CPU (s) | Upload (s) | GPU NVENC (s) | Download (s) | Remote total (s) | Speedup | GPU-only | Net overhead |
|---|---|---|---|---|---|---|---|---|---|
| _pending lab run_ | | | | | | | | | |

`--encoder h264_nvenc` forces the hardware path if the automatic choice picks
something else, and the harness prints the encoder actually used by the worker so
the table cannot silently claim NVENC when `libx264` ran.

### Expected shape of the result

Recorded as a prediction to be checked, not as a measurement: the remote
`compute_seconds` should fall by roughly an order of magnitude for the 1080p and
4K clips, while `t_upload + t_download` stays fixed. The end-to-end speedup will
land somewhere below the compute-only figure by exactly the network overhead
percentage, and for the 57 MiB clip the transfer will start to dominate. That
trade-off - large wins on compute, a fixed byte cost per job - is the reason the
client measures and reports RTT before a job is submitted.
