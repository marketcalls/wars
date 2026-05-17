"""
wars — WhatsApp client for Python, powered by Rust.

A thin PyO3 wrapper over whatsapp-rust. Drop it into any Python app to
send and receive WhatsApp messages — no Node sidecar, no separate server.

Quick start
-----------
    from wars import WhatsApp

    wa = WhatsApp(owner="14155550100")
    wa.on_qr(lambda code: wa.print_qr(code))
    wa.connect()
    wa.wait_until_ready()
    wa.send("Hello from wars")
"""

from __future__ import annotations

import atexit
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional, Union

from ._wars import WhatsApp as _RustWhatsApp  # noqa: F401  (native ext)
from ._wars import __version__ as __version__

__all__ = [
    "WhatsApp",
    "Message",
    "print_qr",
    "show_qr",
    "qr_to_base64",
    "qr_to_data_url",
    "__version__",
]


@dataclass(frozen=True)
class Message:
    """Inbound message — what handlers receive."""

    chat: str
    sender: str
    is_group: bool
    is_from_me: bool
    id: str
    push_name: str
    timestamp: int
    text: Optional[str]
    media_type: str

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(
            chat=d["chat"],
            sender=d["sender"],
            is_group=d["is_group"],
            is_from_me=d["is_from_me"],
            id=d["id"],
            push_name=d["push_name"],
            timestamp=d["timestamp"],
            text=d["text"],
            media_type=d["media_type"],
        )


