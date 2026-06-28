# syntax=docker/dockerfile:1.9
# Multi-target image for the Glasshouse backend.
#   dev    — slim, hot-reload; source is bind-mounted at runtime (docker-compose), .venv baked
#   api    — distroless prod; serves FastAPI via uvicorn
#   worker — distroless prod; runs the arq worker (WorkerSettings lands at M1.9)
# Build a target: docker build --target api -t glasshouse-api .
# The app runs from source (PYTHONPATH=/app); it is not pip-installed as a package.

ARG PYTHON_VERSION=3.12
# uv image tracks latest uv on a pinned Python+distro; digest-pin in M7.3 (CI hardening).
ARG UV_IMAGE=ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# ---- builder: resolve deps into /app/.venv with a relocatable managed Python ----
FROM ${UV_IMAGE} AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed
WORKDIR /app
# A standalone interpreter that survives the copy into distroless.
RUN uv python install ${PYTHON_VERSION}
# Dependencies only — the app runs from source, so we never build/install the project.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev
COPY . /app

# ---- dev: slim + hot-reload; deps baked so a named volume seeds /app/.venv ----
FROM ${UV_IMAGE} AS dev
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:${PATH}"
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project
EXPOSE 8000
# Source arrives via bind-mount; --reload watches it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---- prod base: distroless runtime + managed Python + the project venv + source ----
FROM gcr.io/distroless/cc-debian12 AS prod-base
COPY --from=builder /python /python
COPY --from=builder /app /app
ENV PYTHONPATH=/app \
    PATH="/app/.venv/bin:${PATH}"
WORKDIR /app
# Drop root — distroless ships a uid 65532 'nonroot'; runtime needs no writes (least privilege).
USER nonroot
EXPOSE 8000

# ---- api: FastAPI server ----
FROM prod-base AS api
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---- worker: arq worker (enabled at M1.9 when WorkerSettings exists) ----
FROM prod-base AS worker
ENTRYPOINT ["arq", "app.workers.queue.WorkerSettings"]
