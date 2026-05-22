# Prompt complet – Proiect Retele de Calculatoare, Tema 21 (Distribuirea procesarii)

## Context

Implementeaza un sistem distribuit de procesare in **Python**, conform temei 21 de la cursul Retele de Calculatoare. Proiectul trebuie sa demonstreze comunicare client-server pe socket-uri, distribuire round-robin a task-urilor, executie reala in proces separat si rulare in Docker.

Specificatiile originale se afla in:
- `00-general-specs.md` (cerinte generale)
- `21-distribuirea-procesarii.md` (tema specifica)

## Decizii fixate (varianta simpla default pentru fiecare)

| Decizie | Alegere |
|---|---|
| Limbaj | **Python 3.11+** |
| Transport | **TCP sockets** (socket library standard, fara framework-uri) |
| Framing mesaje | **Length-prefixed** (4 bytes big-endian + payload JSON sau binar) |
| Serializare mesaje de control | **JSON** |
| Transfer cod task | **Bytes raw**, prefixate cu lungime |
| Executie task | `subprocess.run()` pe fisier temporar din `/tmp`, cu `chmod +x` |
| Tip de task suportat | **Script shell sau executabil Linux** (binarul primit e scris pe disk si rulat direct) |
| Raspuns la client emitator | **Sincron** – clientul tine conexiunea deschisa pana primeste exit code-ul |
| Detectare clienti cazuti | **Lazy** – la urmatoarea incercare de trimitere, daca da `ConnectionRefusedError` / timeout, e scos din lista |
| Concurenta server | **threading** (un thread per conexiune) – mai simplu decat asyncio pentru demo |
| Containerizare | **Doar server-ul in container**; clientii ruleaza pe host si se conecteaza la `localhost:PORT` expus de container |
| UI | **Consola** (input / print) |
| Persistenta | **Niciuna** – lista clientilor e in memorie |
| Logging | `print()` cu prefix `[SERVER]` / `[CLIENT-<port>]` |

## Arhitectura

### Componente

1. **`server/server.py`** – server central, ruleaza in Docker
2. **`client/client.py`** – client dual-role (emitator + procesator), ruleaza pe host
3. **`server/Dockerfile`** – imagine Python slim pentru server
4. **`docker-compose.yml`** – orchestrare (doar server)
5. **`README.md`** – instructiuni de rulare
6. **`examples/`** – task-uri de test (ex. `hello.sh`, `fail.sh`)

### Structura repo

```
ReteleDeCalculatoare/
├── server/
│   ├── server.py
│   └── Dockerfile
├── client/
│   └── client.py
├── examples/
│   ├── hello.sh        # exit 0
│   └── fail.sh         # exit 42
├── docker-compose.yml
├── README.md
└── (fisierele de spec existente)
```

## Protocol

### Framing
Fiecare mesaj are forma: `[4 bytes lungime big-endian][payload]`.

### Mesaje de control (JSON)

Toate mesajele JSON au campul `type`. Server-ul si clientul citesc mereu cu length-prefix, parseaza JSON.

**Client → Server: inregistrare**
```json
{ "type": "REGISTER", "processing_port": 6001 }
```
Raspuns:
```json
{ "type": "REGISTER_OK", "client_id": "127.0.0.1:6001" }
```

**Client → Server: deregistrare (la inchidere curata)**
```json
{ "type": "UNREGISTER" }
```

**Client → Server: trimite task**
```json
{ "type": "SUBMIT_TASK", "args": ["arg1", "arg2"], "binary_size": 1234 }
```
Imediat dupa acest JSON (in cadrul aceleiasi conexiuni TCP) se trimite **un al doilea frame** cu cei 1234 bytes raw ai binarului.

Raspuns server (dupa ce a obtinut exit code de la procesatorul ales):
```json
{ "type": "TASK_RESULT", "exit_code": 0, "executed_by": "127.0.0.1:6002" }
```

Sau, in caz de eroare:
```json
{ "type": "TASK_ERROR", "reason": "no active clients" }
```

**Server → Client procesator: cere executie**
```json
{ "type": "EXEC_TASK", "args": ["arg1", "arg2"], "binary_size": 1234 }
```
Urmat de frame-ul cu bytes raw.

**Client procesator → Server: returneaza rezultat**
```json
{ "type": "EXEC_RESULT", "exit_code": 0 }
```

### Porturi
- Server: ascultă pe `5000` (TCP), expus de container pe `localhost:5000`
- Clienti procesatori: ascultă pe porturi date la pornire ca argument CLI (ex. `6001`, `6002`)

## Comportament detaliat

### Server
- Thread principal: `accept()` pe portul 5000, pentru fiecare conexiune lanseaza un thread.
- Structuri partajate (protejate cu `threading.Lock`):
  - `clients: list[tuple[str, int]]` – lista clientilor activi (host, processing_port)
  - `rr_index: int` – contor round-robin
