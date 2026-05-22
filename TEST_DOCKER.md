# Walkthrough — Test real cu Docker

Scop: validezi pas cu pas cele 8 scenarii din spec (sectiunea 4) cu serverul rulat in container Docker si 2 clienti rulati pe host. La final ai certitudine ca proiectul functioneaza in conditiile in care va fi evaluat.

## Pregatire (o singura data)

Deschide **3 terminale** in radacina proiectului (`ReteleDeCalculatoare/`):
- **T1** — server (Docker)
- **T2** — client 1 (port 6001)
- **T3** — client 2 (port 6002)

Verifica ca Docker ruleaza:
```bash
docker --version
docker ps
```
A doua comanda trebuie sa nu dea eroare (chiar daca lista e goala).

---

## Scenariul 1 — Pornire server in Docker

**T1:**
```bash
docker compose up --build
```

**Verifica ca vezi:**
```
dist-server  | [SERVER] ascult pe 0.0.0.0:5000
```

Daca prima rulare cere build (downloadeaza `python:3.11-slim`), asteapta sa termine. Lasa T1 in foreground — pe acolo vor curge log-urile serverului.

> Daca portul 5000 e ocupat pe host, editeaza `docker-compose.yml` la `"5050:5000"` si foloseste `--server-port 5050` la clienti.

---

## Scenariul 2 — Pornire 2 clienti procesatori

**T2:**
```bash
python client/client.py --processing-port 6001
```

Trebuie sa vezi:
```
[CLIENT-6001] procesator asculta pe 0.0.0.0:6001
[CLIENT-6001] inregistrat ca <IP_GATEWAY>:6001
[CLIENT-6001] comenzi: submit <path> [args...] | quit
[CLIENT-6001]> 
```

**T3:**
```bash
python client/client.py --processing-port 6002
```

Aceleasi mesaje, cu portul 6002.

---

## Scenariul 3 — Inregistrarea corecta a clientilor

**Verifica in T1 (log-uri server):**
```
[SERVER] conexiune noua de la <IP_GATEWAY>:<port_efemer>
[SERVER] inregistrat <IP_GATEWAY>:6001 (total: 1)
[SERVER] conexiune noua de la <IP_GATEWAY>:<port_efemer>
[SERVER] inregistrat <IP_GATEWAY>:6002 (total: 2)
```

**!!! verificare critica:** `<IP_GATEWAY>` trebuie sa fie un IP de gateway Docker (`172.x.0.1` pe Linux), **NU** `127.0.0.1`. Daca vezi `127.0.0.1`, inseamna ca rutarea nu e corecta — flag pentru debug.

---

## Scenariul 4 — Primul task -> primul client (RR)

**T2:**
```
[CLIENT-6001]> submit examples/hello.sh
```

**Verifica T2:**
```
[CLIENT-6001] TASK_RESULT exit_code=0 executed_by=<IP_GATEWAY>:6001
```

**Verifica T3 (clientul 6001 e si emitator si procesator — task-ul a ajuns la el):**
Hmm, ATENTIE — daca primul task e trimis din T2, **clientul 6001 e si emitatorul si executorul** (round-robin il alege prima oara). Asta inseamna ca log-ul "primit task" va aparea in **T2**, nu T3:
```
[CLIENT-6001] primit task (XX bytes, args=[])
[CLIENT-6001] exit_code=0
```

**Verifica T1:**
```
[SERVER] task executat de <IP_GATEWAY>:6001 -> exit_code=0
```

---

## Scenariul 5 — Al doilea task -> al doilea client (RR)

**T2:**
```
[CLIENT-6001]> submit examples/hello.sh
```

**Verifica T2:**
```
[CLIENT-6001] TASK_RESULT exit_code=0 executed_by=<IP_GATEWAY>:6002
```

**Verifica T3 (de data asta procesatorul 6002 a primit task-ul):**
```
[CLIENT-6002] primit task (XX bytes, args=[])
[CLIENT-6002] exit_code=0
```

**!!! verificare critica:** `executed_by` trebuie sa fie `:6002` acum, nu `:6001`. Asta demonstreaza round-robin.

---

## Scenariul 6 — Exit code returnat la emitator

Deja validat in scenariile 4 si 5: liniile `TASK_RESULT exit_code=0 executed_by=...` afisate in T2 sunt exact dovada ca exit code-ul ajunge inapoi la clientul solicitant.

Bonus: testeaza cu un task care esueaza:

**T2:**
```
[CLIENT-6001]> submit examples/fail.sh
[CLIENT-6001] TASK_RESULT exit_code=42 executed_by=<IP_GATEWAY>:6001
```

`exit_code=42` confirma ca exit code-ul real al procesului ajunge corect prin tot lantul.

---

## Scenariul 7 — Inchiderea unui client si eliminarea din lista

