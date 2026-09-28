# AI Test Generator — production image (Render, Fly, any Docker host).
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/usr/local/go/bin:${PATH}"

ARG GO_VERSION=1.24.7

# Toolchains used by the analysis sandbox: compilers, linters and test runners.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg build-essential ruby php-cli default-jdk-headless rustc shellcheck \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g typescript@5 \
 && curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" | tar -C /usr/local -xz \
 && apt-get purge -y gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Untrusted code (user sources + generated tests) runs as this unprivileged user.
# The web server stays root only so it can drop privileges for each sandboxed process;
# the sandbox user cannot read the server's environment (API keys) or files.
RUN useradd --system --create-home --shell /usr/sbin/nologin sandbox

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend backend
COPY frontend frontend

WORKDIR /app/backend
ENV APP_ENV=production SANDBOX_USER=sandbox
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz" || exit 1
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