- La `REGISTER`: adauga in lista, raspunde `REGISTER_OK`.
- La `UNREGISTER`: scoate din lista.
- La `SUBMIT_TASK`:
  1. Daca lista goala → `TASK_ERROR` "no active clients".
  2. Altfel, intra in bucla: alege `clients[rr_index % len(clients)]`, incrementeaza `rr_index`.
  3. Deschide socket nou catre clientul ales pe `processing_port`, trimite `EXEC_TASK` + binar, asteapta `EXEC_RESULT`.
  4. Daca conexiunea esueaza (ConnectionRefused, timeout 5s) → scoate clientul din lista, incearca urmatorul.
  5. Daca lista se goleste in timpul incercarilor → `TASK_ERROR`.
  6. Returneaza `TASK_RESULT` cu exit code-ul si cine a executat.

### Client (dual-role)
- Argumente CLI: `python client.py --server-host localhost --server-port 5000 --processing-port 6001`
- La pornire:
  1. Conexiune la server, trimite `REGISTER`, asteapta `REGISTER_OK`. **Mentine aceasta conexiune deschisa** – e folosita pentru toate SUBMIT-urile ulterioare si pentru UNREGISTER la inchidere.
  2. Lanseaza un thread care asculta pe `processing_port` pentru `EXEC_TASK` de la server.
  3. Thread-ul principal intra in bucla de input din consola:
     - `submit <path_la_binar> [args...]` → citeste fisierul, trimite SUBMIT_TASK pe conexiunea persistenta, asteapta TASK_RESULT, printeaza exit code.
     - `quit` → trimite UNREGISTER, inchide.
- Threadul procesator:
  - `accept()` pe `processing_port`, pentru fiecare cerere:
    1. Citeste `EXEC_TASK` + binar.
    2. Scrie binarul in `/tmp/task_<uuid>`, `os.chmod(..., 0o755)`.
    3. `subprocess.run([path, *args], timeout=30)`.
    4. Trimite `EXEC_RESULT` cu `result.returncode`.
    5. Sterge fisierul temporar.
  - Tratare erori: daca executia esueaza/timeout → `exit_code = -1`.

### Tratare erori (rezumat)
- `ConnectionRefusedError`, `socket.timeout`, `BrokenPipeError` la trimiterea spre procesator → scoate din lista, log, incearca urmatorul.
- Binar gol / args lipsa → respinge cu `TASK_ERROR`.
- `KeyboardInterrupt` (Ctrl+C) la client → UNREGISTER + exit curat.
- Ctrl+C la server → inchidere socket, exit.

## Docker

### `server/Dockerfile`
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY server.py .
EXPOSE 5000
CMD ["python", "-u", "server.py"]
```
(Adauga `-u` pentru output nebufferat, important pentru log-uri in `docker compose`.)

### `docker-compose.yml`
```yaml
services:
  server:
    build: ./server
    ports:
      - "5000:5000"
    container_name: dist-server
```

### Rulare
```bash
docker compose up --build
# In alte terminale:
python client/client.py --processing-port 6001
python client/client.py --processing-port 6002
```

## Cerinte de cod

- Comentarii in romana pentru sectiunile cheie.
- Functii scurte, denumiri clare.
- Nu folosi librarii externe in afara de standard library (socket, threading, subprocess, json, struct, uuid, os, sys, argparse).
- Codul trebuie sa fie explicabil linie cu linie la prezentare.

## Scenarii de demo (din spec, sectiunea 4)

Trebuie sa functioneze toate:
1. Pornire server in Docker (`docker compose up --build`).
2. Pornire client 1 (`--processing-port 6001`) – se inregistreaza.
3. Pornire client 2 (`--processing-port 6002`) – se inregistreaza.
4. Din client 1: `submit examples/hello.sh` → executat de client 1 (primul din round-robin), exit 0.
5. Din client 1: `submit examples/hello.sh` din nou → executat de client 2, exit 0.
6. Exit code-ul ajunge inapoi la client 1, e afisat in consola.
7. Client 2 face `quit` – e scos din lista.
8. Din client 1: `submit examples/fail.sh` → executat de client 1 (singurul ramas), exit 42.

## Continut README.md (de generat)

- Descriere scurta
- Cerinte (Docker, Python 3.11+ pe host pentru clienti)
- Comanda de pornire server: `docker compose up --build`
- Comanda de pornire client: `python client/client.py --processing-port <PORT>`
- Comenzi din clientul interactiv: `submit <path> [args...]`, `quit`
- Exemple complete (cu output asteptat)
- Mapare cerinte → cod (ce fisier/functie acopera fiecare cerinta din tema)

## Livrabile finale

1. `server/server.py` – ~150-200 linii
2. `client/client.py` – ~200-250 linii
3. `server/Dockerfile`
4. `docker-compose.yml`
5. `examples/hello.sh`, `examples/fail.sh`
6. `README.md`

## Impartirea pentru prezentare (echipa de 2)

- **Membru A**: server.py, Dockerfile, docker-compose.yml, protocol design, round-robin, tratare clienti cazuti.
- **Membru B**: client.py (ambele roluri), executia in subprocess, integrare cu server, scenarii demo.
