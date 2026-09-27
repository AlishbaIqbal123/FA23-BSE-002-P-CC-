import socket
import threading

HOST = '127.0.0.1'
PORT = 65432
HEADER_SIZE = 3
BACKLOG = 5
MAX_PAYLOAD = 999
REPLY_PREFIX = b'Server received: '

# Shared lock: only one thread prints at a time so lines never interleave
print_lock = threading.Lock()
client_count_lock = threading.Lock()
client_count = 0


def log(message):
    print_lock.acquire()
    try:
        print(message, flush=True)
    finally:
        print_lock.release()


def recv_exact(conn, n):
    # TCP is a byte stream, so recv(n) can return FEWER than n bytes and the
    # 3-byte header can be split across packets. Loop until all n bytes arrive.
    buffer = b''
    while len(buffer) < n:
        chunk = conn.recv(n - len(buffer))
        if not chunk:
            raise ConnectionError('client closed the connection')
        buffer += chunk
    return buffer


def send_message(conn, message):
    # Header protocol: 3-byte length prefix, then the payload
    conn.sendall(str(len(message)).zfill(HEADER_SIZE).encode() + message)


def handle_client(conn, addr):
    thread_name = threading.current_thread().name
    ip, port = addr[0], addr[1]
    try:
        while True:
            # Step 1: read exactly 3 header bytes -> payload length
            header = recv_exact(conn, HEADER_SIZE)
            msg_length = int(header.decode())
            if not 0 < msg_length <= MAX_PAYLOAD:
                raise ValueError(f'invalid payload length {msg_length}')

            # Step 2: read exactly that many payload bytes
            data = recv_exact(conn, msg_length)

            log(f'[{thread_name}] Client IP={ip} Port={port} | header={header.decode()} '
                f'({msg_length} bytes) | message: {data.decode()}')

            reply = REPLY_PREFIX + data
            if len(reply) > MAX_PAYLOAD:
                reply = reply[:MAX_PAYLOAD]
            send_message(conn, reply)
    except (ConnectionError, OSError, UnicodeDecodeError, ValueError) as e:
        log(f'[{thread_name}] Client IP={ip} Port={port} | disconnected: {e}')
    finally:
        conn.close()
        log(f'[{thread_name}] Client IP={ip} Port={port} | connection closed, thread exiting')


def main():
    global client_count

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST, PORT))
    server_socket.listen(BACKLOG)

    log(f'Server is in PASSIVE state, listening for connections on {HOST}:{PORT}')
    log(f'Main thread: {threading.current_thread().name} | waiting to accept...\n')

    try:
        while True:
            # Main thread only accepts; each client is served by its own thread
            conn, addr = server_socket.accept()

            client_count_lock.acquire()
            try:
                client_count += 1
                client_id = client_count
            finally:
                client_count_lock.release()

            thread = threading.Thread(
                target=handle_client,
                args=(conn, addr),
                name=f'ClientHandler-{client_id}',
                daemon=True,
            )
            thread.start()
            log(f'[{thread.name}] STARTED for Client IP={addr[0]} Port={addr[1]} '
                f'| active threads: {threading.active_count()}')
    except KeyboardInterrupt:
        log('\nShutting down server.')
    finally:
        server_socket.close()
        log(f'Server socket closed. Total clients served: {client_count}')


if __name__ == '__main__':
    main()
