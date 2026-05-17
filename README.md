# wars

**WhatsApp client for Python, powered by Rust.** A thin PyO3 wrapper over
[whatsapp-rust](https://github.com/marketcalls/whatsapp-rust) (vendored
as a submodule). Drop it into any Python app to send and receive
WhatsApp messages — no Node.js sidecar, no separate server, no IPC.

> `wars` is an **unofficial** library — it is not built by, affiliated with,
> endorsed by, or sponsored by WhatsApp / Meta. "WhatsApp" and related
> trademarks belong to their respective owners.

```bash
pip install wars
```

## Why

- **No Node.js required.** A single `pip install` ships native code; the
  WhatsApp Web protocol (Noise handshake, Signal Protocol, protobuf) runs
  in Rust inside your Python process.
- **Sync API.** Call `wa.send(...)` from any Flask handler. No `asyncio`,
  no background server, no IPC.
- **No files by default.** In-memory session out of the box — zero
  filesystem permissions to set. When you're ready, dump the session as
  `bytes` and stash it in your existing database.
- **One unified `send()`.** Same call for text, images, documents, groups,
  and broadcasts. Bot mode with an `@on_message` decorator.

## Quick start

### 1. Pair your phone (one-time)

**In a Jupyter notebook** — one cell, end-to-end:

```python
from wars import WhatsApp

wa = WhatsApp(owner="919876543210")
wa.pair()                                # QR shows inline; scan it
wa.send("Hello from wars")               # works after pair
```

`wa.pair()` blocks until the device is paired or 5 minutes elapse,
displays a fresh QR image each time WhatsApp rotates it (~every 30s),
and prints any pair code WhatsApp issues. In a terminal it falls back
to an ASCII QR automatically.

**From the command line** — useful for headless setup:

```bash
curl -O https://raw.githubusercontent.com/marketcalls/wars/main/examples/pair.py
python pair.py --phone 919876543210
```

Use either:

- **QR:** WhatsApp on phone → *Linked devices* → *Link a device* → scan.
- **Code:** *Linked devices* → *Link with phone number* → type the code.

By default **nothing is written to disk** — the session is in-memory and
will be lost when the script exits. For persistence pick one:

```bash
python pair.py --phone 919876543210 --db whatsapp.db    # SQLite file
python pair.py --phone 919876543210 --save-png qr.png   # also dump QR PNG
```

For the cleanest production flow, leave the script in-memory and stash
the session bytes into your own DB — see *Persist the session* below.

### 2. Send a message

One unified `send()` for every pattern. Pass `owner=` once and single-arg
sends go to your own number — perfect for personal alerts.

```python
from wars import WhatsApp

# No db_path → in-memory session. Re-pair if the process restarts.
wa = WhatsApp(owner="14155550100")
wa.connect()
wa.wait_until_ready()

# Single-arg send → goes to owner (yourself)
wa.send("Hello from wars")
wa.send(f"Build #{build_id} finished in {elapsed:.1f}s")

# Send to a specific contact
wa.send("14155550199", "Are you free for a quick call?")

# Image with caption
wa.send("14155550100", image="screenshot.png", caption="Latest dashboard")

# Document
wa.send("14155550100", document="report.pdf")

# Group message
wa.send_group("120363012345678901@g.us", "Daily standup in 5 minutes")

# Broadcast the same body to many recipients
wa.send([(n, "Server maintenance starting now") for n in oncall])
```

### 3. Persist the session in *your own* database

In-memory is simplest but you re-pair on every restart. For production,
keep the paired session as `bytes` inside your existing app database, an
encrypted column in a secret store, or anywhere you already keep API keys.

```python
import os, sqlite3, tempfile

# First run — pair to a short-lived temp file, then dump bytes once.
# (export_session() needs a file-backed DB; Rust + Python use separate
# SQLite library instances so an in-memory DB can't be snapshotted from
# Python. The temp file is 0600 and deleted as soon as we have the bytes.)
fd, pair_db = tempfile.mkstemp(suffix=".db", prefix="wars_pair_")
os.close(fd); os.chmod(pair_db, 0o600)

wa = WhatsApp(pair_db, owner="919876543210")
wa.connect(); wa.wait_until_ready()              # scan QR
blob = wa.export_session()                       # ~300 KB bytes
wa.disconnect()
os.unlink(pair_db)                               # clean up

# Stash wherever — example: a tiny table in your app's SQLite
db = sqlite3.connect("app.db")
db.execute("CREATE TABLE IF NOT EXISTS wa (id INTEGER PRIMARY KEY, blob BLOB)")
db.execute("INSERT OR REPLACE INTO wa VALUES (1, ?)", (blob,)); db.commit()

# Every run after — load and resume. No QR.
blob = db.execute("SELECT blob FROM wa WHERE id = 1").fetchone()[0]
wa = WhatsApp.from_bytes(blob, owner="919876543210")
wa.connect()                                     # instant reconnect
```

