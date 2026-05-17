//! PyO3 bindings for whatsapp-rust.
//!
//! Exposes a single `WhatsApp` class with a synchronous API. All async work
//! happens on a tokio multi-thread runtime owned by the class instance; Python
//! methods block on it. Events are surfaced two ways: (1) via callbacks
//! registered with `set_on_*`, invoked under the GIL on a runtime worker, and
//! (2) via a crossbeam queue drained by `next_event()` for Flask-style polling.

// pyo3 0.22's `#[pymethods]` macro emits `Into::into` shims on every `PyResult`
// return value — clippy flags each as `useless_conversion` even though the
// generated code is fine. Silence at the crate level; revisit on pyo3 0.23+.
#![allow(clippy::useless_conversion)]

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context;
use crossbeam_channel::{Receiver, Sender};
use pyo3::exceptions::{PyRuntimeError, PyTimeoutError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use tokio::runtime::Runtime;

use wacore::download::MediaType;
use wacore::proto_helpers::MessageExt;
use wacore::types::events::Event;
use wacore_binary::Jid;
use waproto::whatsapp as wa;
use whatsapp_rust::bot::{Bot, BotHandle};
use whatsapp_rust::client::Client;
use whatsapp_rust::pair_code::PairCodeOptions;
use whatsapp_rust::store::SqliteStore;
use whatsapp_rust::TokioRuntime;
use whatsapp_rust_tokio_transport::TokioWebSocketTransportFactory;
use whatsapp_rust_ureq_http_client::UreqHttpClient;

// ─── Event flowing from Rust → Python ────────────────────────────────────

#[derive(Clone, Debug)]
enum PyEvent {
    Qr(String),
    PairCode(String),
    Connected,
    LoggedOut,
    Message {
        chat: String,
        sender: String,
        is_group: bool,
        is_from_me: bool,
        id: String,
        push_name: String,
        timestamp: i64,
        text: Option<String>,
        media_type: String,
    },
}

// ─── The Python class ────────────────────────────────────────────────────

#[pyclass(unsendable)]
struct WhatsApp {
    db_path: PathBuf,
    runtime: Arc<Runtime>,
    client: Option<Arc<Client>>,
    handle: Option<BotHandle>,
    event_rx: Receiver<PyEvent>,
    event_tx: Sender<PyEvent>,
    callbacks: Arc<Callbacks>,
}

#[derive(Default)]
struct Callbacks {
    on_qr: parking_lot_lite::Mutex<Option<Py<PyAny>>>,
    on_pair_code: parking_lot_lite::Mutex<Option<Py<PyAny>>>,
    on_message: parking_lot_lite::Mutex<Option<Py<PyAny>>>,
    on_connected: parking_lot_lite::Mutex<Option<Py<PyAny>>>,
    on_disconnect: parking_lot_lite::Mutex<Option<Py<PyAny>>>,
}

// We use std::sync::Mutex via this tiny alias to avoid an extra dep.
mod parking_lot_lite {
    pub use std::sync::Mutex;
}

#[pymethods]
impl WhatsApp {
    /// Open (or create) a session at `db_path`. Does not connect.
    #[new]
    #[pyo3(signature = (db_path, log_level = None))]
    fn new(db_path: String, log_level: Option<String>) -> PyResult<Self> {
        // Init logger once. Ignore if already initialized by host app.
        let _ = env_logger::Builder::from_env(
            env_logger::Env::default().default_filter_or(log_level.as_deref().unwrap_or("warn")),
        )
        .try_init();

        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .thread_name("wars-tokio")
            .build()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to build tokio runtime: {e}")))?;

        let (event_tx, event_rx) = crossbeam_channel::unbounded();

        Ok(Self {
            db_path: PathBuf::from(db_path),
            runtime: Arc::new(runtime),
            client: None,
            handle: None,
            event_rx,
            event_tx,
            callbacks: Arc::new(Callbacks::default()),
        })
    }

    /// Start the background run loop. Returns immediately. Use
    /// `wait_until_ready()` to block until the device is paired+connected.
    ///
    /// `phone` is optional — pass `"919876543210"` (E.164, no +) to enable
    /// pair-code authentication concurrently with QR. If omitted, only QR is
    /// offered on first run.
    #[pyo3(signature = (phone = None))]
    fn connect(&mut self, phone: Option<String>) -> PyResult<()> {
        if self.handle.is_some() {
            return Err(PyRuntimeError::new_err("already connected"));
        }

        let db = self.db_path.to_string_lossy().to_string();
        let runtime = self.runtime.clone();
        let event_tx = self.event_tx.clone();
        let callbacks = self.callbacks.clone();

        let (client, handle) = runtime
            .block_on(async move {
                let backend = Arc::new(SqliteStore::new(&db).await.context("open sqlite")?);

                let mut builder = Bot::builder()
                    .with_backend(backend)
                    .with_transport_factory(TokioWebSocketTransportFactory::new())
                    .with_http_client(UreqHttpClient::new())
                    .with_runtime(TokioRuntime)
                    .skip_history_sync();

                if let Some(phone) = phone {
                    builder = builder.with_pair_code(PairCodeOptions {
                        phone_number: phone,
                        ..Default::default()
                    });
                }

                let mut bot = builder
                    .on_event(move |event, _client| {
                        let event_tx = event_tx.clone();
                        let callbacks = callbacks.clone();
                        async move {
                            dispatch_event(&event, &event_tx, &callbacks);
                        }
                    })
                    .build()
                    .await
                    .context("build bot")?;

                let client = bot.client();
                let handle = bot.run().await.context("run bot")?;
                anyhow::Ok((client, handle))
            })
            .map_err(|e| PyRuntimeError::new_err(format!("connect failed: {e:#}")))?;

        self.client = Some(client);
        self.handle = Some(handle);
        Ok(())
    }

    /// Block until the device is connected (paired and online), or raise
    /// `TimeoutError` after `timeout_secs`.
    #[pyo3(signature = (timeout_secs = 120.0))]
    fn wait_until_ready(&self, py: Python<'_>, timeout_secs: f64) -> PyResult<()> {
        let client = self
            .client
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("not connected — call connect() first"))?
            .clone();
        let runtime = self.runtime.clone();
        let deadline = std::time::Instant::now() + Duration::from_secs_f64(timeout_secs);

        py.allow_threads(|| {
            runtime.block_on(async move {
                loop {
                    if client.is_logged_in() {
                        return Ok(());
                    }
                    if std::time::Instant::now() >= deadline {
                        return Err(PyTimeoutError::new_err(
                            "timed out waiting for WhatsApp login — pair the device first",
                        ));
                    }
                    tokio::time::sleep(Duration::from_millis(250)).await;
                }
            })
        })
    }

    /// Returns `True` if the device is paired and online.
    fn is_connected(&self) -> bool {
        self.client
            .as_ref()
            .map(|c| c.is_logged_in())
            .unwrap_or(false)
    }

    /// The phone number this device is paired to, as raw digits.
    /// `None` before pairing completes. Read from `device.pn` snapshot.
    fn own_phone(&self, py: Python<'_>) -> PyResult<Option<String>> {
        let Some(client) = self.client.as_ref().cloned() else {
            return Ok(None);
        };
        let runtime = self.runtime.clone();
        let device = py.allow_threads(|| {
            runtime
                .block_on(async move { client.persistence_manager().get_device_snapshot().await })
        });
        Ok(device.pn.as_ref().map(|j| j.user.to_string()))
    }

    /// Force the persistence manager to drain queued device state to the
    /// SQLite backend immediately. Without this, fresh writes may sit in
    /// memory for up to 30 s (the background saver interval) and a
    /// just-paired in-memory session would round-trip empty through
    /// `export_session`.
    fn flush(&self, py: Python<'_>) -> PyResult<()> {
        let client = self
            .client
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("not connected"))?
            .clone();
        let runtime = self.runtime.clone();
        py.allow_threads(|| {
            runtime.block_on(async move { client.persistence_manager().flush().await })
        })
        .map_err(|e| PyRuntimeError::new_err(format!("flush failed: {e:#}")))
    }

    /// Send a plain text message. `to` accepts:
    ///   - "919876543210"               (E.164 digits → s.whatsapp.net)
    ///   - "+91 98765 43210"            (spaces / + stripped)
    ///   - "919876543210@s.whatsapp.net" (full JID)
    ///   - "120363012345678901@g.us"    (group JID)
    fn send_text(&self, to: &str, text: String) -> PyResult<String> {
        let client = self.require_client()?;
        let jid = parse_jid(to)?;
        let runtime = self.runtime.clone();

        let msg = wa::Message {
            conversation: Some(text),
            ..Default::default()
        };

        runtime
            .block_on(client.send_message(jid, msg))
            .map(|r| r.message_id)
            .map_err(|e| PyRuntimeError::new_err(format!("send_text failed: {e:#}")))
    }

    /// Send an image. `data` is either a filesystem path (str) or bytes.
    #[pyo3(signature = (to, data, caption = None))]
    fn send_image(
        &self,
        py: Python<'_>,
        to: &str,
        data: PyObject,
        caption: Option<String>,
    ) -> PyResult<String> {
        let (bytes, _) = bytes_from_py(py, &data)?;
        let mime = sniff_mime_image(&bytes).unwrap_or("image/jpeg").to_string();
        self.send_media_inner(
            to,
            bytes,
            MediaType::Image,
            MediaKind::Image,
            mime,
            caption,
            None,
        )
    }

    /// Send a document (any file). `data` is a filesystem path or bytes.
    /// When `data` is a path, `filename` defaults to its basename.
    #[pyo3(signature = (to, data, filename = None, mimetype = None))]
    fn send_document(
        &self,
        py: Python<'_>,
        to: &str,
        data: PyObject,
        filename: Option<String>,
        mimetype: Option<String>,
    ) -> PyResult<String> {
        let (bytes, derived_name) = bytes_from_py(py, &data)?;
        let mime = mimetype.unwrap_or_else(|| "application/octet-stream".to_string());
        let fname = filename
            .or(derived_name)
            .unwrap_or_else(|| "document".to_string());
        self.send_media_inner(
            to,
            bytes,
            MediaType::Document,
            MediaKind::Document,
            mime,
            None,
            Some(fname),
        )
    }

    /// Register a callback for QR-code events. Receives a single str argument
    /// (the raw QR data to encode). Replaces any previous callback.
    fn set_on_qr(&self, cb: PyObject) {
        *self.callbacks.on_qr.lock().unwrap() = Some(cb);
    }

    /// Register a callback for phone pair-code events. Receives the 8-char
    /// code as a single str argument.
    fn set_on_pair_code(&self, cb: PyObject) {
        *self.callbacks.on_pair_code.lock().unwrap() = Some(cb);
    }

    /// Register a callback for incoming messages. Receives one dict argument
    /// with keys: chat, sender, is_group, is_from_me, id, push_name,
    /// timestamp, text, media_type.
    fn set_on_message(&self, cb: PyObject) {
        *self.callbacks.on_message.lock().unwrap() = Some(cb);
    }

    fn set_on_connected(&self, cb: PyObject) {
        *self.callbacks.on_connected.lock().unwrap() = Some(cb);
    }

    fn set_on_disconnect(&self, cb: PyObject) {
        *self.callbacks.on_disconnect.lock().unwrap() = Some(cb);
    }

    /// Pull the next event from the queue. Blocks up to `timeout_secs`.
    /// Returns `None` on timeout. Returns a dict with `{"kind": "...", ...}`.
    /// `kind` is one of: "qr", "pair_code", "connected", "logged_out", "message".
    #[pyo3(signature = (timeout_secs = 1.0))]
    fn next_event(&self, py: Python<'_>, timeout_secs: f64) -> PyResult<Option<PyObject>> {
        let rx = self.event_rx.clone();
        let ev = py.allow_threads(|| rx.recv_timeout(Duration::from_secs_f64(timeout_secs)));
        match ev {
            Ok(e) => Ok(Some(event_to_pydict(py, e)?)),
            Err(_) => Ok(None),
        }
    }

    /// Drain all queued events without blocking. Returns a list.
    fn drain_events(&self, py: Python<'_>) -> PyResult<Vec<PyObject>> {
        let mut out = Vec::new();
        while let Ok(e) = self.event_rx.try_recv() {
            out.push(event_to_pydict(py, e)?);
        }
        Ok(out)
    }

    /// Disconnect cleanly. Idempotent.
    fn disconnect(&mut self, py: Python<'_>) -> PyResult<()> {
        if let Some(client) = self.client.as_ref().cloned() {
            let runtime = self.runtime.clone();
            py.allow_threads(|| runtime.block_on(client.disconnect()));
        }
        if let Some(handle) = self.handle.take() {
            handle.abort();
        }
        self.client = None;
        Ok(())
    }
}

