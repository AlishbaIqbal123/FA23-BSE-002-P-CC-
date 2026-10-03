# Network setup and troubleshooting

The worker listens on TCP **7575** and serves exactly one protocol described in
`PROTOCOL.md`. Everything below assumes a direct LAN connection between two
machines, which is the configuration the lab uses.

```
   client laptop                switch / direct cable              GPU worker
   192.168.1.2                                             192.168.1.1
   python client/app.py  <--------- TCP 7575 --------->   python server/daemon.py
```

## 1. Addressing

A direct cable between two NICs has no DHCP server, so **both** machines need a
static address on the same private subnet. Either run the helper on both sides:

```powershell
# on the worker (the machine with the GPU)
powershell -ExecutionPolicy Bypass -File tools\configure_network.ps1 `
    -Role Server -ServerIP 192.168.1.1 -ClientIP 192.168.1.2

# on the client laptop
powershell -ExecutionPolicy Bypass -File tools\configure_network.ps1 `
    -Role Client -ServerIP 192.168.1.1 -ClientIP 192.168.1.2
```

Useful flags: `-AdapterAlias "Ethernet"` when more than one adapter is up,
`-PrefixLength 30` for a two-machine /30 link, and `-Revert` to hand the adapter
back to DHCP. Without an alias the script picks the first wired adapter and
prints what it chose - check that line before assuming it picked the right NIC.

Or do it by hand:

```powershell
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 192.168.1.1 -PrefixLength 24
Set-DnsClient -InterfaceAlias "Ethernet" -ResetServerAddresses   # no DNS on a bare link
```

Verify from the client before starting anything:

```powershell
ping 192.168.1.1
Test-NetConnection -ComputerName 192.168.1.1 -Port 7575
```

`ping` succeeding but `Test-NetConnection` failing means the daemon is not
running or is bound to the wrong interface - see the next section.

## 2. Start the worker

```powershell
python server\daemon.py
```

The banner prints the resolved configuration; read it before blaming the network:

```
  protocol       : 1.0
  bind           : 0.0.0.0:7575
  allowed        : 127.0.0.0/8,::1/128,192.168.1.0/24
  ffmpeg         : ... (version string)
  gpu            : none detected
```

Flags worth knowing:

| Flag | Purpose |
|---|---|
| `--host 192.168.1.1` | bind one interface instead of all of them |
| `--port 7575` | change the port |
| `--workers 2` | run two jobs at once |
| `--allow 192.168.1.0/24` | repeatable; replaces the default allow-list |
| `--any-client` | accept from anywhere - lab networks only |
| `--ffmpeg <path>` | use a specific binary |
| `--prune-on-start` | delete job directories left over from a crash |
| `--keep-jobs 25` | how many finished jobs to retain |
| `-v` | per-connection debug logging |

The worker also writes `server/data/daemon.json` with its current state, which is
handy for confirming from another shell that it is up.

**The allow-list is a real control, not decoration.** By default only
`127.0.0.0/8`, `::1/128` and `192.168.1.0/24` may connect. A client outside
those ranges is dropped during the handshake with a log line naming its address.
If a connection is refused and you are sure the addresses are right, check that
banner line first.

## 3. Start the client

```powershell
python client\app.py --host 192.168.1.1 --port 7575
```

`--host` only pre-fills the address field; the connection happens when Connect is
pressed, so you can also type it in the window. The state pill turns green
(`ONLINE`) and the capability panel fills in when the handshake succeeds.

## 4. Measure the link before you trust a benchmark

```powershell
# on the worker
python tools\probe_network.py --host 0.0.0.0 --port 7600 --listen

# on the client
python tools\probe_network.py --host 192.168.1.1 --port 7600 --count 50 --mb 64
```

This reports RTT (min/avg/max, jitter) and one-way and round-trip throughput. Run
it *before* the benchmark: if the link is 100 Mbit/s rather than 1 Gbit/s, the
transfer phases will dominate the encode and the end-to-end speedup will be
misleading for reasons that have nothing to do with the code.

## 5. Benchmark

```powershell
python tools\make_sample_media.py --all
python tools\bench.py --host 192.168.1.1 --port 7575 --all --repeat 3 `
    --out benchmarks\results-gpu-lan.json --markdown benchmarks\results-gpu-lan.md
```

`--repeat` matters. A single run on a shared machine has more variance than the
effect being measured - see `benchmarks/RESULTS.md` for two runs of the same clip
differing by 1.8x - so never quote a one-shot figure.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `[WinError 10061] actively refused` | nothing listening on that port/interface | is the daemon running? did it bind `0.0.0.0`? |
| `timed out` while pinging | firewall, or the wrong subnet | allow the port: `New-NetFirewallRule -DisplayName RDO -Direction Inbound -Protocol TCP -LocalPort 7575 -Action Allow` |
| `client 192.168.x.y is not in the allowed subnets` | allow-list | add `--allow 192.168.1.0/24`, or use `--any-client` on a lab LAN |
| handshake `ERROR expected_hello` | something that is not the client connected (a port scanner, `Test-NetConnection` opening a raw connection is fine, a browser is not) | ignore it; only the daemon's own log matters |
| handshake refused on version | client and daemon are from different copies | deploy the same tree to both machines |
| connects, then every job is rejected | the spec does not match the worker (e.g. NVENC requested on a CPU-only box) | check the capability panel; the GUI should have selected `libx264` for you |
| upload stalls near the end | 8 GiB cap, or the disk is full | check free space in `server/data` |
| GUI freezes during a big transfer | a regression - blocking work on the GUI thread | `tests/test_gui.py` asserts the event loop keeps running; run it |
| `ffmpeg not found` | not on `PATH` | `--ffmpeg <path>`, or `RDO_FFMPEG` |
| old ffmpeg rejects an option | 2013-era build | the code already avoids `-hide_banner` and `-2`; report the exact message |

## Firewall

Windows blocks inbound by default on public profiles. Either allow the port
explicitly (above) or run the two machines on a **private** network profile:

```powershell
Set-NetConnectionProfile -InterfaceAlias "Ethernet" -NetworkCategory Private
```

## Security notes for the lab

* The daemon has **no authentication** - anyone who may connect may submit jobs
  and consume CPU. Keep it on a trusted LAN or tunnel it over SSH
  (`ssh -L 7575:127.0.0.1:7575 user@worker`).
* Uploads are capped at 8 GiB and run as the user that started the daemon, so
  start it as a normal user, not as an administrator.
* Filenames and identifiers coming from the client are sanitised before they
  touch the filesystem, but the allow-list is the real boundary.
