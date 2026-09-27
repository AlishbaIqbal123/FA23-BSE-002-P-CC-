# Lab 2 — Multi-Threaded TCP Server with a Length-Prefixed (Header) Protocol

| Field | Detail |
|---|---|
| **Lab Number** | 2 |
| **Course** | Parallel and Distributed Computing |
| **Topic** | Sockets, the `threading` module, thread synchronisation, application-level protocols |

---
<img width="1917" height="396" alt="image" src="https://github.com/user-attachments/assets/8453390e-74ce-4be8-9f1b-86379d7a07d4" />

## 1. Objective

To write a multi-threaded TCP server and a matching client using Python's `threading` module, such that:

1. The server accepts and serves **more than one client simultaneously**.
2. Every incoming connection is handled by a **new thread** created with `threading.Thread()`.
3. The server prints the **active thread name, client IP address, and port number** for each connection.
4. Shared state and terminal output are protected using a **`threading.Lock`**, acquired with `acquire()` and released with `release()`.
5. The client exchanges messages with the server **continuously until the user exits**.
6. Messages are framed with a **3-byte length header** so the server can overcome the 16-character read limitation inherent to stream-oriented TCP.

---

## 2. Task Requirements

### 2.1 Server requirements

- Accept and handle multiple clients simultaneously.
- Start a new thread via `threading.Thread()` for each new client connection.
- Display the active thread name, client IP, and port number on the terminal.
- Use a `Lock` (`acquire()` / `release()`) for thread synchronisation.

### 2.2 Client requirements

- Exchange messages with the server continuously until the user chooses to exit.

### 2.3 The 16-character limitation

TCP is a **stream-oriented** protocol, not a message-oriented one. It moves data as a continuous
flow of bytes with no inherent "start" or "stop" markers and no preserved message boundaries.

If a server calls `recv(16)`, it pulls only the first 16 bytes out of the stream. If the real
message is 50 characters long, the remaining 34 bytes stay sitting in the OS receive buffer and
will be misread by the *next* `recv()` call — producing corrupted or garbled output. A fixed-size
read is therefore unsafe for any variable-length message.

### 2.4 The header protocol solution

To make message boundaries explicit, we implement an **application-level protocol**: every message
is preceded by a fixed-size header that states how long the payload is.

**Client logic**

1. Calculate the length of the message string.
2. Use `.zfill(3)` to format that length as a 3-digit string (e.g. `12` becomes `"012"`).
3. `.encode()` both the 3-byte header and the message payload.
4. Send the 3-byte header first, immediately followed by the message bytes.

**Server logic**

1. Read exactly 3 bytes for the header.
2. `.decode()` the 3 bytes and cast them to an `int` to obtain the payload length.
3. Read again, passing that specific `int` as the buffer size, so the entire message — and
   nothing more — is consumed.

The server therefore always knows precisely how many bytes to "siphon" from the stream, regardless
of the message length. The 3-byte header caps the payload at 999 bytes (`000`–`999`).

**Wire format**

```
+--------------+---------------------------+
| 3 bytes      | N bytes                   |
| length ASCII | UTF-8 encoded payload     |
| e.g. "042"   | 42 characters             |
+--------------+---------------------------+
```

---

## 3. Design

### 3.1 Threading model: thread-per-connection

```
        MainThread                                  ClientHandler-N (one per client)
        -----------                                  ------------------------------------
        bind() / listen()
        accept()  <--- connection 1 --------------->  while True:
        accept()  <--- connection 2 --------------->      read 3-byte header
        accept()  <--- connection 3 --------------->      read N-byte payload
            |                                           print [thread] IP/Port/message
            |                                           send reply
            +-- never blocks on client I/O                close socket
                (each I/O wait happens on the
                 handler thread, not the acceptor)
```

The main thread performs **only** `accept()` and immediately hands the socket off to a new thread.
This is the key design decision: a blocking `recv()` inside a client handler cannot stall the
accept loop, so client 2 can be accepted while client 1 is still waiting for data.

Each thread is given an explicit, descriptive `name` (`ClientHandler-1`, `ClientHandler-2`, …) so
the thread name printed in the log is meaningful rather than an auto-generated `Thread-5`.

