# Launching `wars` on GitHub + PyPI

End-to-end checklist to take the working code in this directory to a
published `pip install wars` package. Follow once, in order. Subsequent
releases are just `git tag vX.Y.Z && git push --tags`.

## 0. Pre-flight

- [ ] **`wars` is available on PyPI.** Check
  https://pypi.org/project/wars/ — if it 404s, you can claim it.
- [ ] **Linked WhatsApp devices < 4** (use `WhatsApp → Settings →
  Linked devices` to clean up old test sessions).
- [ ] You have push access to **`github.com/marketcalls`** (or change
  the org everywhere — see *Renaming* at the bottom).

## 1. Create the GitHub repo

```bash
# Create it empty on github.com (Private or Public, your call) at
# https://github.com/new   →   Owner: marketcalls   Name: wars
# Don't initialize with README/LICENSE/.gitignore — we have all of those.

# Locally:
cd ~/code
mkdir wars && cd wars
git init -b main
```

## 2. Copy this directory into the new repo

```bash
# Assuming the current setup at <wherever-you-developed>/whatsapp-rust/python:
cp -R /path/to/whatsapp-rust/python/. .

# Remove the venv and build artefacts before committing
rm -rf .venv target *.db qr.png miss_you.png _test_*.py
```

## 3. Add `whatsapp-rust` as a git submodule

We vendor the upstream Rust crate as a submodule tracking
`marketcalls/whatsapp-rust`'s `main` branch. Owning the fork protects
this repo from upstream rewrites/deletions; tracking `main` lets you
fast-forward easily.

```bash
git submodule add -b main https://github.com/marketcalls/whatsapp-rust.git vendor/whatsapp-rust
git add .gitmodules vendor/whatsapp-rust
```

To bump to a newer upstream commit later:

```bash
git submodule update --remote vendor/whatsapp-rust
git add vendor/whatsapp-rust
git commit -m "Bump whatsapp-rust submodule"
```

Then edit `Cargo.toml` and replace every `path = "../<x>"` with
`path = "vendor/whatsapp-rust/<x>"`. Concretely:

```toml
whatsapp-rust = { path = "vendor/whatsapp-rust", default-features = false, features = [
    "sqlite-storage", "tokio-transport", "tokio-runtime", "tokio-native", "ureq-client",
] }
wacore                              = { path = "vendor/whatsapp-rust/wacore",          default-features = false }
wacore-binary                       = { path = "vendor/whatsapp-rust/wacore/binary",   default-features = false }
waproto                             = { path = "vendor/whatsapp-rust/waproto" }
whatsapp-rust-tokio-transport       = { path = "vendor/whatsapp-rust/transports/tokio-transport" }
whatsapp-rust-ureq-http-client      = { path = "vendor/whatsapp-rust/http_clients/ureq-client" }
```

Verify locally:

```bash
uv venv && source .venv/bin/activate
uv pip install maturin
maturin develop --release
python -c "import wars; print(wars.__version__)"
```

## 4. First commit & push

```bash
git add .
git commit -m "Initial commit: wars 0.1.0 — WhatsApp Python bindings"
git remote add origin git@github.com:marketcalls/wars.git
git push -u origin main
```

## 5. Configure PyPI Trusted Publishing (no API tokens!)

1. Go to https://pypi.org/manage/account/publishing/
2. Click **Add a new publisher** → **GitHub**
3. Fill in:
   - PyPI project name: `wars`
   - Owner: `marketcalls`
   - Repository: `wars`
   - Workflow filename: `release.yml`
   - Environment: `pypi`
4. Submit.

In your repo: **Settings → Environments → New environment → `pypi`**.
No protection rules needed for v0.1; you can add reviewers later.

## 6. First release

```bash
git tag v0.1.0
git push --tags
```

GitHub Actions will:

1. Build wheels for 5 platforms in parallel (linux x86_64+aarch64,
   macos x86_64+arm64, windows x86_64) — ~5 minutes total.
2. Publish all wheels to PyPI via OIDC. No `PYPI_TOKEN` ever touches
   the repo.

Watch progress at `https://github.com/marketcalls/wars/actions`. When
it goes green, `pip install wars` works for anyone on a supported
platform.

## 7. Verify the published package

From a clean Python environment on any machine:

```bash
uv venv && source .venv/bin/activate
uv pip install wars
python -c "from wars import WhatsApp; print('ok')"
```

## Subsequent releases

```bash
# Bump version in pyproject.toml AND Cargo.toml
sed -i 's/^version = "0.1.0"/version = "0.1.1"/' pyproject.toml Cargo.toml
git commit -am "Release v0.1.1"
git tag v0.1.1
git push --tags
```

CI does the rest.

## Renaming the org / repo

If you'd rather publish under a different GitHub org, update these
places in lockstep:

- `pyproject.toml` → `[project.urls]` block
- `README.md` → curl URL in *Quick start §1*
- `LAUNCH.md` (this file) → all references
- PyPI Trusted Publisher config → must match `<owner>/<repo>/<workflow>`

Everything else is org-agnostic.
