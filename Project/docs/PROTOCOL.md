# Wire protocol

One TCP connection carries the entire session. Every message is a frame:

```
+--------+------------------+--------------------------+
| type   | length           | payload                  |
| 1 byte | 4 bytes (BE)     | length bytes             |
+--------+------------------+--------------------------+
```

`length` is unsigned big-endian and capped at `MAX_FRAME_PAYLOAD` (1 MiB). A
frame claiming more is a protocol error, checked *before* any buffer is
allocated, so a hostile or corrupt peer cannot make the worker reserve 4 GiB.

Only `DOWNLOAD_CHUNK` carries opaque bytes. Every other frame type carries a
UTF-8 JSON object, and `common/protocol.py` enforces that split so a caller
cannot accidentally `json.loads` a video chunk.

## Frame types

| Code | Name | Direction | Payload |
|---|---|---|---|
| `0x01` | `HELLO` | client -> worker | client identity, protocol version |
| `0x02` | `HELLO_ACK` | worker -> client | worker id, capabilities |
| `0x03` | `PING` | both | nonce, client timestamp |
| `0x04` | `PONG` | both | echoed nonce, worker timestamp |
| `0x15` | `PING_BATCH` | client -> worker | `{count}` - latency batch request |
| `0x05` | `UPLOAD_BEGIN` | client -> worker | name, size, sha256 |
| `0x06` | `UPLOAD_ACK` | worker -> client | asset id, stored bytes, digest |
| `0x07` | `UPLOAD_END` | client -> worker | asset id, sha256 |
| `0x08` | `UPLOAD_DONE` | worker -> client | asset id |
| `0x09` | `JOB_SUBMIT` | client -> worker | `JobSpec` |
| `0x0A` | `JOB_ACK` | worker -> client | job id, queue position |
| `0x0B` | `JOB_REJECT` | worker -> client | reason |
| `0x0C` | `PROGRESS` | worker -> client | job id, pct, stage, message |
| `0x0D` | `LOG` | worker -> client | level, message |
| `0x0E` | `RESULT` | worker -> client | status, timings, output digest |
| `0x0F` | `DOWNLOAD_REQ` | client -> worker | job id |
| `0x10` | `STREAM_BEGIN` | worker -> client | name, size, sha256 |
| `0x11` | `STREAM_END` | worker -> client | bytes sent, sha256 |
| `0x16` | `DOWNLOAD_CHUNK` | worker -> client | **binary**, up to 256 KiB |
| `0x12` | `CANCEL` | client -> worker | job id |
| `0x13` | `ERROR` | both | code, message |
| `0x14` | `BYE` | client -> worker | reason |

## Handshake

The client speaks first; anything other than `HELLO` as the first frame is
refused with `ERROR {code: "expected_hello"}`. The version check compares **major
versions only**, so a `1.x` client works against a `1.y` daemon, while `2.0` or
`99.0` is rejected with a message that names both versions.

```json
--> {"magic":"RDO1","protocol_version":"1.0","client":"rdo-gui",
     "platform":"LAB-CLIENT","sent_at":1760000000.0}
<-- {"worker_id":"LAB-WORKER-1a2b3c","capabilities":{ ... }}
```

`capabilities` tells the client what the worker can do before any job is built:
FFmpeg version and path, hardware encoders, GPU name, `nvenc_available`,
`cuda_device`, `torch_available`, engine list, CPU count, `max_concurrent_jobs`.

## Upload

```
--> UPLOAD_BEGIN {"name":"clip.mp4","size":59898880,"sha256":"...",
                 "asset_id":"a1b2c3d4e5f6"}
    <raw bytes, exactly `size` of them, nothing else on the wire>
<-- UPLOAD_ACK   {"asset_id":"...","direction":"upload",
                  "received":4194304,"size":59898880,"pct":7.01}   (every 4 MiB)
--> UPLOAD_END   {"asset_id":"...","sha256":"..."}
<-- UPLOAD_DONE  {"asset_id":"...","size":59898880,"sha256":"...",
                  "seconds":0.54}
```