// ─── Helpers ─────────────────────────────────────────────────────────────

enum MediaKind {
    Image,
    Document,
}

impl WhatsApp {
    fn require_client(&self) -> PyResult<Arc<Client>> {
        self.client
            .clone()
            .ok_or_else(|| PyRuntimeError::new_err("not connected — call connect() first"))
    }

    #[allow(clippy::too_many_arguments)]
    fn send_media_inner(
        &self,
        to: &str,
        data: Vec<u8>,
        media_type: MediaType,
        kind: MediaKind,
        mimetype: String,
        caption: Option<String>,
        filename: Option<String>,
    ) -> PyResult<String> {
        let client = self.require_client()?;
        let jid = parse_jid(to)?;
        let runtime = self.runtime.clone();

        runtime
            .block_on(async move {
                let upload = client
                    .upload(data, media_type, Default::default())
                    .await
                    .context("upload media")?;

                let msg = match kind {
                    MediaKind::Image => wa::Message {
                        image_message: Some(Box::new(wa::message::ImageMessage {
                            url: Some(upload.url),
                            direct_path: Some(upload.direct_path),
                            media_key: Some(upload.media_key.to_vec()),
                            file_sha256: Some(upload.file_sha256.to_vec()),
                            file_enc_sha256: Some(upload.file_enc_sha256.to_vec()),
                            file_length: Some(upload.file_length),
                            mimetype: Some(mimetype),
                            caption,
                            ..Default::default()
                        })),
                        ..Default::default()
                    },
                    MediaKind::Document => wa::Message {
                        document_message: Some(Box::new(wa::message::DocumentMessage {
                            url: Some(upload.url),
                            direct_path: Some(upload.direct_path),
                            media_key: Some(upload.media_key.to_vec()),
                            file_sha256: Some(upload.file_sha256.to_vec()),
                            file_enc_sha256: Some(upload.file_enc_sha256.to_vec()),
                            file_length: Some(upload.file_length),
                            mimetype: Some(mimetype),
                            file_name: filename,
                            ..Default::default()
                        })),
                        ..Default::default()
                    },
                };

                let result = client.send_message(jid, msg).await.context("send")?;
                anyhow::Ok(result.message_id)
            })
            .map_err(|e| PyRuntimeError::new_err(format!("send_media failed: {e:#}")))
    }
}

