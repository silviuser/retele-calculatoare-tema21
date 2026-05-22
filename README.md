# Tema 21 — Distribuirea procesarii

Sistem distribuit client-server implementat in **Python 3.11+**, conform temei 21
de la cursul Retele de Calculatoare. Serverul ruleaza intr-un container Docker si
distribuie task-uri primite de la clienti, round-robin, catre procesatori. Fiecare
client este *dual-role*: trimite task-uri pentru cluster si executa task-uri venite
de la server intr-un proces separat.

## Cerinte

- Docker + Docker Compose (pentru server)
- Python 3.11+ pe host (pentru clienti)
- Sistem Linux/macOS (clientii executa binare/scripturi POSIX in `/tmp`)

## Pornire rapida

Server (in radacina proiectului):

```bash
docker compose up --build
```

Server-ul asculta pe `localhost:5000`.

In alte terminale, doi clienti:

```bash
python client/client.py --processing-port 6001
python client/client.py --processing-port 6002
```

Comenzi disponibile in clientul interactiv:

- `submit <path> [args...]` — trimite un binar/script pentru executie in cluster
- `quit` — se dezinregistreaza si iese

## Exemplu complet

Terminal A (server):
```
$ docker compose up --build
dist-server  | [SERVER] ascult pe 0.0.0.0:5000
dist-server  | [SERVER] inregistrat 172.18.0.1:6001 (total: 1)
dist-server  | [SERVER] inregistrat 172.18.0.1:6002 (total: 2)
```

Terminal B (client 1):
```
$ python client/client.py --processing-port 6001
[CLIENT-6001] procesator asculta pe 0.0.0.0:6001
[CLIENT-6001] inregistrat ca 172.18.0.1:6001
[CLIENT-6001]> submit examples/hello.sh
hello from task, args:
[CLIENT-6001] TASK_RESULT exit_code=0 executed_by=172.18.0.1:6001
[CLIENT-6001]> submit examples/hello.sh
[CLIENT-6001] TASK_RESULT exit_code=0 executed_by=172.18.0.1:6002
[CLIENT-6001]> submit examples/fail.sh
[CLIENT-6001] TASK_RESULT exit_code=42 executed_by=172.18.0.1:6001
```

Terminal C (client 2):
```
$ python client/client.py --processing-port 6002
[CLIENT-6002] procesator asculta pe 0.0.0.0:6002
[CLIENT-6002] inregistrat ca 172.18.0.1:6002
[CLIENT-6002] primit task (97 bytes, args=[])
[CLIENT-6002] exit_code=0
[CLIENT-6002]> quit
[CLIENT-6002] bye
```

> Nota: `executed_by` afiseaza IP-ul vazut de server. Daca serverul ruleaza in
> Docker, peer-ul este IP-ul gateway-ului bridge (ex. `172.18.0.1`), nu `127.0.0.1`.

## Protocol

Framing: fiecare mesaj are forma `[4 bytes lungime big-endian][payload]`. Mesajele
de control sunt JSON; binarul task-ului este un frame raw.

| Mesaj | Sens | Payload |
| --- | --- | --- |
| `REGISTER` | client → server | `{"type":"REGISTER","processing_port":6001}` |
| `REGISTER_OK` | server → client | `{"type":"REGISTER_OK","client_id":"..."}` |
| `UNREGISTER` | client → server | `{"type":"UNREGISTER"}` |
| `SUBMIT_TASK` | client → server | `{"type":"SUBMIT_TASK","args":[...],"binary_size":N}` + frame de N bytes |
| `TASK_RESULT` | server → client | `{"type":"TASK_RESULT","exit_code":N,"executed_by":"..."}` |
| `TASK_ERROR` | server → client | `{"type":"TASK_ERROR","reason":"..."}` |
| `EXEC_TASK` | server → procesator | `{"type":"EXEC_TASK","args":[...],"binary_size":N}` + frame de N bytes |
| `EXEC_RESULT` | procesator → server | `{"type":"EXEC_RESULT","exit_code":N}` |