### 3.2 Synchronisation

Two `threading.Lock` objects are used, both with explicit `acquire()` / `release()`:

| Lock | Protects | Why it is needed |
|---|---|---|
| `print_lock` | `print()` calls in `log()` | Without it, two threads printing at once produce interleaved, unreadable output such as `Client IP=127.0.0.1 Porm: 54069age: hi`. |
| `client_count_lock` | the `client_count` counter | The read-modify-write on the counter is three separate bytecodes and is not atomic, so two threads could otherwise assign the same ID. |

`print_lock` is the required demonstration of thread synchronisation. It is released in a `finally`
block, so a crash inside the print cannot leave the lock held and deadlock every other thread.

### 3.3 Message flow per client

1. Client reads a line from the user and truncates it to 999 characters.
2. Client sends `zfill(3)` length header + payload.
3. Server's handler thread reads the header, derives the length, reads the payload.
4. Server logs `[thread name] IP | header=NNN (N bytes) | message: ...` under the print lock.
5. Server replies with its own header + `Server received: <message>`.
6. Steps 2–5 repeat until the client disconnects.

---

## 4. File Structure

```
lab2/
├── readme.md      ← this document
├── Server.py      ← multi-threaded TCP server (110 lines)
├── Client.py      ← interactive TCP client (85 lines)
└── screenshots/
    └── 01-lab2-output.png
```

### 4.1 Server.py

| Function / Object | Role |
|---|---|
| `log(message)` | Synchronised print helper — `acquire()` / `release()` around `print()`. |
| `recv_exact(conn, n)` | Loops on `recv()` until exactly `n` bytes have arrived. |
| `send_message(conn, message)` | Sends `zfill(3)` header + payload. |
| `handle_client(conn, addr)` | Per-connection loop: read header, read payload, log, reply. |
| `main()` | Binds, listens, and accepts in a loop, spawning a thread per connection. |

### 4.2 Client.py

| Function / Object | Role |
|---|---|
| `log(message)` | Synchronised print helper (main thread vs. receiver thread). |
| `recv_exact(conn, n)` | Same exact-length read helper as the server. |
| `send_message(conn, message)` | Sends `zfill(3)` header + payload. |
| `receive_responses(conn)` | Background thread: continuously reads and prints server replies. |
| `main()` | User input loop; exits on `exit` / `quit` / `bye` / `Ctrl+C`. |

The client is itself multi-threaded: the **main thread** blocks on `input()` for the user, while the
**`ResponseReceiver` thread** blocks on `recv()` for server replies. Neither can wait on the other,
so replies appear the instant they arrive.

---

## 5. How to Run

Open two terminals in the `lab2` directory.

**Terminal 1 — start the server**

```bash
python Server.py
```

```
Server is in PASSIVE state, listening for connections on 127.0.0.1:65432
Main thread: MainThread | waiting to accept...
```

**Terminals 2, 3, 4… — start one or more clients**

```bash
python Client.py
```

```
Connected to server at 127.0.0.1:65432  (type "exit" or press Ctrl+C to quit)

You  -> hello
You  -> This is a message longer than 16 characters
You  -> exit
```

Repeat the client command in additional terminals to connect several clients at once. Type `exit`
(or press `Ctrl+C`) in a client to disconnect it; the server thread for that client exits on its own.

To stop the server, press `Ctrl+C` in terminal 1.

**To test on other machines**, change `HOST` in *both* files from `127.0.0.1` to the server's LAN
address (e.g. `192.168.1.5`) and make sure the OS firewall permits inbound TCP on port `65432`.

---

## 6. Sample Output

### 6.1 Server

