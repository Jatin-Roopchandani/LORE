# Self-hosting LORE

Run LORE on your own machine (a desktop, a home server, a NAS, a small VPS)
for yourself and the people you add. Each person has their own private
library and reading progress. You choose the AI model: a cloud provider with
your key, or a model you run yourself, such as a Qwen model on llama.cpp.

Everything runs in Docker. Nothing is sent anywhere except to the AI provider
you choose: no sign-up, no mail service, no outside login.

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Choosing the AI model](#choosing-the-ai-model) (with a [llama.cpp
  example](#a-model-on-your-own-machine-llamacpp))
- [Adding people](#adding-people)
- [Upgrading](#upgrading)
- [Backups](#backups-and-restore)
- [Opening it beyond this computer](#opening-it-beyond-this-computer)
- [Settings](#settings)
- [Troubleshooting](#troubleshooting)

## Requirements

- **Docker** with Compose v2 (`docker compose version`): Docker Desktop on
  macOS or Windows, or Docker Engine on Linux. linux/amd64 and linux/arm64
  (a Raspberry Pi 5, an Apple Silicon Mac, an Ampere server) are both built.
- **Memory:** 4 GB free for LORE itself (the web app, the book processor and
  the embedding model), 8 GB to be comfortable. A local language model needs
  its own memory on top: a 28-30B model at 4-bit is about 17-20 GB of GPU or
  system memory.
- **Disk:** about 10 GB for the images (LORE's is about 1.2 GB; the
  `ollama/ollama` image is about 8.5 GB, most of it GPU libraries) and the
  embedding model, plus roughly 50-200 MB per processed book. With your own
  Ollama for embeddings (`LORE_OLLAMA_HOST`), the bundled image is still
  pulled for the one-shot model download.

## Quick start

```bash
mkdir lore && cd lore
curl -fsSLO https://raw.githubusercontent.com/Jatin-Roopchandani/LORE/main/docker-compose.yml
docker compose up -d
docker compose logs lore | grep "setup code"
```

Open **http://localhost:8080**, enter the setup code from the log (three
groups of four, like `K7QM-3XRT-9WHP`) and create the admin account (your
email and a password). That's all the setup there is: every page waits for
the admin, and only someone who can read the server's log can create it.
The code works once and is never printed again after that.

The first start downloads the images and the embedding model
(`nomic-embed-text`, about 300 MB), so give it a few minutes. `docker compose
ps` shows each service; `lore` turns healthy when it is ready.

**From a clone instead** (to build the image yourself, or change the code):

```bash
git clone https://github.com/Jatin-Roopchandani/LORE.git
cd LORE
docker compose up -d --build
```

`docker compose build` rebuilds the image from your checkout at any time.

No `.env` file is needed. To change the port or anything else, copy
[`.env.example`](../.env.example) to `.env` next to `docker-compose.yml`,
edit it, and run `docker compose up -d` again.

### What runs

| Service | What it does |
|---|---|
| `lore` | The web app, on port 8080. |
| `worker` | Processes uploaded books, one at a time. |
| `migrate` | Runs once at start to bring the database up to date, then exits. |
| `ollama` | Embeddings (`nomic-embed-text`), used to search your books. |
| `ollama-pull` | Runs once to download the embedding model, then exits. |

Data lives in two Docker volumes: `lore-data` (every library, the accounts
database and the server's secrets) and `ollama` (the embedding model, which
can always be downloaded again).

**Why SQLite, not Postgres:** the accounts database is a SQLite file in the
`lore-data` volume. LORE already keeps each library in SQLite, a graph and a
vector store on that same volume, so a separate database server would add a
service, a password and a second thing to back up without letting anything
scale further on one machine. The central database is built and tested on
SQLite (it is what local mode and the test suite use), in WAL mode with a
busy timeout, so the web app and the worker share it safely. To use a Postgres server
you already run, set `LORE_DATABASE_URL` in `.env` (see
[Settings](#settings)).

## Choosing the AI model

Until you choose one, each person has to add their own API key in Settings.
Choose one for everyone instead: as the admin, open **Admin** (the account
menu, or http://localhost:8080/admin) and use **AI provider for everyone**.

- **Gemini, Claude or OpenAI:** paste an API key. It is checked once, stored
  encrypted, and never shown again (only its last four characters). Gemini
  has a free tier: a key from [Google AI Studio](https://aistudio.google.com/apikey).
- **Local model:** any server that speaks OpenAI's chat completions API:
  llama.cpp's `llama-server`, vLLM, LM Studio, or Ollama's `/v1`. Give the
  server's address and the model's name, press **Test connection** (one tiny
  request; a first request may take a while as the model loads), then
  **Save for everyone**.

Anyone can still add their own key in **Settings → AI providers**; their own
key then wins for them. Only the admin can point LORE at a server address:
people you add choose among Gemini, Claude and OpenAI keys only.

### A model on your own machine (llama.cpp)

Say you run a Qwen model of about 28-30B parameters with llama.cpp on the same
computer as Docker. Start `llama-server` so the containers can reach it:

```bash
llama-server -m ~/models/qwen-28b-instruct-Q4_K_M.gguf \
  --host 0.0.0.0 --port 8081 \
  -c 32768 --jinja -ngl 99
```

- `--host 0.0.0.0`: containers reach your computer through Docker's bridge,
  not through 127.0.0.1, so a server listening only on 127.0.0.1 can't be
  reached. (On Linux, a firewall such as ufw may also need to allow port
  8081 from Docker's network.)
- `--port 8081`: llama-server's default, 8080, is LORE's own port.
- `-c 32768`: LORE's prompts carry book passages; give it 16k tokens at least.
- `--jinja`: the model's own chat template, which instruct models need.
- `-ngl 99`: everything on the GPU, if it fits.

Then in **Admin → AI provider for everyone**, choose **Local model** and
enter:

- **Server address:** `http://host.docker.internal:8081/v1`
- **Model:** any name (llama-server serves the one model it loaded)

`host.docker.internal` is this computer as seen from inside the containers
(`docker-compose.yml` maps it with `host-gateway`, so it works on Linux as
well as Docker Desktop). A server on another machine is just its address:
`http://192.168.1.50:8081/v1`.

**What to expect from a local model.** Processing a novel sends many hundreds
of requests: on a 28B model with one GPU, plan on hours for a long book (it
runs in the background; the Library panel shows progress, and you can ask
questions about the chapters already done). Questions take tens of seconds.
LORE asks for JSON and checks every reply; small models (under about 7B)
often fail at that, so quality and success rates drop sharply below that
size. One request runs at a time by default, which suits one GPU.

Other servers: vLLM `http://host.docker.internal:8000/v1`, LM Studio
`http://host.docker.internal:1234/v1`, Ollama
`http://host.docker.internal:11434/v1` (Ollama's `/v1` defaults to a 4,096
token context: start it with `OLLAMA_CONTEXT_LENGTH=16384` or more).

## Adding people

In **Admin → People → Add a person**, enter their email and either:

- **a password**, which you then tell them; or
- **nothing**, to get a **set-password link**. Send it to them however you
  like (there is no email): they open it, choose a password and are signed in.
  It works once, for 7 days.

Each person gets their own library (it starts with a small sample book), their
own reading progress and chat history. Nobody sees anyone else's books.

On the same page you can **reset a password** (type a new one, or get a new
link; either way that person is signed out everywhere), disable an account,
sign someone out everywhere, or delete an account and everything in it.

Everyone can change their own password in **Settings → Account**.

To make someone else an admin too:

```bash
docker compose exec lore lore admin grant person@example.com
```

## Upgrading

```bash
docker compose pull
docker compose up -d
```

The `migrate` service brings the database up to date before the app starts.
Built from a clone? `git pull` then `docker compose up -d --build`.

To stay on one version, set `LORE_VERSION=0.2.0` (for example) in `.env`;
`LORE_VERSION=edge` follows the main branch.

## Backups and restore

Everything that matters is in the `lore-data` volume. The simplest consistent
backup stops LORE for a moment:

```bash
docker compose stop lore worker
docker run --rm -v lore_lore-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/lore-backup-$(date +%F).tar.gz -C /data .
docker compose start lore worker
```

(`lore_lore-data` is the volume's full name: the project name `lore`, then
the volume. `docker volume ls` lists it.)

To restore onto a fresh install:

```bash
docker compose down                      # keep the volumes
docker run --rm -v lore_lore-data:/data -v "$PWD":/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/lore-backup-2026-11-13.tar.gz -C /data && chown -R 10001:10001 /data'
docker compose up -d
```

The backup holds `secrets.env`, which includes the key that opens the stored
API keys: keep the backup private. Without it, the libraries and accounts are
still fine, but every saved API key has to be entered again.

## Opening it beyond this computer

**On your home network:** other devices can already open
`http://<this computer's address>:8080` (for example `http://192.168.1.20:8080`).
LORE answers any IP address and `localhost`. For a name instead (such as
`nas.local`), add it to `LORE_ALLOWED_HOSTS` in `.env`, and set
`LORE_BASE_URL=http://nas.local:8080` so the links the admin copies use it.
To keep LORE on this computer only, set `LORE_BIND=127.0.0.1`.

**On the internet:** put a reverse proxy with HTTPS in front, and never
publish plain http. With [Caddy](https://caddyserver.com) (automatic
certificates):

```
lore.example.com {
    reverse_proxy localhost:8080
}
```

and in `.env`:

```bash
LORE_BASE_URL=https://lore.example.com
LORE_TRUST_PROXY=1      # the client's address from X-Forwarded-For (rate limits)
LORE_BIND=127.0.0.1     # only the proxy reaches LORE directly
```

nginx works too; pass the original host on (`proxy_set_header Host $host;`)
and `X-Forwarded-For`, and turn off buffering for `/api/ask` (answers stream:
`proxy_buffering off;`).

Create the admin account before you expose it anyway: until then the setup
page is open to anyone who has the setup code from the log.

### Cookies and HTTPS

Sign-in cookies are marked `Secure` (sent over HTTPS only) exactly when
`LORE_BASE_URL` starts with `https://`. So on `http://localhost` or a LAN
address sign-in just works, and behind an HTTPS proxy the cookies are secure.
`LORE_COOKIE_SECURE=1` or `0` overrides that. One catch: with an `https://`
base URL, signing in over plain `http://` (say, the LAN address directly) will
not stick, because the browser refuses Secure cookies there: use the HTTPS
address.

## Settings

All optional; see [`.env.example`](../.env.example) for the full list.

| Variable | Default | What it does |
|---|---|---|
| `LORE_PORT` | `8080` | The port on this computer. |
| `LORE_BIND` | `0.0.0.0` | Listen on every address, or `127.0.0.1` for this computer only. |
| `LORE_BASE_URL` | `http://localhost:8080` | The address people use: links and the cookie `Secure` flag follow it. |
| `LORE_ALLOWED_HOSTS` | (none) | Extra host names to answer, comma-separated; `*` for any. |
| `LORE_COOKIE_SECURE` | from `LORE_BASE_URL` | Force Secure cookies on (`1`) or off (`0`). |
| `LORE_TRUST_PROXY` | `0` | `1` behind your own reverse proxy. |
| `LORE_OLLAMA_HOST` | `http://ollama:11434` | Your own Ollama instead of the bundled one. |
| `LORE_DATABASE_URL` | SQLite in the volume | `postgresql+psycopg://user:pass@host:5432/lore` for Postgres. |
| `LORE_VERSION` | `latest` | The image tag: a version, `latest` or `edge`. |
| `LORE_SETUP_CODE` | a random code, in the log | Your own setup code, for an unattended install. |
| `LORE_SESSION_SECRET`, `LORE_CSRF_SECRET`, `LORE_KEY_ENCRYPTION_KEY` | made on first start | Kept in `/data/secrets.env`; set them only to manage them yourself. |

**Your own Ollama for embeddings.** Set `LORE_OLLAMA_HOST`, for an Ollama on
this computer `http://host.docker.internal:11434` (it must listen beyond
127.0.0.1: start it with `OLLAMA_HOST=0.0.0.0 ollama serve`). `ollama-pull`
then pulls `nomic-embed-text` there, and the bundled `ollama` service just
idles (a few MB of memory, no model loaded).

**Tuning the local model.** The image's `lore.toml` sends one request at a
time and caps each reply at 8192 tokens. To change that, mount your own copy:
add `- ./lore.toml:/app/lore.toml:ro` under the `lore` and `worker` services'
`volumes:` and set `[lore.llm] concurrency` (for `llama-server -np 4`) or
`max_output_tokens` there (keep `provider` as it is: the model itself is
chosen on the admin page).

## Troubleshooting

**See what's happening.**

```bash
docker compose ps                    # every service and its health
docker compose logs -f lore worker   # the web app and the book processor
docker compose logs ollama-pull      # the embedding model download
```

**Check every part.** `lore doctor` inside the container checks the database,
the graph and vector stores, and Ollama:

```bash
docker compose exec lore lore doctor
```

**"no server at http://ollama:11434", or books stuck on the first step.** The
embedding model may still be downloading, or the download failed (no
internet at first start). Pull it again:

```bash
docker compose run --rm ollama-pull
```

**Test connection fails for a local model.**
- "Couldn't reach a server": is `llama-server` running with `--host 0.0.0.0`?
  From the container: `docker compose exec lore python -c "import urllib.request as u;
  print(u.urlopen('http://host.docker.internal:8081/v1/models').read()[:200])"`.
  On Linux, check the firewall allows Docker's network to that port.
- "didn't return the JSON LORE asked for": the model is too small, or not an
  instruction-tuned model, or the server lacks `--jinja`. Try a larger
  instruct model.
- Very slow, or a test that times out: the first request loads the model; try
  again. If every request takes minutes, use a smaller quantization, put more
  layers on the GPU (`-ngl`), or a smaller model for the short steps.

**A book fails or pauses.** The Library panel says why. "Paused" with a key
problem means the AI provider refused the key or there is none: set the
provider for everyone again (Admin), or add a key in Settings; paused books
continue by themselves once a working key is saved.

**"this server does not answer to that address".** You opened LORE by a host
name it doesn't know. Add it to `LORE_ALLOWED_HOSTS` (or `LORE_BASE_URL`).

**Can't stay signed in.** `LORE_BASE_URL` is `https://` but you opened the
`http://` address: see [Cookies and HTTPS](#cookies-and-https).

**Locked out as the admin.** Another admin can reset your password from the
admin page. Or set it from the server's shell (it asks for the new one twice):

```bash
docker compose exec lore lore admin set-password you@example.com
```

**Where is the setup code?** `docker compose logs lore | grep -A1 "setup code"`.
It is printed on every start until the admin exists, and kept in the volume
(`/data/setup-code`) so a restart keeps the same one. Wrong codes are limited
to 10 tries per 15 minutes from one address.

**Start over completely.** This deletes every library and account:

```bash
docker compose down -v
```