See [`examples/webapp_integration.py`](examples/webapp_integration.py)
for the full pattern including a `pair` CLI subcommand, lazy singleton,
and Flask integration.

### 4. Show the pairing QR in a browser (no files)

For a browser-based pairing screen, convert the QR to a base64 data URL
and embed it in HTML — no PNG ever touches the disk:

```python
from wars import WhatsApp, qr_to_data_url

wa = WhatsApp(owner="919876543210")
latest_qr = {"data_url": None}

@wa.on_qr
def cache(code):
    latest_qr["data_url"] = qr_to_data_url(code)

wa.connect()

# Flask route
@app.get("/pair-qr")
def pair_qr():
    return {"img": latest_qr["data_url"]}      # JSON for SPAs

# Or render directly:
#   <img src="{{ qr.data_url }}" />            # works because it's a data URL
```

Also available: `qr_to_base64(code)` for the raw base64 string without the
`data:` prefix. Both require the `qrcode` extra (`pip install wars[qr]`).

### 5. Or: just use a file path

If you want SQLite-on-disk (simpler than DB-stored bytes for solo runs),
pass a path:

```python
wa = WhatsApp("whatsapp.db", owner="919876543210")
```

The file lives wherever the path points. Treat it like any other secret
(restrict permissions, exclude from git — `.gitignore` already covers
`*.db`).

### 6. Receive messages (bot mode)

```python
from wars import WhatsApp, Message

wa = WhatsApp("whatsapp.db", owner="14155550100")

@wa.on_message
def handle(msg: Message):
    if msg.is_from_me:
        return  # ignore echoes of our own sends
    if msg.text == "/ping":
        wa.send(msg.chat, "pong")
    elif msg.text == "/status":
        wa.send(msg.chat, f"Uptime: {get_uptime()}")
    elif msg.text.startswith("/echo "):
        wa.send(msg.chat, msg.text.removeprefix("/echo "))

wa.connect()
wa.run_forever()
```

### 7. Integrate into a web app

Singleton pattern — one connection per process, send from any route. See
[`examples/webapp_integration.py`](examples/webapp_integration.py) for the
full sketch.

```python
# yourapp/whatsapp.py
import atexit
from threading import Lock
from wars import WhatsApp

_wa: WhatsApp | None = None
_lock = Lock()
OWNER = "14155550100"

def wa() -> WhatsApp:
    """Lazy singleton — first call pairs/connects, rest are free."""
    global _wa
    if _wa is not None:
        return _wa
    with _lock:
        if _wa is not None:
            return _wa
        client = WhatsApp("whatsapp.db", owner=OWNER)
        client.connect()
        client.wait_until_ready(timeout=60)
        atexit.register(client.disconnect)
        _wa = client
        return _wa
```

```python
# any Flask blueprint
from yourapp.whatsapp import wa

@app.post("/webhook/build")
def build_done():
    data = request.json
    wa().send(f"Build #{data['id']} {data['status']} in {data['duration']}s")
    return "ok"
```

Works the same way under **Django** (call from `AppConfig.ready()`), **FastAPI**
(wrap blocking calls with `asyncio.to_thread(wa().send, ...)`), **Streamlit**,
**Dash**, etc.

> ⚠️ **One process only.** WhatsApp Web is one-device-per-session. Run
> `gunicorn -w 1 --threads 8` or equivalent — *don't* fork multiple workers
> all using the same `whatsapp.db`, or Meta will unlink the device.

## API

### `WhatsApp(db_path=None, owner=None, log_level=None)`

Construct a client. Does not connect.

- `db_path=None` (default) — in-memory session, no filesystem touched.
- `db_path="path.db"` — SQLite on disk, session survives restarts.
- `owner=` — default recipient for single-arg `send()` calls.