```
Server is in PASSIVE state, listening for connections on 127.0.0.1:65432
Main thread: MainThread | waiting to accept...

[ClientHandler-1] STARTED for Client IP=127.0.0.1 Port=54069 | active threads: 2
[ClientHandler-1] Client IP=127.0.0.1 Port=54069 | header=021 (21 bytes) | message: hello from client one
[ClientHandler-1] Client IP=127.0.0.1 Port=54069 | header=014 (14 bytes) | message: second message
[ClientHandler-1] Client IP=127.0.0.1 Port=54069 | disconnected: client closed the connection
[ClientHandler-1] Client IP=127.0.0.1 Port=54069 | connection closed, thread exiting
[ClientHandler-2] STARTED for Client IP=127.0.0.1 Port=63834 | active threads: 2
[ClientHandler-2] Client IP=127.0.0.1 Port=63834 | header=012 (12 bytes) | message: alpha from A
[ClientHandler-2] Client IP=127.0.0.1 Port=63834 | header=007 (7 bytes) | message: A again
[ClientHandler-2] Client IP=127.0.0.1 Port=63834 | disconnected: client closed the connection
[ClientHandler-2] Client IP=127.0.0.1 Port=63834 | connection closed, thread exiting
```

Note that client 1 sent **two** messages over one connection — the handler's `while True` loop keeps
serving the same client, which is what makes the exchange continuous.

### 6.2 Client

```
Connected to server at 127.0.0.1:65432  (type "exit" or press Ctrl+C to quit)

You  -> SERVER REPLY  -> Server received: hello from client one
You  -> SERVER REPLY  -> Server received: second message
You  -> Closing connection. Goodbye!
[ResponseReceiver] Server closed the connection.
```

### 6.3 Evidence of simultaneous handling

Three clients connected at the same time, each holding its socket open:

```
[ClientHandler-1] STARTED for Client IP=127.0.0.1 Port=63111 | active threads: 2
[ClientHandler-2] STARTED for Client IP=127.0.0.1 Port=63112 | active threads: 3
[ClientHandler-3] STARTED for Client IP=127.0.0.1 Port=63113 | active threads: 4
```

`threading.active_count()` climbing from 2 to 4 proves all three handler threads were alive
**simultaneously**, not one after another. Had the server been single-threaded, only one client
could ever be in this state at a time.

---

## 7. Screenshot

Save the image below in a `screenshots/` subfolder next to this file, using the exact filename shown
in the image tag. If you prefer to keep everything flat, move the PNG into `lab2/` and drop the
`screenshots/` prefix from the path.

| File | What it shows |
|---|---|
| `screenshots/01-lab2-output.png` | Server terminal serving multiple clients simultaneously |

The single screenshot captures the server window with the main thread plus every
`ClientHandler-*` thread visible at once, so one image covers the whole lab: the thread names,
the client IP and port numbers, the 3-byte header with its decoded length, and the reassembled
message longer than 16 characters.

![Server serving multiple clients simultaneously](screenshots/01-lab2-output.png)

**To capture it:** maximise the terminal and increase the font size so the text stays legible when
printed. Start the server, then open `Client.py` in two or three further terminals and send a message
from each. Take the screenshot of the server window once all the connections are showing — ideally
with `active threads` having climbed past 2. On Windows press `Win + Shift + S`; on macOS press
`Cmd + Shift + 4`.

---

## 8. Testing Performed

| # | Test | Result |
|---|---|---|
| 1 | Single client, two sequential messages on one connection | Both received and acknowledged |
| 2 | 25 clients launched simultaneously | All 25 served, no errors |
| 3 | 3 clients holding connections open at once | `active threads` 2 → 3 → 4, all three concurrent |
| 4 | 999-byte payload (maximum the 3-byte header allows) | Reassembled intact |
| 5 | 1-byte payload (minimum) | Reassembled intact |
| 6 | Header deliberately sent **one byte at a time** in three separate packets | Correctly reassembled — see §9 |
| 7 | Client types `exit` | Clean disconnect, handler thread exits, no traceback |
| 8 | Server `Ctrl+C` | Clean shutdown, total client count reported |
| 9 | 68 total connections across mixed tests | Zero errors, empty `stderr` |

---

## 9. Important Note: `recv(3)` Is Not Guaranteed to Return 3 Bytes

The lab brief states that `recv(3)` "retrieves exactly the length information". This is the standard
simplification used to teach the header protocol, but it is **not guaranteed by TCP**. A single
`recv(3)` call can legally return 1, 2, or 3 bytes, and the 3 header bytes may be split across
several packets. Consequences of the naive version:

- `int(header.decode())` raises `ValueError` on a 1-byte fragment such as `b"0"`.
- If a truncated header decodes to a smaller number, the server reads too few payload bytes and the
  real message is silently truncated.
- Leftover bytes remain in the buffer and corrupt the *next* `recv()` on that connection.

To be safe while still following the intended two-step logic, each read goes through `recv_exact()`:

```python
def recv_exact(conn, n):
    buffer = b''
    while len(buffer) < n:
        chunk = conn.recv(n - len(buffer))
        if not chunk:
            raise ConnectionError('client closed the connection')
        buffer += chunk
    return buffer
```

It requests the remaining byte count each iteration, so it is correct whether the data arrives in
one packet, split across packets, or coalesced with the payload. Test 6 above confirms this works
when the header is deliberately fragmented.

Two related corrections applied in this implementation:

- **`sendall()` instead of `send()`** — a single `send()` may transfer only part of the buffer.
- **`SO_REUSEADDR` + an explicit backlog** — without `SO_REUSEADDR`, restarting the server within
  the `TIME_WAIT` window fails with `[Errno 10048] Address already in use`.

---

## 10. Limitations

1. **Payloads are capped at 999 bytes**, because the header is a fixed 3 bytes (`zfill(3)`).
   Supporting longer messages needs a wider field or a variable-length/decimal length header.
2. **Thread-per-connection does not scale to thousands of clients.** Each connection costs a full OS
   thread plus its stack (~8 MB virtual on Windows). A fixed-size thread pool or an
   `asyncio` event loop would be the next step.
3. **No authentication, encryption, or authorisation.** The channel is plaintext HTTP-free TCP on
   the loopback interface only.
4. **The server's `print` is synchronous**, so logging is fine for a lab but a bottleneck at scale.
5. **No automatic reconnect** in the client if the server restarts.

---

## 11. Possible Extensions

- A thread pool with `concurrent.futures.ThreadPoolExecutor` and a bounded queue.
- `selectors` or `asyncio` for handling many sockets on a small number of threads.
- A 4-byte (`zfill(4)`) or 8-byte header to lift the 999-byte limit.
- Per-client state (message history, timestamps) guarded by that client's own lock.
- Graceful shutdown with `signal` handling and a `threading.Event` to wake blocked readers.
- Logging to a file via the `logging` module with thread-name tagging.
- Unit tests with `pytest` and a `socketpair()` to assert header framing and fragmented reads.

---

## 12. Key Concepts Demonstrated

- **Client–server socket programming** — `socket()`, `bind()`, `listen()`, `accept()`, `connect()`.
- **`threading.Thread`** — one thread per connection, custom thread names, `daemon=True`.
- **Thread synchronisation** — `threading.Lock`, `acquire()` / `release()`, `try` / `finally` to
  guarantee release, and which shared state actually needs protecting.
- **Blocking I/O and concurrency** — why the acceptor thread must never perform client I/O.
- **Stream vs. message semantics** — why a length prefix is required, and how it fixes the
  16-character limitation.
- **Encoding** — `.encode()` / `.decode()` for converting between `str` and `bytes`.
- **Resource management** — `try` / `except` / `finally`, half-close via `shutdown(socket.SHUT_WR)`
  so the peer sees a clean EOF instead of a connection reset, and `SO_REUSEADDR`.
- **`threading.active_count()`** as a simple empirical check that N clients really were concurrent.

---

## 13. Conclusion

The server successfully handles multiple clients simultaneously by delegating each accepted
connection to its own thread while the main thread remains free to accept the next one. The
3-byte length-header protocol gives both sides an unambiguous way to find message boundaries in
TCP's byte stream, and the `print_lock` demonstrates that shared resources — here, terminal output
— must be guarded explicitly when several threads run concurrently. Testing confirmed correct
behaviour for payload sizes from 1 to 999 bytes, for fragmented headers, and for 25 simultaneous
clients with no errors.

---

## 14. Declaration

This lab was submitted as original work. All code was written and tested by the student named
above. No third-party libraries were used — only the Python standard library modules `socket`
and `threading`.

**Signature:** ____________________  **Date:** ______________