The payload between `UPLOAD_BEGIN` and `UPLOAD_END` is **not framed**. The worker
consumes exactly `size` raw bytes with `recv_exact`, writing and hashing as it
goes, so a truncated transfer fails as a short read instead of a checksum
mismatch at the end. Intermediate `UPLOAD_ACK` frames report upload progress;
they are JSON and travel back on the socket, never in the byte stream.

The file is written to `<upload-id>/<name>.part` and only promoted to its final
name once the size **and** digest match; a mismatch emits
`ERROR {code: "checksum_mismatch"}` and discards the staging directory.

Client-supplied identifiers are treated as untrusted input, because they become
directory names:

* `asset_id` is kept only when it is at most 64 characters of lowercase hex -
  otherwise the worker substitutes a fresh uuid, so a value like
  `../../etc/passwd` can never escape the data root.
* `name` goes through `safe_filename()`, which keeps the basename, replaces
  every character outside `[A-Za-z0-9._-]`, strips leading dots and truncates to
  180 characters.

Requests above `MAX_UPLOAD_BYTES` are refused with
`ERROR {code: "upload_too_large"}` before a single byte is accepted.

## Job lifecycle

```
--> JOB_SUBMIT {"job_type":"transcode","video_codec":"libx264", ... }
<-- JOB_ACK   {"job_id":"j-000123","queue_position":0}
--> PROGRESS  {"pct":12.5,"stage":"encode","message":"frame=120"}
--> RESULT    {"status":"ok","compute_seconds":13.13,
               "output_name":"out.mp4","output_size":...,
               "output_sha256":"...","encoder":"libx264","device":"CPU"}
```

`PROGRESS` is rate-limited to one frame per 120 ms and is **monotonic per job**:
if an out-of-order or duplicate update would move the percentage backwards the
worker clamps it. A UI that jumps from 80% to 20% looks broken even when the
engine is fine.

Rejections are explicit rather than best-effort: an unknown encoder, an
out-of-range CRF, a resolution the spec does not define or a job type the worker
does not implement all come back as `JOB_REJECT` (or `RESULT` with
`status != ok`) carrying the reason.

## Download

```
--> DOWNLOAD_REQ {"job_id":"j-000123"}
<-- STREAM_BEGIN {"name":"out.mp4","size":4194304,"sha256":"..."}
<-- DOWNLOAD_CHUNK <binary, 256 KiB>   (repeated)
<-- STREAM_END   {"bytes":4194304,"sha256":"..."}
```

The client hashes the bytes while writing them to `<name>.part` and renames only
after the digest matches. A mismatch discards the file; the download is never
reported as successful.

`LOG` frames may be interleaved with `PROGRESS` and `STREAM_*` at any point, so
the client keeps a per-phase frame reader rather than assuming strict
alternation.

## Latency measurement

`PING_BATCH {"count":25}` returns aggregated statistics - min/avg/max, jitter,
and the worker's own clock - so the client can show link quality before a job is
submitted, and the benchmark can report the network floor separately from the
encode.

## Limits and timeouts

| Constant | Value | Why |
|---|---|---|
| `MAX_FRAME_PAYLOAD` | 1 MiB | caps per-frame memory; chunks are 256 KiB |
| `STREAM_CHUNK` | 256 KiB | large enough to amortise syscalls, small enough for smooth progress |
| `MAX_UPLOAD_BYTES` | 8 GiB | disk guard; a client asking for more is rejected |
| `UPLOAD_ACK_INTERVAL` | 4 MiB | intermediate progress acknowledgement while streaming |
| `SOCKET_CONNECT_TIMEOUT` | 8 s | fail fast on a wrong address |
| `SOCKET_READ_TIMEOUT` | 20 s | a silent peer is dropped |
| `STALL_TIMEOUT` | 90 s | no progress for this long ends the job |
| `HEARTBEAT_INTERVAL` | 2 s | keeps NAT/proxy state alive on long jobs |
| `PROGRESS_MIN_INTERVAL` | 120 ms | caps progress frame rate |

## Compatibility rules

* Major versions must match; minor versions may differ in either direction.
* A frame that is unexpected at that point in the session is not silently
  ignored: the worker answers `ERROR {code: "unexpected_frame", ...}` naming the
  frame and closes the connection. A half-implemented feature therefore fails
  loudly instead of hanging both ends.
* New optional JSON fields may be added to any payload; readers ignore unknown
  keys.
