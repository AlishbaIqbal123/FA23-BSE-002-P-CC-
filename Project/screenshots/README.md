# Screenshots

Captured from the running application, not drawn by hand:

```powershell
python tools\screenshots.py
```

The tool starts a real daemon on a loopback port, connects the real
`MainWindow`, submits a real ffmpeg encode and grabs the widget as the state
changes. It exits non-zero rather than saving a misleading image: if the client
never reaches the online state, the job never finishes, or no progress frame ever
arrives, it fails instead of producing a screenshot that implies otherwise.

| File | State |
|---|---|
| `01-startup.png` | fresh window, offline |
| `02-connected-capabilities.png` | handshake complete - worker capabilities, latency |
| `03-job-running.png` | mid-encode, progress bar advancing |
| `04-job-complete.png` | result downloaded and verified |
| `05-compute-tab.png` | the compute tab (matmul / conv2d / FFN / elementwise) |
| `06-log-console.png` | the colour-coded log pane with each severity level |

Resolution is 1440x940. Because Qt runs with `QT_QPA_PLATFORM=offscreen`, the
capture works over SSH and in CI.