fn parse_jid(input: &str) -> PyResult<Jid> {
    use std::str::FromStr;
    let trimmed = input.trim();
    if trimmed.contains('@') {
        return Jid::from_str(trimmed)
            .map_err(|e| PyValueError::new_err(format!("invalid JID {trimmed:?}: {e}")));
    }
    let digits: String = trimmed.chars().filter(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        return Err(PyValueError::new_err(format!(
            "cannot parse {trimmed:?} as a phone number — pass digits or a full JID"
        )));
    }
    Ok(Jid::pn(digits))
}

/// Accept bytes/bytearray or a filesystem path string. Returns the bytes plus
/// the basename derived from the path (if a path was given) so callers can
/// default a document's filename without re-parsing the original object.
fn bytes_from_py(py: Python<'_>, obj: &PyObject) -> PyResult<(Vec<u8>, Option<String>)> {
    if let Ok(b) = obj.extract::<&[u8]>(py) {
        return Ok((b.to_vec(), None));
    }
    if let Ok(path) = obj.extract::<String>(py) {
        let bytes = std::fs::read(&path)
            .map_err(|e| PyValueError::new_err(format!("cannot read {path:?}: {e}")))?;
        let basename = std::path::Path::new(&path)
            .file_name()
            .and_then(|n| n.to_str())
            .map(|s| s.to_string());
        return Ok((bytes, basename));
    }
    Err(PyValueError::new_err(
        "expected bytes or a filesystem path string",
    ))
}