**T3:**
```
[CLIENT-6002]> quit
[CLIENT-6002] bye
```

**Verifica T1:**
```
[SERVER] sters client <IP_GATEWAY>:6002
```

---

## Scenariul 8 — Submit dupa eliminare

**T2:**
```
[CLIENT-6001]> submit examples/hello.sh
[CLIENT-6001]> submit examples/fail.sh
```

**Verifica T2:**
Ambele rezultate au `executed_by=<IP_GATEWAY>:6001`, pentru ca 6002 nu mai e in lista. Round-robin nu mai are pe cine sa aleaga in afara de 6001.

**Verifica T1:**
```
[SERVER] task executat de <IP_GATEWAY>:6001 -> exit_code=0
[SERVER] task executat de <IP_GATEWAY>:6001 -> exit_code=42
```

---

## BONUS — Test crash brutal (validare lazy detection)

Acopera cerinta "client cazut detectat lazy" mai bine decat scenariul 7, pentru ca aici clientul nu mai apuca sa trimita UNREGISTER.

**Repornire curata:** Ctrl+C in T2 si T3 (sau `quit`), apoi reporneste ambii clienti exact ca la scenariul 2.

**Gaseste PID-ul clientului 6002 (intr-un al 4-lea terminal sau in T3 daca ai pornit cu `&`):**
```bash
pgrep -af "processing-port 6002"
```

**Omoara-l brutal:**
```bash
kill -9 <PID>
```

T3 dispare instant, **fara** sa fi trimis UNREGISTER.

**T2:**
```
[CLIENT-6001]> submit examples/hello.sh
```

**Verifica T1:**
```
[SERVER] procesator <IP_GATEWAY>:6002 indisponibil (ConnectionRefusedError...), incerc altul
[SERVER] sters client <IP_GATEWAY>:6002
[SERVER] task executat de <IP_GATEWAY>:6001 -> exit_code=0
```

> NOTA: Datorita celui de-al doilea nivel de detectie (close al conexiunii de control la moartea procesului), serverul s-ar putea sa fi sters deja clientul 6002 inainte de SUBMIT. Atunci log-ul "indisponibil" nu mai apare — dar functionalitatea e identica si tot demonstreaza ca crash-ul e tratat corect. Daca vrei sa fortezi traseul lazy, suspenda procesul cu `kill -STOP <PID>` in loc de `-9` (procesul nu mai raspunde, dar conexiunea de control ramane deschisa) — atunci serverul va incerca sa dispatch-uiasca, va da timeout, va scoate clientul. Refacut: `kill -CONT <PID>` la final.

---

## Oprire curata

In T2 si T3: `quit` sau Ctrl+C.
In T1: Ctrl+C, apoi:
```bash
docker compose down
```

---

## Checklist final (de bifat dupa rulare)

- [ ] `docker compose up --build` porneste serverul fara erori
- [ ] Ambii clienti se inregistreaza si apar in log cu IP de gateway, nu `127.0.0.1`
- [ ] Primul task -> client 6001 (executed_by:6001)
- [ ] Al doilea task -> client 6002 (executed_by:6002) — demonstreaza RR
- [ ] Exit code 0 si exit code 42 trec corect prin tot lantul
- [ ] `quit` scoate clientul din lista (`[SERVER] sters client ...`)
- [ ] Dupa scoatere, task-urile noi merg toate la clientul ramas
- [ ] (Bonus) Crash cu `kill -9` e detectat si clientul e scos
- [ ] `docker compose down` curata totul

Daca toate sunt bifate, esti gata pentru prezentare.

---

## Probleme posibile si rezolvari

| Simptom | Cauza | Rezolvare |
| --- | --- | --- |
| `port already in use` la `docker compose up` | Alta aplicatie tine 5000 | Schimba in `docker-compose.yml` la `"5050:5000"` si la clienti `--server-port 5050` |
| Clientul nu se conecteaza: `Connection refused` | Server nu a pornit complet | Asteapta linia `[SERVER] ascult pe 0.0.0.0:5000` in T1 |
| Server nu poate dispatch-ui catre client (timeout) | Clientul asculta pe `127.0.0.1` in loc de `0.0.0.0`, sau firewall blocheaza | Codul deja face `bind("0.0.0.0", ...)`. Verifica ufw/iptables daca esti pe Linux cu firewall activ |
| `executed_by` arata `127.0.0.1` in loc de gateway IP | Rulezi serverul fara Docker (direct cu `python server.py`) | Asta e normal. In Docker iei IP de gateway |
| Pe Mac/Windows clientii nu pot fi contactati de server | Docker Desktop NAT diferit fata de Linux bridge | Pe Mac/Windows ar putea fi necesar `--network host` (nu functioneaza pe Mac in unele versiuni) sau sa rulezi totul direct fara Docker pentru demo |
