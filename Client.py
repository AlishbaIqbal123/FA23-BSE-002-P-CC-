import socket
import threading

HOST = '127.0.0.1'
PORT = 65432
HEADER_SIZE = 3
MAX_PAYLOAD = 999

# Shared lock so the receiver thread and the input thread never interleave output
print_lock = threading.Lock()


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
            raise ConnectionError('server closed the connection')
        buffer += chunk
    return buffer


def send_message(conn, message):
    # Header protocol: 3-byte length prefix, then the payload
    conn.sendall(str(len(message)).zfill(HEADER_SIZE).encode() + message)


def receive_responses(conn):
    """Background thread: keeps reading server replies until the socket closes."""
    thread_name = threading.current_thread().name
    try:
        while True:
            header = recv_exact(conn, HEADER_SIZE)
            msg_length = int(header.decode())
            data = recv_exact(conn, msg_length)
            log(f'SERVER REPLY  -> {data.decode()}')
    except (ConnectionError, OSError, UnicodeDecodeError, ValueError):
        log(f'[{thread_name}] Server closed the connection.')


def main():
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((HOST, PORT))
    log(f'Connected to server at {HOST}:{PORT}  (type "exit" or press Ctrl+C to quit)\n')

    receiver = threading.Thread(target=receive_responses, args=(client_socket,),
                                name='ResponseReceiver', daemon=True)
    receiver.start()

    try:
        while True:
            message = input('You  -> ')
            if message.strip().lower() in ('exit', 'quit', 'bye'):
                log('Closing connection. Goodbye!')
                break
            if not message:
                continue

            # 3-byte header caps the payload at 999 characters
            message = message[:MAX_PAYLOAD]
            send_message(client_socket, message.encode())
    except (KeyboardInterrupt, EOFError):
        log('\nInterrupted by user. Closing connection.')
    finally:
        # Half-close first so the server sees a clean EOF instead of a reset
        try:
            client_socket.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        receiver.join(timeout=2)
        client_socket.close()


if __name__ == '__main__':
    main()