fn sniff_mime_image(data: &[u8]) -> Option<&'static str> {
    match data {
        [0xFF, 0xD8, 0xFF, ..] => Some("image/jpeg"),
        [0x89, 0x50, 0x4E, 0x47, ..] => Some("image/png"),
        [0x47, 0x49, 0x46, ..] => Some("image/gif"),
        b if b.starts_with(b"RIFF") && b.len() > 11 && &b[8..12] == b"WEBP" => Some("image/webp"),
        _ => None,
    }
}

fn dispatch_event(event: &Event, tx: &Sender<PyEvent>, cbs: &Callbacks) {
    let py_ev = match event {
        Event::PairingQrCode { code, .. } => Some(PyEvent::Qr(code.clone())),
        Event::PairingCode { code, .. } => Some(PyEvent::PairCode(code.clone())),
        Event::Connected(_) => Some(PyEvent::Connected),
        Event::LoggedOut(_) => Some(PyEvent::LoggedOut),
        Event::Message(msg, info) => Some(PyEvent::Message {
            chat: info.source.chat.to_string(),
            sender: info.source.sender.to_string(),
            is_group: info.source.is_group,
            is_from_me: info.source.is_from_me,
            id: info.id.clone(),
            push_name: info.push_name.clone(),
            timestamp: info.timestamp.timestamp(),
            text: msg.text_content().map(|s| s.to_string()),
            media_type: info.media_type.clone(),
        }),
        _ => None,
    };

    let Some(py_ev) = py_ev else {
        return;
    };

    // Always enqueue so next_event()/drain_events() see it.
    let _ = tx.send(py_ev.clone());

    // Pick the slot for the corresponding user callback.
    let cb_slot = match &py_ev {
        PyEvent::Qr(_) => &cbs.on_qr,
        PyEvent::PairCode(_) => &cbs.on_pair_code,
        PyEvent::Connected => &cbs.on_connected,
        PyEvent::LoggedOut => &cbs.on_disconnect,
        PyEvent::Message { .. } => &cbs.on_message,
    };
    // Cheap check before acquiring the GIL — keep hot paths GIL-free.
    if cb_slot.lock().unwrap().is_none() {
        return;
    }

    Python::with_gil(|py| {
        let guard = cb_slot.lock().unwrap();
        let Some(cb) = guard.as_ref() else { return };
        let result = match &py_ev {
            PyEvent::Qr(s) | PyEvent::PairCode(s) => cb.call1(py, (s.clone(),)),
            PyEvent::Connected | PyEvent::LoggedOut => cb.call0(py),
            PyEvent::Message { .. } => match event_to_pydict(py, py_ev.clone()) {
                Ok(d) => cb.call1(py, (d,)),
                Err(e) => Err(e),
            },
        };
        if let Err(e) = result {
            e.print(py);
        }
    });
}

