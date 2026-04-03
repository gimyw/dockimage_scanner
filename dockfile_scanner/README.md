# imgadvisor

Dockerfile pre-build static analyzer and image optimization advisor.

`imgadvisor` reads a Dockerfile before build time, flags image bloat and risky runtime defaults, and can generate an optimized Dockerfile. The current optimization path is deepest for Python images: it can rebuild single-stage Python Dockerfiles into concrete multi-stage output instead of only printing a generic template.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/0206pdh/dockimage_scanner/main/install.sh | bash
source ~/.bashrc
```

The installer creates `~/.imgadvisor` and exposes `imgadvisor` through `~/.local/bin`.

Requirements:
- Python 3.11+
- Docker daemon for `layers` and `validate`
- Trivy for `scan`

## Commands

| Command | Docker required | Purpose |
|---|---|---|
| `analyze` | No | Static analysis of Dockerfile issues |
| `recommend` | No | Generate an optimized Dockerfile |
| `layers` | Yes | Build image and inspect layer sizes |
| `validate` | Yes | Build original vs optimized Dockerfiles and compare results |
| `scan` | No | Run Trivy pre-build config and filesystem scans |

## Python-Focused Optimization

The current implementation is intentionally Python-first.

For Python Dockerfiles, `imgadvisor` can:
- detect single-stage runtime images that still contain build dependencies
- generate a real builder/runtime split instead of appending a comment-only template
- create and copy a dedicated virtualenv from builder to runtime
- prefer `python:*‑slim` for runtime output
- normalize `apt` and `pip` commands with cleanup flags
- use manifest-first dependency layers when `requirements*.txt`, `constraints*.txt`, `pyproject.toml`, or `poetry.lock` exist
- inject container-safe Python env defaults
- rewrite some runtime entrypoints conservatively

Python runtime defaults currently handled:
- `PYTHONUNBUFFERED=1`
- `PYTHONDONTWRITEBYTECODE=1`
- `PIP_NO_CACHE_DIR=1`
- `PIP_DISABLE_PIP_VERSION_CHECK=1`

Python runtime command handling:
- `flask run` is flagged as a development server and, when inference is safe enough, the generated multi-stage Dockerfile switches to `gunicorn`
- `uvicorn` without `--workers` is flagged and the generated multi-stage Dockerfile adds `--workers 2`

## Typical Workflow

```bash
imgadvisor analyze -f Dockerfile
imgadvisor recommend -f Dockerfile -o optimized.Dockerfile
imgadvisor validate -f Dockerfile --optimized optimized.Dockerfile
```

If you want actual layer-level evidence before rewriting:

```bash
imgadvisor layers -f Dockerfile
```

If you want pre-build security checks:

```bash
imgadvisor scan -f Dockerfile
```

## Examples

Analyze a Dockerfile:

```bash
imgadvisor analyze -f Dockerfile
```

Generate an optimized Dockerfile:

```bash
imgadvisor recommend -f Dockerfile -o optimized.Dockerfile
```

Validate actual size reduction:

```bash
imgadvisor validate -f Dockerfile --optimized optimized.Dockerfile
```

Run Trivy pre-build checks:

```bash
imgadvisor scan -f Dockerfile --severity HIGH,CRITICAL
```

## Main Rules

### Base image

Flags oversized base images and recommends smaller compatible options. For Python, the current generated multi-stage path prefers a conservative slim runtime instead of aggressive runtime swaps that often break package-manager compatibility.

### Build tools in final stage

Flags compilers and development packages left in the final runtime image.

### Cache cleanup

Flags package manager installs that leave cache behind, such as:
- `apt-get install` without apt-list cleanup
- `pip install` without `--no-cache-dir`

### Python runtime defaults

Flags missing or conflicting Python runtime env values and development-oriented runtime commands.

### Broad copy scope

Flags `COPY . .`-style instructions that bring the whole build context into the image, especially when `.dockerignore` is missing.

### Single-stage Python build

Flags Python Dockerfiles that should become multi-stage and generates a concrete builder/runtime Dockerfile from the original instruction stream.

## What `recommend` Does For Python

When the final stage is Python and the Dockerfile has meaningful optimization signals, `recommend` can generate output like:

```dockerfile
# -- builder stage --
FROM python:3.11 AS builder
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN python -m venv $VIRTUAL_ENV
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# -- runtime stage --
FROM python:3.11-slim
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
```

This is not a fixed template pasted blindly. The generator reads the parsed Dockerfile, keeps the instruction order where it can, and changes the runtime stage only where there is a concrete optimization reason.

## Trivy Pre-Build Scan

`scan` combines:
- `trivy config` for Dockerfile misconfiguration checks
- `trivy fs` for dependency vulnerability checks from the build context

Example:

```bash
imgadvisor scan -f Dockerfile --ignore-unfixed
```

## Project Layout

```text
imgadvisor/
├── main.py
├── parser.py
├── analyzer.py
├── recommender.py
├── validator.py
├── layer_analyzer.py
├── trivy_scanner.py
├── display.py
├── models.py
└── rules/
    ├── base_image.py
    ├── build_tools.py
    ├── cache_cleanup.py
    ├── copy_scope.py
    ├── multi_stage.py
    └── python_runtime.py
```

## Current Scope

The deepest rewrite logic is Python-specific by design. Other language ecosystems may still be analyzed by broader rules, but the concrete multi-stage reconstruction path is currently focused on Python.