REGISTER si SUBMIT_TASK-urile ulterioare merg pe **aceeasi** conexiune TCP
persistenta. EXEC_TASK se face pe o conexiune noua (server → procesator).

## Mapare cerinte (tema 21) → cod

| Cerinta | Locatie |
| --- | --- |
| Server asculta pe TCP, mentine lista clienti | `server/server.py` — `main()`, `clients` |
| Server concurent (thread-per-conn) | `handle_connection()` lansat din `main()` |
| Inregistrare client | `client.py` — `main()` (REGISTER) ↔ `server.py` — `handle_connection()` branch `REGISTER` |
| Deregistrare la inchidere curata | `client.py` — `finally` din `main()` ↔ `server.py` branch `UNREGISTER` |
| Trimitere task (binar + args) | `client.py` — `cmd_submit()` ↔ `server.py` branch `SUBMIT_TASK` |
| Round-robin | `server.py` — `pick_next_processor()` (`rr_index` + `state_lock`) |
| Transfer binar (length-prefixed frame raw) | `send_frame` / `recv_frame` in ambele fisiere |
| Executie reala in proces separat | `client.py` — `execute_task()` foloseste `subprocess.run` |
| Returnare exit code | `EXEC_RESULT` → `handle_submit()` → `TASK_RESULT` |
| Detectie clienti cazuti (lazy) | `server.py` — `handle_submit()` try/except + `remove_client()` |
| Eroare "no active clients" | `server.py` — `handle_submit()` returneaza `TASK_ERROR` |
| Server in Docker | `server/Dockerfile`, `docker-compose.yml` |

## Scenariile demo (sectiunea 4 din spec)

| # | Scenariu | Status |
| --- | --- | --- |
| 1 | Pornire server in Docker | `docker compose up --build` |
| 2 | Pornire 2 clienti | comenzile din "Pornire rapida" |
| 3 | Inregistrare clienti | log `[SERVER] inregistrat ... (total: 2)` |
| 4 | Submit task → primul client | `submit examples/hello.sh` din client 1 → `executed_by=...:6001` |
| 5 | Submit al doilea → al doilea client (RR) | un nou `submit` → `executed_by=...:6002` |
| 6 | Exit code returnat clientului solicitant | `TASK_RESULT exit_code=...` afisat in clientul emitator |
| 7 | Inchidere client → eliminat din lista | `quit` in client 2 → log `[SERVER] sters client ...:6002` |
| 8 | Submit dupa eliminare | `submit examples/fail.sh` → `exit_code=42 executed_by=...:6001` |

Toate au fost validate cu un harness automat de smoke-test (vezi sectiunea
"Testare").

## Testare

Daca nu vrei sa pornesti Docker pentru un test rapid, poti rula serverul direct:

```bash
PORT=5050 python server/server.py
python client/client.py --server-port 5050 --processing-port 6101
python client/client.py --server-port 5050 --processing-port 6102
```

## Tratare erori

- `no active clients` — `TASK_ERROR` daca lista e goala la momentul submit
- procesator indisponibil — server prinde `ConnectionRefusedError`/`timeout`,
  scoate clientul din lista si incearca urmatorul
- task gol sau binary_size invalid — `TASK_ERROR` cu motiv
- crash al unui client (fara UNREGISTER) — detectat lazy la urmatorul dispatch
- `Ctrl+C` in client — UNREGISTER + iesire curata
- `Ctrl+C` in server — inchidere socket si exit

## Structura repo

```
ReteleDeCalculatoare/
├── server/
│   ├── server.py        # ~180 linii
│   └── Dockerfile
├── client/
│   └── client.py        # ~220 linii
├── examples/
│   ├── hello.sh         # exit 0
│   └── fail.sh          # exit 42
├── docker-compose.yml
└── README.md
```

## Impartirea pentru prezentare (echipa de 2)

- **Membru A** — `server/server.py`, `Dockerfile`, `docker-compose.yml`,
  protocol, round-robin, tratare clienti cazuti.
- **Membru B** — `client/client.py` (ambele roluri), executia in subprocess,
  integrare cu serverul, scenarii demo.