fn event_to_pydict(py: Python<'_>, ev: PyEvent) -> PyResult<PyObject> {
    let d = PyDict::new_bound(py);
    match ev {
        PyEvent::Qr(s) => {
            d.set_item("kind", "qr")?;
            d.set_item("code", s)?;
        }
        PyEvent::PairCode(s) => {
            d.set_item("kind", "pair_code")?;
            d.set_item("code", s)?;
        }
        PyEvent::Connected => {
            d.set_item("kind", "connected")?;
        }
        PyEvent::LoggedOut => {
            d.set_item("kind", "logged_out")?;
        }
        PyEvent::Message {
            chat,
            sender,
            is_group,
            is_from_me,
            id,
            push_name,
            timestamp,
            text,
            media_type,
        } => {
            d.set_item("kind", "message")?;
            d.set_item("chat", chat)?;
            d.set_item("sender", sender)?;
            d.set_item("is_group", is_group)?;
            d.set_item("is_from_me", is_from_me)?;
            d.set_item("id", id)?;
            d.set_item("push_name", push_name)?;
            d.set_item("timestamp", timestamp)?;
            d.set_item("text", text)?;
            d.set_item("media_type", media_type)?;
        }
    }
    Ok(d.into())
}

#[pymodule]
fn _wars(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<WhatsApp>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
