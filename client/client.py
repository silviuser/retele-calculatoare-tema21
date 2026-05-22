"""Client dual-role pentru distribuirea procesarii (tema 21).

Acelasi proces:
  - se inregistreaza la server pe o conexiune persistenta (folosita si pentru SUBMIT-uri),
  - asculta pe processing_port pentru cereri EXEC_TASK de la server,
  - ofera o consola interactiva: `submit <path> [args...]`, `quit`.

Rulare:
    python client/client.py --processing-port 6001
    python client/client.py --server-host localhost --server-port 5000 --processing-port 6002
"""

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import uuid

EXEC_TIMEOUT = 30  # secunde, timeout pentru rularea task-ului


# --- I/O cu framing (identic cu serverul) ------------------------------------

def recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("conexiune inchisa de peer")
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock: socket.socket) -> bytes:
    header = recv_exactly(sock, 4)
    (length,) = struct.unpack(">I", header)
    return recv_exactly(sock, length)


def send_frame(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def send_json(sock: socket.socket, obj: dict) -> None:
    send_frame(sock, json.dumps(obj).encode("utf-8"))


def recv_json(sock: socket.socket) -> dict:
    return json.loads(recv_frame(sock).decode("utf-8"))


# --- Executia task-ului local -----------------------------------------------

def execute_task(binary: bytes, args: list, log_prefix: str) -> int:
    """Scrie binarul in /tmp, il face executabil si il ruleaza ca proces separat.
    Returneaza exit code-ul real al procesului, sau -1 in caz de eroare/timeout."""
    path = f"/tmp/task_{uuid.uuid4().hex}"
    try:
        with open(path, "wb") as f:
            f.write(binary)
        os.chmod(path, 0o755)
        try:
            # subprocess.run -> proces complet separat (nu thread al server-ului)
            result = subprocess.run([path, *args], timeout=EXEC_TIMEOUT)
            return result.returncode
        except subprocess.TimeoutExpired:
            print(f"{log_prefix} task timeout dupa {EXEC_TIMEOUT}s", flush=True)
            return -1
        except OSError as e:
            print(f"{log_prefix} eroare exec: {e}", flush=True)
            return -1
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --- Threadul procesator (asculta cereri de la server) -----------------------

def processor_handler(conn: socket.socket, addr: tuple, log_prefix: str) -> None:
    """Trateaza o singura cerere EXEC_TASK venita de la server."""
    try:
        req = recv_json(conn)
        if req.get("type") != "EXEC_TASK":
            print(f"{log_prefix} cerere necunoscuta de la {addr}: {req}", flush=True)
            return
        args = req.get("args", []) or []
        binary_size = int(req.get("binary_size", 0))
        binary = recv_frame(conn)
        if len(binary) != binary_size:
            print(f"{log_prefix} binary size mismatch ({len(binary)} vs {binary_size})", flush=True)
            send_json(conn, {"type": "EXEC_RESULT", "exit_code": -1})
            return
        print(f"{log_prefix} primit task ({binary_size} bytes, args={args})", flush=True)
        exit_code = execute_task(binary, args, log_prefix)
        print(f"{log_prefix} exit_code={exit_code}", flush=True)
        send_json(conn, {"type": "EXEC_RESULT", "exit_code": exit_code})
    except (ConnectionError, OSError) as e:
        print(f"{log_prefix} eroare procesator: {e}", flush=True)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def processor_loop(processing_port: int, log_prefix: str) -> None:
    """Bucla principala a procesatorului: accept + thread pe cerere."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # bind pe 0.0.0.0 ca sa fim accesibili si din container (prin gateway-ul Docker)
    s.bind(("0.0.0.0", processing_port))
    s.listen(8)
    print(f"{log_prefix} procesator asculta pe 0.0.0.0:{processing_port}", flush=True)
    while True:
        try:
            conn, addr = s.accept()
        except OSError:
            return  # socketul a fost inchis
        t = threading.Thread(
            target=processor_handler,
            args=(conn, addr, log_prefix),
            daemon=True,
        )
        t.start()


# --- Consola interactiva (rol de emitator) -----------------------------------

def cmd_submit(control: socket.socket, parts: list, log_prefix: str) -> None:
    """Trimite un SUBMIT_TASK pe conexiunea persistenta si asteapta TASK_RESULT."""
    if len(parts) < 2:
        print(f"{log_prefix} usage: submit <path> [args...]")
        return
    path = parts[1]
    args = parts[2:]
    try:
        with open(path, "rb") as f:
            binary = f.read()
    except OSError as e:
        print(f"{log_prefix} nu pot citi {path}: {e}")
        return
    if not binary:
        print(f"{log_prefix} fisier gol, abandonez")
        return

    send_json(control, {
        "type": "SUBMIT_TASK",
        "args": args,
        "binary_size": len(binary),
    })
    send_frame(control, binary)
    response = recv_json(control)
    rtype = response.get("type")
    if rtype == "TASK_RESULT":
        print(f"{log_prefix} TASK_RESULT exit_code={response['exit_code']} "
              f"executed_by={response['executed_by']}")
    elif rtype == "TASK_ERROR":
        print(f"{log_prefix} TASK_ERROR: {response.get('reason')}")
    else:
        print(f"{log_prefix} raspuns neasteptat: {response}")


def repl(control: socket.socket, log_prefix: str) -> None:
    """Bucla de input. Iese la `quit`, EOF sau Ctrl+C."""
    print(f"{log_prefix} comenzi: submit <path> [args...] | quit")
    while True:
        try:
            line = input(f"{log_prefix}> ").strip()
        except EOFError:
            print()
            return
        if not line:
            continue
        parts = line.split()
        cmd = parts[0]
        if cmd == "submit":
            try:
                cmd_submit(control, parts, log_prefix)
            except ConnectionError as e:
                print(f"{log_prefix} conexiune cu serverul pierduta: {e}")
                return
        elif cmd == "quit":
            return
        else:
            print(f"{log_prefix} comanda necunoscuta: {cmd}")


# --- Main --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Client dual-role pentru tema 21")
    parser.add_argument("--server-host", default="localhost", help="hostul serverului central")
    parser.add_argument("--server-port", type=int, default=5000, help="portul serverului central")
    parser.add_argument("--processing-port", type=int, required=True,
                        help="portul pe care acest client asculta cereri de procesare")
    parser.add_argument("--callback-host", default="host.docker.internal",
                        help=("hostname-ul la care serverul (din container) sa se conecteze "
                              "inapoi pentru EXEC_TASK. Default 'host.docker.internal' "
                              "merge pe Docker Desktop (Windows/Mac) si pe Linux Docker "
                              "(cu extra_hosts host-gateway). Pentru rulare fara Docker, "
                              "foloseste 127.0.0.1."))
    args = parser.parse_args()

    log_prefix = f"[CLIENT-{args.processing_port}]"

    # 1. Pornim threadul procesator inainte de REGISTER, ca serverul sa ne poata
    #    contacta imediat dupa ce ne-a vazut in lista.
    proc_thread = threading.Thread(
        target=processor_loop,
        args=(args.processing_port, log_prefix),
        daemon=True,
    )
    proc_thread.start()

    # 2. Conexiune persistenta cu serverul + REGISTER.
    try:
        control = socket.create_connection((args.server_host, args.server_port), timeout=5)
    except OSError as e:
        print(f"{log_prefix} nu pot conecta la {args.server_host}:{args.server_port}: {e}")
        return 1
    control.settimeout(None)  # apoi blocant; SUBMIT poate dura

    send_json(control, {
        "type": "REGISTER",
        "processing_port": args.processing_port,
        "callback_host": args.callback_host,
    })
    try:
        response = recv_json(control)
    except (ConnectionError, OSError) as e:
        print(f"{log_prefix} eroare la REGISTER: {e}")
        return 1
    if response.get("type") != "REGISTER_OK":
        print(f"{log_prefix} REGISTER esuat: {response}")
        return 1
    print(f"{log_prefix} inregistrat ca {response['client_id']}")

    # 3. Consola interactiva.
    try:
        repl(control, log_prefix)
    except KeyboardInterrupt:
        print()
    finally:
        # 4. UNREGISTER curat + inchidere.
        try:
            send_json(control, {"type": "UNREGISTER"})
        except OSError:
            pass
        try:
            control.close()
        except OSError:
            pass
        print(f"{log_prefix} bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
