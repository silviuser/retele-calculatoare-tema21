"""Server central pentru distribuirea procesarii (tema 21).

Ruleaza intr-un container Docker, asculta pe portul 5000 (TCP).
Mentine o lista de clienti activi (host, processing_port) si distribuie
task-urile primite round-robin catre procesatori.

Protocol:
  - framing: 4 bytes big-endian (lungime) + payload
  - mesajele de control sunt JSON, binarul task-ului este un frame raw
  - un client trimite REGISTER pe o conexiune si o tine deschisa pentru SUBMIT-uri ulterioare;
    cand vrea sa iasa curat trimite UNREGISTER pe aceeasi conexiune
  - pentru fiecare SUBMIT_TASK, serverul deschide o conexiune noua catre procesatorul ales,
    asteapta sincron EXEC_RESULT si raspunde TASK_RESULT pe conexiunea originala
"""

import json
import os
import socket
import struct
import sys
import threading

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "5000"))

# Stare partajata, protejata de state_lock.
clients = []          # list[tuple[str, int]]: (host, processing_port)
rr_index = 0          # contor round-robin
state_lock = threading.Lock()


# --- I/O cu framing ----------------------------------------------------------

def recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Citeste exact n bytes sau ridica exceptie daca conexiunea se inchide."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("conexiune inchisa de peer")
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock: socket.socket) -> bytes:
    """Citeste un frame: 4 bytes lungime big-endian + payload."""
    header = recv_exactly(sock, 4)
    (length,) = struct.unpack(">I", header)
    return recv_exactly(sock, length)


def send_frame(sock: socket.socket, payload: bytes) -> None:
    """Trimite un frame length-prefixed."""
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def send_json(sock: socket.socket, obj: dict) -> None:
    send_frame(sock, json.dumps(obj).encode("utf-8"))


def recv_json(sock: socket.socket) -> dict:
    return json.loads(recv_frame(sock).decode("utf-8"))


# --- Distribuire task --------------------------------------------------------

def dispatch_to_processor(host: str, port: int, args: list, binary: bytes) -> int:
    """Trimite EXEC_TASK catre un procesator si returneaza exit code-ul primit.

    Ridica exceptie daca procesatorul e indisponibil sau raspunde anormal."""
    s = socket.create_connection((host, port), timeout=5)
    try:
        send_json(s, {"type": "EXEC_TASK", "args": args, "binary_size": len(binary)})
        send_frame(s, binary)
        # executia pe procesator are timeout 30s, asteptam ceva mai mult
        s.settimeout(60)
        result = recv_json(s)
        if result.get("type") != "EXEC_RESULT":
            raise ConnectionError(f"raspuns neasteptat: {result}")
        return int(result.get("exit_code", -1))
    finally:
        s.close()


def remove_client(target: tuple) -> None:
    """Scoate un client din lista (sub lock)."""
    with state_lock:
        if target in clients:
            clients.remove(target)
            print(f"[SERVER] sters client {target[0]}:{target[1]}", flush=True)


def pick_next_processor():
    """Alege urmatorul procesator (round-robin) sau None daca lista e goala."""
    global rr_index
    with state_lock:
        if not clients:
            return None
        idx = rr_index % len(clients)
        target = clients[idx]
        rr_index += 1
        return target


def handle_submit(args: list, binary: bytes) -> tuple:
    """Distribuie un task. Returneaza (exit_code, executed_by) sau (None, motiv)."""
    # Bucla de retry: daca procesatorul ales e indisponibil, il scoatem si trecem mai departe.
    while True:
        target = pick_next_processor()
        if target is None:
            return None, "no active clients"
        host, port = target
        try:
            exit_code = dispatch_to_processor(host, port, args, binary)
            print(f"[SERVER] task executat de {host}:{port} -> exit_code={exit_code}", flush=True)
            return exit_code, f"{host}:{port}"
        except (socket.timeout, ConnectionError, OSError) as e:
            print(f"[SERVER] procesator {host}:{port} indisponibil ({e}), incerc altul", flush=True)
            remove_client(target)
            # bucla continua si alege altul


# --- Handler conexiune client ------------------------------------------------

def handle_connection(conn: socket.socket, addr: tuple) -> None:
    """Trateaza o conexiune persistenta cu un client (REGISTER + SUBMIT-uri)."""
    print(f"[SERVER] conexiune noua de la {addr[0]}:{addr[1]}", flush=True)
    registered_entry = None  # (host, processing_port) daca s-a inregistrat
    try:
        while True:
            msg = recv_json(conn)
            mtype = msg.get("type")

            if mtype == "REGISTER":
                port = int(msg["processing_port"])
                # Clientul poate trimite explicit hostname-ul la care serverul sa se conecteze
                # inapoi (ex. "host.docker.internal" pe Docker Desktop pentru Windows/Mac,
                # unde peer IP-ul = VM-ul Docker, nu host-ul real).
                # Daca nu trimite, cadem pe peer IP (functioneaza pe Linux Docker bridge).
                host = msg.get("callback_host") or addr[0]
                registered_entry = (host, port)
                with state_lock:
                    if registered_entry not in clients:
                        clients.append(registered_entry)
                client_id = f"{host}:{port}"
                send_json(conn, {"type": "REGISTER_OK", "client_id": client_id})
                print(f"[SERVER] inregistrat {client_id} (total: {len(clients)})", flush=True)

            elif mtype == "UNREGISTER":
                if registered_entry is not None:
                    remove_client(registered_entry)
                    registered_entry = None
                # iesim din loop, conexiunea se inchide
                break

            elif mtype == "SUBMIT_TASK":
                args = msg.get("args", []) or []
                binary_size = int(msg.get("binary_size", 0))
                if binary_size <= 0:
                    send_json(conn, {"type": "TASK_ERROR", "reason": "binary gol"})
                    continue
                binary = recv_frame(conn)
                if len(binary) != binary_size:
                    send_json(conn, {
                        "type": "TASK_ERROR",
                        "reason": f"binary_size mismatch ({len(binary)} vs {binary_size})",
                    })
                    continue
                exit_code, info = handle_submit(args, binary)
                if exit_code is None:
                    send_json(conn, {"type": "TASK_ERROR", "reason": info})
                else:
                    send_json(conn, {
                        "type": "TASK_RESULT",
                        "exit_code": exit_code,
                        "executed_by": info,
                    })

            else:
                send_json(conn, {"type": "TASK_ERROR", "reason": f"tip necunoscut: {mtype}"})

    except ConnectionError as e:
        print(f"[SERVER] conexiune intrerupta de la {addr}: {e}", flush=True)
    except Exception as e:
        print(f"[SERVER] eroare in handler {addr}: {e}", flush=True)
    finally:
        # daca clientul a picat fara UNREGISTER, scoatem inregistrarea
        if registered_entry is not None:
            remove_client(registered_entry)
        try:
            conn.close()
        except OSError:
            pass


# --- Main loop --------------------------------------------------------------

def main() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, PORT))
    s.listen(16)
    print(f"[SERVER] ascult pe {HOST}:{PORT}", flush=True)
    try:
        while True:
            conn, addr = s.accept()
            t = threading.Thread(target=handle_connection, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("[SERVER] inchidere la Ctrl+C", flush=True)
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