class WhatsApp:
    """
    High-level WhatsApp client. Wraps the Rust extension with ergonomic
    callbacks and an iterator API.

    Example
    -------
    Send-only::

        wa = WhatsApp("whatsapp.db")
        wa.connect()
        wa.wait_until_ready()
        wa.send_text("14155550100", "Build #4321 finished")

    Two-way bot::

        wa = WhatsApp("whatsapp.db")

        @wa.on_message
        def handle(msg):
            if msg.text == "/ping":
                wa.send_text(msg.chat, "pong")

        wa.connect()
        wa.run_forever()
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        owner: Optional[str] = None,
        log_level: Optional[str] = None,
    ):
        """
        Parameters
        ----------
        db_path : str, optional
            Where to persist the SQLite session. Three modes:

            - **omitted / ``None`` (default)** — in-memory. No filesystem
              touched. Simplest deployment, but you re-pair on every restart.
              Use this for tests, ephemeral runs, and as the starting point
              before you wire persistence into your own database.
            - **a file path** (``"whatsapp.db"``, ``"/var/lib/app/wa.db"``)
              — SQLite on disk. Session survives restarts. You manage
              backups, permissions, and rotation.
            - **a SQLite URI** (``"file:foo?mode=memory&cache=shared"``)
              — advanced; honors any URI SQLite accepts.

            For storing the session inside *your own* database
            (e.g. ``app.db``), keep this as ``None`` and round-trip
            through :meth:`export_session` / :meth:`from_bytes`.
        owner : str, optional
            Default recipient for single-argument ``send()`` calls — usually
            your own phone number. Lets you write ``wa.send("alert text")``
            without repeating the JID.
        log_level : str, optional
            Rust log level: "error" | "warn" | "info" | "debug" | "trace".
            Defaults to "warn".
        """
        if db_path is None:
            # Each instance gets its own isolated in-memory DB. ``cache=shared``
            # lets diesel's connection pool open multiple handles against the
            # same logical DB. The PID + object-id keeps multiple WhatsApp
            # instances in the same process from clobbering each other.
            db_path = (
                f"file:wars_mem_{os.getpid()}_{id(self):x}"
                f"?mode=memory&cache=shared"
            )
        self._db_path = db_path
        self._inner = _RustWhatsApp(db_path, log_level)
        self._owner = owner

    # ── session persistence (for storing in your own DB) ──────────────

    def export_session(self) -> bytes:
        """
        Dump the entire session (Signal keys, device credentials, prekey
        bundles) to a single ``bytes`` blob.

        Store this anywhere safe — your existing database (encrypted
        column in ``app.db``), a secret manager, an env var, etc.

        Restore later with :meth:`from_bytes`.

        Requirements
        ------------
        **The client must have been constructed with a file-backed
        ``db_path``** — not the default in-memory URI. Rust's bundled
        SQLite and Python's stdlib ``sqlite3`` are two separate library
        instances and cannot share an in-memory database. Export only
        works when both libraries can open the same file on disk.

        Typical pairing flow::

            import os
            wa = WhatsApp("pair_temp.db", owner=ME)         # file-backed
            wa.connect(); wa.wait_until_ready()             # pair via QR
            blob = wa.export_session()                       # dump bytes
            wa.disconnect()
            os.unlink("pair_temp.db")                        # clean up
            # now stash ``blob`` inside your DB / env / secret manager

        Notes
        -----
        - Forces a flush of pending writes from the Rust persistence layer
          first (the background saver only runs every 30 s).
        - Uses SQLite's online backup API via the stdlib ``sqlite3`` module,
          so it works while the client is connected without disrupting writes.
        """
        import sqlite3

        if "mode=memory" in self._db_path:
            raise RuntimeError(
                "export_session() requires a file-backed db_path — the "
                "in-memory default cannot be snapshotted from Python "
                "(Rust and Python use separate SQLite library instances).\n\n"
                "Construct with `WhatsApp(\"pair_temp.db\", owner=...)`, "
                "pair, export, then delete the file."
            )

        # Force pending device-state writes to land in SQLite before we snapshot.
        if self.is_connected():
            self._inner.flush()

        src = sqlite3.connect(self._db_path, uri=True)
        fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="wars_dump_")
        os.close(fd)
        try:
            dst = sqlite3.connect(tmp_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            with open(tmp_path, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @classmethod
    def from_bytes(
        cls,
        blob: bytes,
        owner: Optional[str] = None,
        log_level: Optional[str] = None,
    ) -> "WhatsApp":
        """
        Restore a session previously dumped by :meth:`export_session`.

        Bytes are written to a private temp file (auto-deleted at process
        exit) because SQLite needs a file handle at runtime — but *your*
        stored blob never needs to touch the filesystem.

        Typical flow::

            # First time — pair to a temp file, then stash bytes in your DB:
            wa = WhatsApp("pair_temp.db", owner=ME)
            wa.connect(); wa.wait_until_ready()           # pair via QR
            blob = wa.export_session()
            db.execute("UPDATE secrets SET wa = ? WHERE id = 1", (blob,))

            # Every run after:
            blob = db.execute("SELECT wa FROM secrets WHERE id = 1").fetchone()[0]
            wa = WhatsApp.from_bytes(blob, owner=ME)
            wa.connect()                                  # no QR — reuses session
        """
        fd, path = tempfile.mkstemp(suffix=".db", prefix="wars_session_")
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)
        # Clean up the temp file when the process exits. We don't trigger
        # this earlier because SQLite holds an open handle for the lifetime
        # of the WhatsApp instance.
        atexit.register(_safe_unlink, path)
        return cls(path, owner=owner, log_level=log_level)

    # ── one-shot interactive helper ───────────────────────────────────

    def pair(self, phone: Optional[str] = None, timeout: float = 300.0) -> None:
        """
        One-call pairing helper for notebooks and scripts.

        Equivalent to wiring ``on_qr`` / ``on_pair_code`` / ``on_connected``
        yourself and calling ``connect()`` + ``wait_until_ready()``. The QR
        is rendered with :func:`show_qr` — inline in Jupyter, ASCII in a
        terminal. Blocks until the device is paired or ``timeout`` seconds
        elapse.

        Idempotent: if already connected, returns immediately.

        Example (Jupyter)::

            from wars import WhatsApp
            wa = WhatsApp(owner="14155550100")
            wa.pair()                                # scan the inline QR
            wa.send("Hello from wars")               # works
        """
        if self.is_connected():
            return

        self.on_qr(show_qr)
        self.on_pair_code(lambda code: print(f"Pair code: {code}"))

        self.connect(phone=phone)
        self.wait_until_ready(timeout=timeout)

    # ── connection ────────────────────────────────────────────────────

    def connect(self, phone: Optional[str] = None) -> None:
        """
        Start the background connection. Returns immediately.

        Parameters
        ----------
        phone : str, optional
            E.164 phone number (digits only, no '+'). When set, pair-code
            authentication runs concurrently with QR — whichever the user
            completes first wins. If omitted, only QR is offered on first
            run; subsequent runs reuse the saved session.
        """
        self._inner.connect(phone)

    def wait_until_ready(self, timeout: float = 120.0) -> None:
        """Block until logged in. Raises ``TimeoutError`` on timeout."""
        self._inner.wait_until_ready(timeout)

    def is_connected(self) -> bool:
        return self._inner.is_connected()

    def disconnect(self) -> None:
        self._inner.disconnect()

    # ── sending ───────────────────────────────────────────────────────

    def send(
        self,
        *args: Any,
        image: Optional[Union[str, bytes]] = None,
        document: Optional[Union[str, bytes]] = None,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        mimetype: Optional[str] = None,
    ) -> Union[str, list]:
        """
        One call for every send pattern. Smart-dispatches based on args.

        Calling shapes::

            wa.send("Hello there")                           # text → owner
            wa.send("14155550100", "Hello there")            # text → recipient
            wa.send("14155550100", image="screenshot.png")   # image → recipient
            wa.send("14155550100", image="screenshot.png",
                    caption="Latest dashboard")
            wa.send("14155550100", document="report.pdf")    # doc → recipient
            wa.send("123456789@g.us", "Group msg")           # group JID OK
            wa.send([(num, "Maintenance window in 5min")     # broadcast
                     for num in oncall])

        Returns
        -------
        str
            message_id for a single send.
        list[str]
            list of message_ids for broadcast (same order as input).

        Raises
        ------
        ValueError
            If single-arg form is used without an ``owner`` configured.
        """
        # ── broadcast: list of (recipient, payload) tuples ─────────
        if len(args) == 1 and isinstance(args[0], list):
            results = []
            for item in args[0]:
                if not (isinstance(item, tuple) and len(item) == 2):
                    raise ValueError(
                        "broadcast items must be (recipient, text) tuples"
                    )
                to, text = item
                results.append(self.send_text(to, text))
            return results

        # ── normalize positional args into (to, body) ──────────────
        if len(args) == 0:
            to, body = self._default_owner(), None
        elif len(args) == 1:
            # Could be wa.send("text")  — text to owner
            # Or     wa.send("91...", image=...)  — image to recipient
            if image is not None or document is not None:
                to, body = args[0], None
            else:
                to, body = self._default_owner(), args[0]
        elif len(args) == 2:
            to, body = args
        else:
            raise TypeError(
                f"send() takes 0, 1 or 2 positional args, got {len(args)}"
            )

        # ── dispatch on kwargs ─────────────────────────────────────
        if image is not None:
            return self._inner.send_image(to, image, caption)
        if document is not None:
            return self._inner.send_document(to, document, filename, mimetype)

        if not isinstance(body, str):
            raise TypeError(
                "text body must be a str — pass image=… or document=… for media"
            )
        return self._inner.send_text(to, body)

    def send_group(self, group_id: str, text: str) -> str:
        """Send a text message to a group. ``group_id`` is the JID
        (``"123456789@g.us"`` or just ``"123456789"``)."""
        jid = group_id if "@" in group_id else f"{group_id}@g.us"
        return self._inner.send_text(jid, text)

    def _default_owner(self) -> str:
        if not self._owner:
            raise ValueError(
                "no default recipient — pass owner=… to WhatsApp(...) "
                "or call send(to, body)"
            )
        return self._owner

    # Explicit forms (kept for power users / clarity in code review)

    def send_text(self, to: str, text: str) -> str:
        return self._inner.send_text(to, text)

    def send_image(
        self,
        to: str,
        data: Union[str, bytes],
        caption: Optional[str] = None,
    ) -> str:
        return self._inner.send_image(to, data, caption)

    def send_document(
        self,
        to: str,
        data: Union[str, bytes],
        filename: Optional[str] = None,
        mimetype: Optional[str] = None,
    ) -> str:
        return self._inner.send_document(to, data, filename, mimetype)

    # ── event callbacks (can be used as decorators) ───────────────────

    def on_qr(self, fn: Callable[[str], Any]) -> Callable[[str], Any]:
        self._inner.set_on_qr(fn)
        return fn

    def on_pair_code(self, fn: Callable[[str], Any]) -> Callable[[str], Any]:
        self._inner.set_on_pair_code(fn)
        return fn

    def on_message(self, fn: Callable[[Message], Any]) -> Callable[[Message], Any]:
        def adapter(d: dict) -> None:
            fn(Message.from_dict(d))

        self._inner.set_on_message(adapter)
        return fn

    def on_connected(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        self._inner.set_on_connected(fn)
        return fn

    def on_disconnect(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        self._inner.set_on_disconnect(fn)
        return fn

    # ── pull-style event iteration ────────────────────────────────────

    def messages(self, timeout: float = 1.0) -> Iterator[Message]:
        """
        Yield incoming messages forever. ``timeout`` is the inner poll
        interval — small values keep the loop responsive to KeyboardInterrupt.
        Non-message events are skipped silently.
        """
        while True:
            ev = self._inner.next_event(timeout)
            if ev is None:
                continue
            if ev.get("kind") == "message":
                yield Message.from_dict(ev)

    def events(self, timeout: float = 1.0) -> Iterator[dict]:
        """Yield raw event dicts (including qr/pair_code/connected/...)."""
        while True:
            ev = self._inner.next_event(timeout)
            if ev is not None:
                yield ev

    def drain_events(self) -> list:
        """Non-blocking — return all queued events as dicts."""
        return self._inner.drain_events()

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def print_qr(code: str) -> None:
        """Render a QR code to stdout. Requires the optional `qrcode` extra."""
        print_qr(code)

    @staticmethod
    def qr_to_base64(code: str) -> str:
        """Return the QR as a base64-encoded PNG string (no ``data:`` prefix).

        No filesystem I/O. Useful for serving over HTTP, embedding in a
        message, or storing in a database. Requires the optional ``qrcode``
        extra: ``pip install wars[qr]``.
        """
        return qr_to_base64(code)

    @staticmethod
    def qr_to_data_url(code: str) -> str:
        """Return the QR as a ``data:image/png;base64,...`` URL.

        Drop directly into an HTML ``<img src=...>`` for browser pairing
        flows::

            @app.get("/pair-qr")
            def pair_qr():
                return {"img": WhatsApp.qr_to_data_url(latest_qr_code)}
        """
        return qr_to_data_url(code)

    def run_forever(self) -> None:
        """Block the calling thread until KeyboardInterrupt, then disconnect."""
        try:
            while self.is_connected() or not self._inner.next_event(0.5) is None:
                # Drain so callbacks fire even with no per-event handler
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.disconnect()

    # Allow ``with WhatsApp(...) as wa:``
    def __enter__(self) -> "WhatsApp":
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def print_qr(code: str) -> None:
    """Render a QR code to stdout using the optional `qrcode` extra."""
    try:
        import qrcode  # type: ignore
    except ImportError:
        print(
            "wars: install the QR extra to render QR codes inline:\n"
            "    pip install wars[qr]\n"
            f"Raw QR payload (encode this yourself):\n{code}",
            file=sys.stderr,
        )
        return
    q = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    q.add_data(code)
    q.make()
    q.print_ascii(invert=True)


def qr_to_base64(code: str) -> str:
    """Encode the QR as a base64 PNG string. No filesystem I/O.

    Requires the optional ``qrcode`` extra. Returns the raw base64 (no
    ``data:`` prefix); for the data URL form use :func:`qr_to_data_url`.
    """
    import base64
    import io

    try:
        import qrcode  # type: ignore
    except ImportError as e:
        raise ImportError(
            "wars: qr_to_base64 needs the qrcode extra — "
            "`pip install wars[qr]`"
        ) from e

    buf = io.BytesIO()
    qrcode.make(code).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def qr_to_data_url(code: str) -> str:
    """Encode the QR as a ``data:image/png;base64,...`` URL — drop into an
    HTML ``<img src=...>`` for browser-based pairing flows. No filesystem
    I/O. Requires the optional ``qrcode`` extra.
    """
    return f"data:image/png;base64,{qr_to_base64(code)}"


def _is_jupyter() -> bool:
    """Best-effort detection of an interactive Jupyter / IPython kernel."""
    try:
        from IPython import get_ipython  # type: ignore

        ip = get_ipython()
        if ip is None:
            return False
        # ZMQInteractiveShell = Jupyter notebook / qtconsole.
        # TerminalInteractiveShell = `ipython` in a terminal — fall back
        # to plain stdout there.
        return type(ip).__name__ == "ZMQInteractiveShell"
    except Exception:
        return False


def show_qr(code: str) -> None:
    """Display a QR for ``code`` in whatever surface is available.

    - In a Jupyter notebook: renders the QR as an inline PNG image (uses
      ``IPython.display.Image``).
    - In a regular terminal: prints an ASCII QR via :func:`print_qr`.

    Requires the optional ``qrcode`` extra: ``pip install wars[qr]``.
    """
    if _is_jupyter():
        try:
            import io

            import qrcode  # type: ignore
            from IPython.display import Image, display  # type: ignore

            buf = io.BytesIO()
            qrcode.make(code).save(buf, format="PNG")
            display(Image(data=buf.getvalue()))
            return
        except ImportError:
            # Fall through to the terminal renderer below.
            pass
    print_qr(code)
