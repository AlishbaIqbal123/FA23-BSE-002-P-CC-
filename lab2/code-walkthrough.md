# Understanding the Code — Lab 2 Walkthrough

A companion to `readme.md`. That document says *what* was built and *what was tested*. This one
explains **how the code actually works, line by line**, so you can understand it well enough to
present it or answer questions about it.

**Read in order.** Sections 1–4 build the intuition, 5–7 walk the code, 8–11 cover the details that
come up in viva questions.

---

## Table of Contents

1. [The 30-second picture](#1-the-30-second-picture)
2. [Words you need to know](#2-words-you-need-to-know)
3. [The core problem: TCP is a stream, not a stream of messages](#3-the-core-problem-tcp-is-a-stream-not-a-stream-of-messages)
4. [The solution: a 3-byte length header](#4-the-solution-a-3-byte-length-header)
5. [Why threads, and what breaks without them](#5-why-threads-and-what-breaks-without-them)
6. [Walking through `Server.py`](#6-walking-through-serverpy)
7. [Walking through `Client.py`](#7-walking-through-clientpy)
8. [One full message, end to end](#8-one-full-message-end-to-end)
9. [Synchronisation: what the lock is protecting](#9-synchronisation-what-the-lock-is-protecting)
10. [The `recv(3)` subtlety](#10-the-recv3-subtlety)
11. [Error handling: every exception and when it fires](#11-error-handling-every-exception-and-when-it-fires)
12. [Full lifecycle: startup to shutdown](#12-full-lifecycle-startup-to-shutdown)
13. [Viva questions and answers](#13-viva-questions-and-answers)
14. [Experiments to try](#14-experiments-to-try)

---

## 1. The 30-second picture

You type into a client window. The text travels to the server, the server prints it with the
client's IP and port, and sends an acknowledgement back, which appears in your window. Meanwhile
other client windows are doing exactly the same thing, and none of them wait on each other.

Underneath, three separate ideas are doing the work:

| Idea | Why it exists | Where it lives |
|---|---|---|
| **Sockets** | let two processes on a network exchange bytes | `socket` module |
| **The header protocol** | makes message boundaries explicit in a byte stream | `recv_exact()` / `send_message()` |
| **Threads** | let many clients be served at the same time | `threading` module |

If you remember nothing else: **the protocol tells us how much to read, the threads decide who gets
to read it, and the lock stops their output from turning to mush.**

---

## 2. Words you need to know

### Socket

A socket is one end of a connection. When a client and server talk, there are **two** sockets, one
at each end. Each socket has a file descriptor (a number the OS uses to identify it) — on Windows
it can be over 1000, which is why you sometimes see the client port as `54069`.

### IP address and port

- **IP** identifies the *machine*. `127.0.0.1` is localhost — this machine only.
- **Port** identifies the *program* on that machine. Our server uses `65432`, an arbitrary number
  above 1024 (below 1024 is reserved for well-known services like HTTP on 80).
- `addr` returned by `accept()` is a tuple `(ip, port)` of the **client**, not the server. The
  server's own port is always 65432. This trips people up: the varying port in the log is the
  client's *ephemeral* port, assigned automatically by the OS.

### The four server calls, in order

| Call | What it does |
|---|---|
| `socket(AF_INET, SOCK_STREAM)` | Create the socket. `AF_INET` = IPv4. `SOCK_STREAM` = TCP (reliable, ordered bytes). |
| `bind((HOST, PORT))` | Claim a specific address and port. |
| `listen(BACKLOG)` | Start listening; allow up to 5 pending connections in the queue. |
| `accept()` | **Blocks** until a client connects, then returns `(conn, addr)`. |

The client only needs two: `socket(...)` then `connect((HOST, PORT))`.

### `bytes` vs `str` — why `.encode()` and `.decode()` appear everywhere

Sockets transfer **bytes**, never text. `send()` and `recv()` in Python 3 require `bytes`. But
`input()` gives you a `str` and `int(header.decode())` needs a `str`. So every crossing needs a
conversion:

```
str  --.encode()-->  bytes  --.send()-->  network  --.recv()-->  bytes  --.decode()-->  str
```

Forgetting either conversion is the single most common error in a first socket program:
`TypeError: a bytes-like object is required, not 'str'`.

### `AF_INET` vs `AF_INET6`

`AF_INET` is IPv4 (`127.0.0.1`). `AF_INET6` is IPv6 (`::1`). Not needed here.

---

## 3. The core problem: TCP is a stream, not a stream of messages

**TCP guarantees bytes arrive in order, and nothing else.** It has no concept of "a message". It
does not preserve your `send()` boundaries, so two separate `send()` calls might arrive as one
chunk, and one `send()` call might arrive as several.

### The failure, concretely

Say the client sends the 67-character message:

```
a much longer message from C that is well beyond sixteen characters
```

A naive server does `recv(16)`. That reads the **first 16 bytes only**:

```
recv(16) returned:  'a much longer me'
still in the buffer: 'ssage from C that is well beyond sixteen characters'
                      ^^^^^^^^ 51 bytes left sitting in the OS receive buffer
```

The server now has 51 bytes it does not know what to do with. The only sane thing to do is call
`recv()` again — but **how many bytes should it ask for?** It has no idea. If it asks for 16 again
it gets a meaningless fragment, and the printed output is corrupted. This is the 16-character
limitation the lab is about.

Three things can go wrong, and all three are real:

1. **Truncation** — the server reads 16 of 67 bytes and prints half a message.
2. **Corruption** — leftover bytes get mixed into the *next* read.
3. **Blocking** — if the server assumes each `recv(16)` equals one message, and the client sends a
   5-character message, the server **waits forever** for 16 bytes that will never come.

The underlying mistake is assuming a network stream has message boundaries. It does not.

---

## 4. The solution: a 3-byte length header

The fix is to **not guess**. Before the payload, the sender states exactly how many bytes follow.
Now the receiver never has to guess.

### The wire format

```
 +---------------------------+------------------------------+
 |  3 bytes                  |  N bytes                     |
 |  payload length as ASCII  |  the UTF-8 message          |
 +---------------------------+------------------------------+
```

### A worked example, byte by byte

Message: `"Hello"` — 5 characters.

```python
message = "Hello"                      # len() == 5
header  = str(5).zfill(3)              # "5"   -> "005"
wire    = header.encode() + b"Hello"   # what actually goes on the wire
```

The exact bytes on the wire:

```
30 30 35  48 65 6C 6C 6F
^^^^^^^^^  ^^^^^^^^^^^
"005"      "Hello"
header      payload
```

The server then does:

```python
header     = recv_exact(conn, 3)   # gets b"005"
msg_length = int(header.decode())  # gets 5
data       = recv_exact(conn, 5)   # gets b"Hello"  <- exactly right
```

And the 67-character message from the test run sent header `"067"` for 67 bytes.

### Why `zfill(3)` and not just `str(len(...))`

If we sent `"5"`, the server would read 1 byte, decode `5`, read 5 bytes, and then be **permanently
out of sync** — the next 2 bytes of the *next* header would be left over. Zero-padding makes the
header a **fixed width**, so the server always knows to read exactly 3. That fixed width is what
keeps the two sides in sync.

### Why 3 bytes, and what it costs you

3 digits = `000` to `999`, so the largest payload is **999 characters**. This is a real limitation,
not a theoretical one — send 1000 characters and the header becomes `"1000"`, four bytes, and the
server reads the first 3 (`"100"`), believes the message is 100 bytes, and desynchronises.

The server defends against this at `Server.py:50-51`:

```python
if not 0 < msg_length <= MAX_PAYLOAD:
    raise ValueError(f'invalid payload length {msg_length}')
```

A 4-byte header (`zfill(4)`) would raise the cap to 9999. The trade-off is a slightly larger
per-message overhead.

### Why `sendall()` and not `send()`

`send()` may transfer only *part* of the buffer and return that count. `sendall()` keeps going until
every byte is written. Note `send_message()` sends header and payload in **one** `sendall()` call
rather than two separate `send()` calls — fewer chances for the two halves to be split or
reordered.

---

## 5. Why threads, and what breaks without them

### Without threads — the naive version

```python
while True:
    conn, addr = server_socket.accept()
    header = conn.recv(3)          # client 1 is slow to type
    data = conn.recv(int(header))
    # ... handle, reply, close
```

A timeline, with Client 1 being slow to type and Client 2 already waiting:

```
time ──────────────────────────────────────────────────►
Server:  accept() → C1  recv() blocks...   still blocked...  finally gets C1
Client1: connect()   ████ typing slowly ████████████████████►
Client2:        connect() ·············· waiting, blocked in the OS queue
Client3:        connect() ·············· waiting, blocked in the OS queue
```

While the server is blocked in `recv()` for Client 1, **Clients 2 and 3 sit in the accept queue
doing nothing.** Worse, because the server reads a fixed 3 bytes then a fixed N bytes, a slow client
stalls everyone. The server is technically multi-client but practically single-client.

### With threads — our version

```
time ──────────────────────────────────────────────────►
Server:  accept()→C1  spawn T1  accept()→C2  spawn T2  accept()→C3  spawn T3
T1:      recv() blocks for C1 ... wakes, replies, closes
T2:      recv() blocks for C2 ... wakes, replies, closes
T3:      recv() blocks for C3 ... wakes, replies, closes
```

Three handler threads exist at once, each independently blocked in its own `recv()`. The main
thread **never blocks on client I/O at all** — it only accepts, which is the entire design.

This is why `Server.py:84` hands the socket off immediately:

```python
conn, addr = server_socket.accept()     # main thread
# ... spawn thread ...
thread.start()                          # handler thread does all the I/O
```

**The single most important line in the lab** is the fact that `accept()` is separated from `recv()`
by a thread boundary. If you ever put a `recv()` back into the main loop, you have lost the lab's
entire point.

---

## 6. Walking through `Server.py`

### 6.1 Imports and constants (`Server.py:1-9`)

```python
import socket
import threading
```

`HOST` and `PORT` must match the client's exactly or `connect()` fails with
`ConnectionRefusedError`.

`BACKLOG = 5` is the size of the pending-connection queue, **not** the max number of clients. With
threads, real concurrency is limited by RAM, not by the backlog.

`REPLY_PREFIX = b'Server received: '` is a `bytes` literal so it can be concatenated directly with
the incoming `bytes` payload.

### 6.2 The shared state and its locks (`Server.py:11-14`)

```python
print_lock = threading.Lock()
client_count_lock = threading.Lock()
client_count = 0
```

Two locks because there are two different pieces of shared state with different risks — see §9.

### 6.3 `log()` — the synchronised print (`Server.py:17-22`)

```python
def log(message):
    print_lock.acquire()
    try:
        print(message, flush=True)
    finally:
        print_lock.release()
```

- `acquire()` takes the lock, blocking until no other thread holds it. Only one thread can be inside
  this block at a time, so lines never interleave.
- `flush=True` forces the text out of Python's buffer immediately. Without it, output can appear
  late or out of order when redirected to a file — which is exactly what happens when you run
  `python Server.py > log.txt`.
- `finally` guarantees `release()` runs **even if `print` raises**. This is the correct discipline:
  without `finally`, one crash would leave the lock held forever and deadlock every other thread.

### 6.4 `recv_exact()` — the heart of the protocol (`Server.py:25-34`)

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

Line by line:

- `buffer = b''` — accumulate what we've got so far. `b''` is an empty **bytes** object.
- `while len(buffer) < n` — keep going until we have everything.
- `conn.recv(n - len(buffer))` — **ask only for what's still missing.** This is the key detail: if
  we already have 1 of 3 header bytes, we request 2, not 3. This is what makes the function immune
  to over-reading into the payload.
- `if not chunk` — `recv()` returning empty bytes `b''` is how TCP signals **orderly shutdown** (a
  clean disconnect, the peer closed its end). This is not an error condition, so we raise
  `ConnectionError` to unwind out to the handler's `except` block. Note that `b''` is falsy while
  `b'0'` is truthy, so this check is about emptiness, not content.
- `buffer += chunk` — bytes are immutable, so this builds a new object. Fine for a few hundred
  bytes; for megabytes you would use a `bytearray` to avoid repeated copying.

Why not just `recv(n)`? Because a single call is **not guaranteed** to return `n` bytes. It returns
whatever has arrived — possibly 1 byte, possibly all of them. See §10.

### 6.5 `send_message()` (`Server.py:37-39`)

```python
def send_message(conn, message):
    conn.sendall(str(len(message)).zfill(HEADER_SIZE).encode() + message)
```

One line doing four things:

1. `len(message)` — payload size in bytes.
2. `str(...)` → `"42"`.
3. `.zfill(HEADER_SIZE)` → `"042"`, the 3-digit header.
4. `.encode()` → `b"042"`, then `+ message` concatenates header and payload, and `sendall()`
   writes both.

`len()` must be called on **bytes**, not `str`. `len("😀")` is 1 in Python 3 (one character) but
`len("😀".encode())` is 4 (four bytes). This function is always called with bytes, so it is
correct — but it is a real trap if you ever pass a `str`.

### 6.6 `handle_client()` — the per-connection worker (`Server.py:42-67`)

This function runs **once per client, on its own thread**, for the client's whole lifetime.

```python
thread_name = threading.current_thread().name   # :43
ip, port = addr[0], addr[1]                     # :44
```

`threading.current_thread()` returns the thread object running *this* function. Reading `.name`
gives us the label we set in `main()`, which is why the log shows `ClientHandler-2` rather than a
useless `Thread-5 (handle_client)`. It is captured **once** outside the loop because it cannot
change during the thread's life.

`addr[0]` and `addr[1]` unpack the tuple into IP and port for readable log lines.

Then the `while True:` loop at `:46` is the heart of it — this is what makes the exchange
**continuous** rather than one-message-per-connection:

1. `header = recv_exact(conn, HEADER_SIZE)` (`:48`) — read 3 bytes.
2. `msg_length = int(header.decode())` (`:49`) — `"067"` → `67`.
3. Validate the length (`:50-51`) — reject nonsense before allocating.
4. `data = recv_exact(conn, msg_length)` (`:54`) — read exactly that many bytes.
5. `log(...)` (`:56-57`) — print thread name, IP, port, header, and decoded message.
6. Build a reply and `send_message(conn, reply)` (`:59-62`) — **the server speaks the same protocol
   in reverse**, so the client needs no special-case code for replies.

Steps 1–5 repeat for the next message on the same socket. Only when the client disconnects does
`recv_exact` raise and drop us out of the loop.

The reply truncation at `:60-61` is needed because `REPLY_PREFIX` is 17 bytes, so a 999-byte
incoming message would produce a 1016-byte reply — over the 999 cap, and its own header would be
wrong.

### 6.7 Error handling and cleanup (`Server.py:63-67`)

```python
except (ConnectionError, OSError, UnicodeDecodeError, ValueError) as e:
    log(f'... disconnected: {e}')
finally:
    conn.close()
    log(f'... connection closed, thread exiting')
```

`finally` guarantees the socket is closed **and** the thread's final line is logged, whether the
loop ended by a clean disconnect, an error, or anything unexpected. Without `finally`, a client that
vanished would leak a socket and an OS file descriptor.

### 6.8 `main()` — setup (`Server.py:70-79`)

```python
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server_socket.bind((HOST, PORT))
server_socket.listen(BACKLOG)
```

`SO_REUSEADDR` (`:74`) lets the server restart immediately after being stopped. Without it, the
old connection lingers in `TIME_WAIT` for a minute or two and you get
`[Errno 10048] Address already in use` — a confusing error the first time you rerun any server.

`global client_count` (`:71`) is required because the function **assigns** to the variable, not just
reads it. Without it Python would treat `client_count` as a new local and the counter would always
reset to 1.

### 6.9 The accept loop (`Server.py:81-101`)

```python
while True:
    conn, addr = server_socket.accept()        # :84  blocks here, not on I/O

    client_count_lock.acquire()                # :86
    try:
        client_count += 1
        client_id = client_count
    finally:
        client_count_lock.release()

    thread = threading.Thread(                 # :93
        target=handle_client,                  # what to run
        args=(conn, addr),                     # what to pass it
        name=f'ClientHandler-{client_id}',     # label shown in logs
        daemon=True,                           # see below
    )
    thread.start()                             # :99  begins running immediately
```

- `target=handle_client` is the **function** to run. `args=(conn, addr)` are the values passed in.
  (The keyword is `args` even for a tuple — a small Python wart.)
- `daemon=True` means the thread dies automatically when the main program exits, so you can hit
  `Ctrl+C` and the process shuts down without hanging on blocked threads. The trade-off: daemon
  threads are killed abruptly, so `finally` blocks may not complete on shutdown. For a lab this is
  the right choice; for production you'd join them cleanly.
- `threading.active_count()` at `:101` is printed as a **self-check** — it should climb as clients
  connect. Seeing it stay at 2 while you expect 3 connections would immediately tell you something
  is wrong.

There is one subtlety here: `thread.start()` at `:99` begins the new thread *before* the `log()` at
`:100`, so the handler may print its first line before the `STARTED` line. The output can therefore
look slightly out of order — that is expected, not a bug.

### 6.10 Shutdown (`Server.py:102-106`)

```python
except KeyboardInterrupt:      # Ctrl+C
finally:
    server_socket.close()
```

`Ctrl+C` raises `KeyboardInterrupt` **in the main thread** at the `accept()` call. It is caught so
the socket is closed cleanly and the total is reported. Note that `KeyboardInterrupt` is not caught
anywhere in `handle_client` — if you pressed `Ctrl+C` with three clients connected, the handler
threads would terminate with a traceback. That is acceptable for a lab; the robust fix is
`signal.signal(signal.SIGINT, handler)` plus a `threading.Event` to wake blocked readers.

### 6.11 The entry point (`Server.py:109-110`)

```python
if __name__ == '__main__':
    main()
```

`__name__` is `'__main__'` only when the file is run directly. This guard means the file can also be
`import`ed by another script without starting a server as a side effect. Standard Python practice.

---

## 7. Walking through `Client.py`

The client is **also** multi-threaded, for a mirror-image reason: the main thread blocks on
`input()` waiting for you, so a second thread is needed to read replies.

### 7.1 `receive_responses()` — the background reader (`Client.py:38-48`)

```python
while True:
    header = recv_exact(conn, HEADER_SIZE)   # :43
    msg_length = int(header.decode())
    data = recv_exact(conn, msg_length)      # :45
    log(f'SERVER REPLY  -> {data.decode()}')
```

Structurally identical to the server's read logic — which is the point: **both sides implement the
same protocol**, so neither needs to know in advance whether bytes are a message or a reply.

`except ... :` at `:47-48` has **no `as e`**, because the client does not care *why* the connection
ended, only that it ended. Printing a friendly message beats a raw traceback.

### 7.2 The input loop (`Client.py:60-71`)

```python
while True:
    message = input('You  -> ')                       # :62 blocks until you press Enter
    if message.strip().lower() in ('exit', 'quit', 'bye'):
        break                                         # :65 leave the loop
    if not message:
        continue                                      # ignore blank lines
    message = message[:MAX_PAYLOAD]                   # :70 enforce the 999 cap
    send_message(client_socket, message.encode())    # :71
```

- `.strip().lower()` makes `EXIT`, `Exit`, and `  exit  ` all work.
- The tuple `('exit', 'quit', 'bye')` with `in` is a clean way to accept several keywords.
- The cap at `:70` matters because a 3-byte header cannot describe a 1000-character message. The
  **client** must enforce it; the server could only detect the violation after the header was
  already misread.
- `input()` **blocks**, so while you sit there typing, the `ResponseReceiver` thread keeps printing
  replies. Neither thread waits on the other.

### 7.3 Graceful shutdown (`Client.py:74-81`)

```python
finally:
    try:
        client_socket.shutdown(socket.SHUT_WR)   # :77
    except OSError:
        pass
    receiver.join(timeout=2)                    # :80
    client_socket.close()                       # :81
```

This is the subtlest part of the client, and it fixes a real problem.

`shutdown(socket.SHUT_WR)` performs a **half-close**: it send a FIN to the server, saying "I will
send no more data," while still allowing the client to *receive*. The server's `recv_exact` receives
`b''`, raises `ConnectionError`, and logs a clean `disconnected: client closed the connection`.

Without it, calling `close()` while the server's reply is still in flight makes the OS send an RST
(reset) instead of a FIN, and the server logs the alarming but harmless:

```
[WinError 10054] An existing connection was forcibly closed by the remote host
```

`receiver.join(timeout=2)` waits up to 2 seconds for the reader thread to notice the close. The
**timeout is essential** — an untimed `join()` on a thread blocked in `recv()` would hang your
program forever. `timeout=2` means "give up waiting after 2 seconds" rather than "wait forever".

---

## 8. One full message, end to end

You type `hi` and press Enter in the client. Here is everything that happens, in order:

| # | Thread | What happens | Code |
|---|---|---|---|
| 1 | Client main | `input()` returns `"hi"` | `Client.py:62` |
| 2 | Client main | Not an exit word, not empty | `Client.py:63-68` |
| 3 | Client main | Truncate to 999 → still `"hi"` | `Client.py:70` |
| 4 | Client main | `str(2).zfill(3)` → `"002"`, `.encode()` → `b"002"` | `Client.py:35` |
| 5 | Client main | `sendall(b"002hi")` writes 5 bytes; `hi` is 2 | `Client.py:35` |
| 6 | — | Bytes travel over TCP, possibly split across packets | network |
| 7 | Server main | Blocked in `accept()` — **wakes up, returns `(conn, addr)`** | `Server.py:84` |
| 8 | Server main | Lock, increment counter, `client_id = 1`, release | `Server.py:86-91` |
| 9 | Server main | Create `Thread(name="ClientHandler-1")` and `start()` it | `Server.py:93-99` |
| 10 | ClientHandler-1 | `recv_exact(conn, 3)` may return 1, 2, or 3 bytes — **loops until 3** | `Server.py:48` |
| 11 | ClientHandler-1 | `int(b"002".decode())` → `2` | `Server.py:49` |
| 12 | ClientHandler-1 | `recv_exact(conn, 2)` → `b"hi"` | `Server.py:54` |
| 13 | ClientHandler-1 | `log(...)` under `print_lock` prints thread name, IP, port, header, message | `Server.py:56` |
| 14 | ClientHandler-1 | Builds `b"Server received: hi"` (19 bytes), sends `b"019"` + 19 bytes | `Server.py:59-62` |
| 15 | Client main | Already looping, waiting for your next input | `Client.py:61` |
| 16 | ResponseReceiver | `recv_exact(conn, 3)` → `b"019"` → 19, then 19 bytes | `Client.py:43-45` |
| 17 | ResponseReceiver | Prints `SERVER REPLY -> Server received: hi` | `Client.py:46` |

Note step 7: the main thread was parked in `accept()` the whole time you were typing. It only woke
once your bytes actually arrived.

---

## 9. Synchronisation: what the lock is protecting

### 9.1 Why `print_lock` is necessary

Python's `print()` is not atomic — it writes a string in several steps, including a separate
`write()` for the text and then for the newline. Two threads printing at once can be preempted
between those steps, producing output like:

```
[ClientHandler-1] Client IP=127.0.0.1 Porm: 54069age: hello
```

Both threads' characters are interleaved mid-word. The lock forces the whole print to finish
before another thread starts printing.

You can see this yourself: comment out the `acquire()` and `release()` in `log()`, open three
clients, and send messages from all three at once.

### 9.2 Why a *second* lock for the counter

```python
client_count_lock.acquire()
try:
    client_count += 1
    client_id = client_count
finally:
    client_count_lock.release()
```

`client_count += 1` looks like one line but compiles to roughly three machine-level steps: *read
the value, add one, store it back*. If two threads both read `5` before either stores, both store
`6`, and the counter under-counts — you would get two threads both named `ClientHandler-6`, and a
"6" that was never issued. The lock makes the read-modify-write atomic.

The general rule: **any shared mutable state that more than one thread touches needs a lock.**
Shared read-only data (like `HOST` and `PORT`) does not.

### 9.3 Why `try` / `finally` matters

If `print()` raised and we skipped `release()`, the lock would stay held forever. Every other thread
would then block forever on `acquire()` — a **deadlock**, and the process would appear to hang with
no error. The `finally` block makes the release unconditional.

---

## 10. The `recv(3)` subtlety

The lab brief says `recv(3)` "retrieves exactly the length information". For teaching purposes this
is the standard simplification, and in practice it usually works on a quiet local network. But it is
**not a guarantee TCP makes**, and a single call may legally return 1, 2, or 3 bytes.

**What goes wrong if you trust it:**

- `recv(3)` returns `b"0"` (one byte of a split header) → `int(b"0")` = `0` → you then read 0 bytes
  and print an **empty message**.
- `recv(3)` returns `b"0"` → if it were a non-digit fragment, `int()` raises
  `ValueError: invalid literal for int()`.
- The remaining header bytes sit in the buffer and corrupt the **next** read.

`recv_exact()` fixes all of these by looping and requesting only the missing count. It produces
identical behaviour when the header does arrive in one piece, so following the intended two-step
logic costs nothing:

```python
def recv_exact(conn, n):
    buffer = b''
    while len(buffer) < n:
        chunk = conn.recv(n - len(buffer))   # ask only for what's missing
        if not chunk:
            raise ConnectionError('client closed the connection')
        buffer += chunk
    return buffer
```

I verified this by sending the header **one byte at a time** in three separate packets — the server
still reassembled it correctly. The naive version has no defence against that.

---

## 11. Error handling: every exception and when it fires

| Exception | Raised when | Caught at |
|---|---|---|
| `ConnectionError` | `recv()` returned `b''` — the peer closed cleanly | `Server.py:63`, `Client.py:47` |
| `OSError` | Socket-level failure: reset, broken pipe, refused | `Server.py:63`, `Client.py:47` |
| `UnicodeDecodeError` | Bytes were not valid UTF-8 (`Client.py:49` decode) | `Server.py:63`, `Client.py:47` |
| `ValueError` | Header was not a number, or out of range (`Server.py:49-51`) | `Server.py:63` |
| `KeyboardInterrupt` | `Ctrl+C` | `Server.py:102`, `Client.py:72` |
| `EOFError` | `input()` hit end-of-file (piped input ran out) | `Client.py:72` |
| `ConnectionRefusedError` | Nothing listening on that host/port | uncaught — fails fast, which is fine |
| `OSError: [Errno 10048]` | Port still in `TIME_WAIT` | prevented by `SO_REUSEADDR` (`Server.py:74`) |
| `TypeError: a bytes-like object is required, not 'str'` | Forgot `.encode()` before `send` | uncaught — a bug, not a runtime condition |

The `except` clause on `Server.py:63` lists **`OSError` before its subclasses**, which is worth
noticing: `ConnectionError` is itself a subclass of `OSError`, so listing both is slightly
redundant, but it documents intent clearly.

The design principle: **one handler thread's failure must never affect the others.** Each client is
isolated in its own thread with its own `try`/`except`, so if one client sends a malformed header and
crashes its thread, the other clients keep being served. This is called **fault isolation** and is
one of the real advantages of the thread-per-connection model.

---

## 12. Full lifecycle: startup to shutdown

**Server startup** (`Server.py:70-79`)

1. `socket()` — create the socket object.
2. `setsockopt(SO_REUSEADDR, 1)` — permit immediate restart.
3. `bind((HOST, PORT))` — claim `127.0.0.1:65432`.
4. `listen(5)` — begin listening, queue up to 5 pending connections.
5. Print the PASSIVE-state banner and the main thread's name.
6. Enter the `while True` loop and block in `accept()`.

**Per client** (`Server.py:84-101`)

1. `accept()` returns `(conn, addr)`.
2. Allocate a unique `client_id` under `client_count_lock`.
3. Create and start a named daemon thread running `handle_client(conn, addr)`.
4. Log the new thread with its IP, port, and the current `active_count()`.
5. The main thread immediately loops back to `accept()` — **it is free again**.

**Per message** (`Server.py:46-62`) — the 4-step read-header / read-payload / log / reply cycle,
repeated until disconnect.

**Client disconnect** (`Server.py:63-67`)

1. The client's `shutdown(SHUT_WR)` sends a FIN; the client's socket then fully closes.
2. The handler's `recv_exact` gets `b''` → raises `ConnectionError`.
3. Log `disconnected: client closed the connection`.
4. `finally` closes the socket and logs that the thread is exiting.
5. The thread dies and is reclaimed by the OS. **The main thread never notices or cares.**

**Server shutdown** (`Server.py:102-106`) — `Ctrl+C` raises `KeyboardInterrupt` in the main thread
at `accept()`; the socket is closed and the total is reported. Daemon threads do not block exit.

---

## 13. Viva questions and answers

**Q: Why can't you just use `recv(16)`?**
Because TCP is a stream with no message boundaries. A 67-character message would return 16 bytes and
leave 51 in the buffer, corrupting the next read; and a 5-character message would leave the server
blocked forever waiting for bytes that never arrive.

**Q: What does the 3-byte header actually buy you?**
It removes all guessing. The receiver knows the exact byte count in advance, so `recv_exact` can
loop until it has precisely that many bytes. Fixed width (via `zfill`) is what keeps both sides in
sync.

**Q: Why `zfill(3)` and not `zfill(4)`?**
The brief specifies 3, which caps payloads at 999 bytes. `zfill(4)` would allow 9999 at the cost of
one extra byte per message. The cap is a real trade-off, not a free choice.

**Q: What is the maximum message size, and what happens if you exceed it?**
999 bytes. At 1000, `str(1000).zfill(3)` is `"1000"` — four bytes. The server reads only the first 3
(`"100"`), thinks the message is 100 bytes, and desynchronises. `Server.py:50-51` rejects it, and
`Client.py:70` prevents it by truncating.

**Q: Why a thread per connection rather than one thread handling everyone?**
Because a handler thread blocks in `recv()` while waiting for its client. If the main thread did
that, no other client could be accepted. Separating accept from I/O is what makes it concurrent.

**Q: What does the `print_lock` protect, and what would happen without it?**
Terminal output. `print()` is not atomic, so two threads printing simultaneously interleave
mid-string and produce garbled lines like `Client IP=127.0.0.1 Porm: 54069age: hi`.

**Q: Why `acquire()`/`release()` instead of `with lock:`?**
Both are correct; `with` is the more modern idiom and releases automatically. The brief asks for
explicit `acquire()` and `release()`, which makes the locking visible in the code. Either way, a
`try`/`finally` is needed to guarantee release.

**Q: What does `daemon=True` do?**
The thread is killed automatically when the main program exits, so `Ctrl+C` doesn't hang on threads
blocked in `recv()`. The cost is that daemon threads are terminated abruptly, so cleanup may not run.

**Q: Does one client's bad data affect the others?**
No. Each client is isolated in its own thread with its own `try`/`except`, so a malformed header
kills only that thread. This fault isolation is a genuine benefit of the model.

**Q: What is `SO_REUSEADDR` for?**
Lets the server restart immediately instead of failing with `[Errno 10048] Address already in use`
while the previous connection lingers in `TIME_WAIT`.

**Q: What is the difference between the client's port and 65432?**
`65432` is the server's fixed port. The varying port in the log is the client's ephemeral port,
assigned automatically by the OS. `accept()` returns the **client's** address.

**Q: Why does the client need a thread at all?**
The main thread blocks in `input()` while you type. Without `ResponseReceiver`, you would not see a
reply until after you typed your next message.

**Q: What are the real limitations of this design?**
Thread-per-connection costs an OS thread plus ~8 MB stack each, so it does not scale to thousands
of connections. The 3-byte header caps messages at 999 bytes. There is no authentication, no
encryption, and no automatic reconnect.

**Q: What would you use instead for 10,000 clients?**
A fixed thread pool with `concurrent.futures.ThreadPoolExecutor`, or better, an event-driven design
with `asyncio` or `selectors` — one or a few threads multiplexing many sockets with non-blocking I/O.

---

## 14. Experiments to try

Do these in a copy, not the submission. Each one demonstrates a concept from the document.

| # | Change | What to observe |
|---|---|---|
| 1 | Replace `recv_exact(conn, 3)` with `conn.recv(3)` in the server | Usually still works — then send the header one byte at a time from the client and watch it break. This is the point of §10. |
| 2 | Comment out the `acquire()`/`release()` in `log()` | Garbled, interleaved output with 3+ clients. |
| 3 | Delete `thread.start()` at `Server.py:99` | The server accepts the connection and then does nothing — the handler never runs. |
| 4 | Move a `recv()` into the main `while True` loop | Concurrency collapses; a slow client stalls everyone. |
| 5 | Remove `daemon=True` | `Ctrl+C` no longer exits promptly. |
| 6 | Change `HOST` to a different value in only one file | `ConnectionRefusedError` — the two sides must agree. |
| 7 | Change `HEADER_SIZE` to `2` in only one file | Total desync, because the header is no longer fixed width. |
| 8 | Send exactly 999 characters, then 1000 | The 1000 case gets truncated client-side at `Client.py:70`. |
| 9 | Add a `print` *outside* `log()` in `handle_client` | It can interleave with the locked prints — showing the lock only protects code that uses it. |
| 10 | Open 10 clients at once and watch `active threads` | It climbs to 11, then falls back as clients exit. |

---

*Companion to `readme.md` (lab report) and `code-walkthrough.md` (this file).*