### `WhatsApp.from_bytes(blob, owner=None)`

Class method. Restore a session previously exported with `export_session()`.
Use this to load a paired session from your own database.

| Method | Returns | Notes |
|---|---|---|
| **`pair(phone=None, timeout=300)`** | `None` | One-call interactive pairing helper. Renders QR inline in Jupyter, ASCII in a terminal. Blocks until paired. |
| `connect(phone=None)` | `None` | Start background run loop. Optional E.164 digits enable pair-code auth. |
| `wait_until_ready(timeout=120)` | `None` | Block until paired+online. Raises `TimeoutError`. |
| `is_connected()` | `bool` | |
| `disconnect()` | `None` | Idempotent. |
| **`export_session()`** | `bytes` | Dump session for safe storage in your own DB / secret manager. |
| **`send(*args, image=, document=, caption=, filename=)`** | `message_id` or `list` | Unified API — see shapes below. |
| `send_group(group_id, text)` | `message_id: str` | Convenience for groups. `group_id` accepts `"…@g.us"` or bare digits. |
| `send_text(to, text)` | `message_id: str` | Explicit form. `to` accepts `"919876543210"`, `"+91 98765 43210"`, full JID, or group JID. |
| `send_image(to, data, caption=None)` | `message_id: str` | Explicit form. `data`: file path *or* `bytes`. MIME auto-sniffed. |
| `send_document(to, data, filename=None, mimetype=None)` | `message_id: str` | Explicit form. |
| `on_qr(fn)` | decorator | `fn(qr_data: str)` |
| `on_pair_code(fn)` | decorator | `fn(code: str)` |
| `on_message(fn)` | decorator | `fn(msg: Message)` |
| `on_connected(fn)` / `on_disconnect(fn)` | decorator | `fn()` |
| `messages(timeout=1.0)` | iterator | yields `Message` |
| `events(timeout=1.0)` | iterator | yields raw dicts |
| `run_forever()` | `None` | Block until Ctrl-C |
| `print_qr(code)` *(static)* | `None` | Render QR to stdout (terminal). |
| `show_qr(code)` *(module fn)* | `None` | Render QR inline in Jupyter (PNG), or ASCII in a terminal. |
| `qr_to_base64(code)` *(static)* | `str` | QR → base64 PNG. No filesystem I/O. |
| `qr_to_data_url(code)` *(static)* | `str` | QR → `data:image/png;base64,…` URL. |

#### `send()` shapes

```python
wa.send("alert text")                          # → owner
wa.send("919876543210", "alert text")          # → recipient
wa.send("919876543210", image="screenshot.png", caption="Dashboard")
wa.send("919876543210", document="report.pdf")
wa.send("120363…@g.us", "group msg")           # group JIDs work as-is
wa.send([(n, "msg") for n in subscribers])     # broadcast
```

### `Message` (dataclass)

```python
chat: str           # JID of the chat (1:1 or group)
sender: str         # JID of the person who sent it
is_group: bool
is_from_me: bool
id: str             # message ID
push_name: str      # sender's display name
timestamp: int      # unix seconds
text: str | None    # plain text content if any
media_type: str     # "" / "image" / "document" / ...
```

## Building from source

```bash
git clone --recursive https://github.com/marketcalls/wars
cd wars
uv venv && source .venv/bin/activate
uv pip install maturin
maturin develop --release        # build + install into current venv
```

`--recursive` is required because `wars` vendors the upstream Rust crate
as a submodule at `vendor/whatsapp-rust`. If you forgot it, run
`git submodule update --init --recursive` after cloning.

The first build takes ~5 min (compiles the whole workspace); incremental
builds are seconds.

To produce a wheel:

```bash
maturin build --release          # wheel lands in target/wheels/
```

To publish to PyPI:

```bash
maturin publish                  # needs PYPI_TOKEN
```

Wheels target Python 3.8+ via abi3 — one wheel per OS+arch covers every
Python version from 3.8 onwards.

## What's in v0.1

- ✅ QR + pair-code auth, persistent SQLite session
- ✅ Unified `send()` — text, image, document, groups, broadcast
- ✅ `send_group()`, `send_text()`, `send_image()`, `send_document()` (explicit forms)
- ✅ Receive messages via `@on_message` callback or `wa.messages()` iterator
- ✅ All connection events: `on_qr`, `on_pair_code`, `on_connected`, `on_disconnect`
- ✅ Built-in JID normalization (digits, +91 …, full JIDs, group JIDs)

Not in v0.1 (the Rust crate supports them; bindings to be added):

- ❌ Group create/manage, reactions, edit/revoke, status, polls
- ❌ Voice notes, video, contact lookup, presence
- ❌ Async/await API (use `asyncio.to_thread` for now)

PRs welcome.

## Credits

`wars` is a thin Python binding layer. The protocol work — Noise handshake,
Signal Protocol, binary stanza encoding, media encryption, every byte that
goes on the wire — is done by the projects below. If `wars` is useful to
you, please go support them:

- **[whatsapp-rust](https://github.com/marketcalls/whatsapp-rust)**
  (originally by [@jlucaso1](https://github.com/jlucaso1/whatsapp-rust))
  — the Rust client that this package wraps, vendored here as a fork
  for stable supply-chain. Without it, `wars` is nothing.
- **[Baileys](https://github.com/WhiskeySockets/Baileys)** by the
  [WhiskeySockets](https://github.com/WhiskeySockets) maintainers — the
  TypeScript reference implementation that `whatsapp-rust` learns from for
  protocol quirks, edge cases, and behavior parity.
- **[whatsmeow](https://github.com/tulir/whatsmeow)** by
  [@tulir](https://github.com/tulir) — the Go implementation that pioneered
  much of the multi-device protocol reverse-engineering.
- **[@pokearaujo](https://github.com/pokearaujo/multidevice)** and
  **[@Sigalor](https://github.com/sigalor/whatsapp-web-reveng)** for early
  observations of the WhatsApp Multi-Device and WhatsApp Web protocols that
  the above projects were built on.

The Python bindings layer (this package) is by Rajendran R / Marketcalls.

## License

Copyright (c) 2026 Rajendran R / Marketcalls

Licensed under the MIT License: Permission is hereby granted, free of
charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software,
and to permit persons to whom the Software is furnished to do so, subject
to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.

Thus, the maintainers of the project can't be held liable for any potential
misuse of this project.

`wars` also bundles and statically links the upstream
[`whatsapp-rust`](https://github.com/marketcalls/whatsapp-rust) crate
(a fork of [@jlucaso1's original](https://github.com/jlucaso1/whatsapp-rust)),
also
distributed under the MIT License (Copyright (c) 2025 João Lucas de
Oliveira Lopes). See [`LICENSE`](LICENSE) for the full combined notice.

## Disclaimer

This is an **unofficial**, open-source reimplementation. Using custom
WhatsApp clients may violate Meta's Terms of Service and could result in
account suspension. **Use at your own risk.**

This project is not affiliated, associated, authorized, endorsed by, or in
any way officially connected with WhatsApp or any of its subsidiaries or
its affiliates. The official WhatsApp website can be found at
[whatsapp.com](https://whatsapp.com). "WhatsApp" and related marks are
registered trademarks of their respective owners.

The maintainers of `wars` do not in any way condone the use of this
package in practices that violate the Terms of Service of WhatsApp. We
call upon the personal responsibility of users to use this package fairly,
as it is intended to be used. Do not spam people with this. We discourage
any stalkerware, bulk, or automated mass-messaging usage.

### WhatsApp Terms of Service — practical risk note

Unofficial WhatsApp clients can get the linked device unlinked or the entire
account banned by Meta's automation. The dominant trigger is **send volume
and pattern**, not the client itself:

- **Low risk (typical personal/automation usage)** — a handful of
  notifications a day, the occasional `/status` reply to your own number
  or a small private group. This pattern is indistinguishable from a
  person using WhatsApp normally and stays well under Meta's automated
  thresholds.
- **Medium risk** — sending to dozens of distinct contacts who haven't
  messaged you first, frequent broadcast lists, sending the same message
  body to many recipients in a short window.
- **High risk (don't)** — bulk marketing, cold outreach to numbers you
  scraped, lookalike-spam patterns, evading rate limits. This is what
  triggers bans. Use the official WhatsApp Business / Cloud API for those
  use cases.

Treat your paired session file (`whatsapp.db`) as sensitive — it contains the
private keys for your linked device. Anyone who gets a copy can impersonate
that session.
