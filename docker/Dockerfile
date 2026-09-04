# syntax=docker/dockerfile:1.7
# Toolbox for dropbox-mirror. The repo is bind-mounted at /work; this image holds
# the toolchain only, pinned by config/toolchain.lock.toml.
FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS fetch

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY config/toolchain.lock.toml /tmp/lock.toml

# Architecture is read from the image itself: BuildKit sets TARGETARCH, Apple
# container does not, and a defaulted arg would install amd64 binaries into an
# arm64 image.
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64|arm64) ;; *) echo "unsupported architecture: $arch" >&2; exit 1 ;; esac; \
    lock() { python -c "import functools,sys,tomllib; x=tomllib.load(open('/tmp/lock.toml','rb')); print(functools.reduce(lambda d,k: d[k], sys.argv[1].split('.'), x))" "$1"; }; \
    curl -fsSL "$(lock proton_drive_cli.linux_${arch}.url)" -o /tmp/proton-drive; \
    echo "$(lock proton_drive_cli.linux_${arch}.sha512)  /tmp/proton-drive" | sha512sum -c -; \
    install -m 0755 /tmp/proton-drive /usr/local/bin/proton-drive; \
    curl -fsSL "$(lock age.base_url)/$(lock age.linux_${arch}.archive)" -o /tmp/age.tgz; \
    echo "$(lock age.linux_${arch}.sha256)  /tmp/age.tgz" | sha256sum -c -; \
    tar -xzf /tmp/age.tgz -C /tmp; \
    install -m 0755 /tmp/age/age /tmp/age/age-keygen /usr/local/bin/; \
    curl -fsSL "$(lock task.base_url)/$(lock task.linux_${arch}.archive)" -o /tmp/task.tgz; \
    echo "$(lock task.linux_${arch}.sha256)  /tmp/task.tgz" | sha256sum -c -; \
    tar -xzf /tmp/task.tgz -C /tmp task; \
    install -m 0755 /tmp/task /usr/local/bin/task

FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS toolbox

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=fetch /usr/local/bin/proton-drive \
     /usr/local/bin/age /usr/local/bin/age-keygen /usr/local/bin/task /usr/local/bin/
COPY config/toolchain.lock.toml /etc/toolchain.lock.toml

RUN python - <<'PY'
import subprocess, tomllib
lock = tomllib.load(open("/etc/toolchain.lock.toml", "rb"))
pins = [f"{k}=={v}" for k, v in lock["python"]["packages"].items()]
pins += [f"{k}=={v}" for k, v in lock["python"]["test_packages"].items()]
subprocess.check_call(["pip", "install", "--no-cache-dir", "--no-deps", *pins])
PY

RUN python - <<'PY'
import importlib.metadata, platform, subprocess, tomllib
lock = tomllib.load(open("/etc/toolchain.lock.toml", "rb"))
assert platform.python_version() == lock["python"]["version"]
for table in ("packages", "test_packages"):
    for name, version in lock["python"][table].items():
        assert importlib.metadata.version(name) == version, name
first = lambda argv: subprocess.check_output(argv, text=True).splitlines()[0]
assert "@" + lock["proton_drive_cli"]["version"] in first(["proton-drive", "version"])
assert lock["age"]["version"] in first(["age", "--version"])
assert lock["task"]["version"] in subprocess.check_output(["task", "--version"], text=True)
PY

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/work/src \
    MIGRATOR_TOOLCHAIN_LOCK=/etc/toolchain.lock.toml \
    PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file \
    PROTON_DRIVE_CACHE_DIR=/work/.run/session \
    PROTON_DRIVE_LOG_LEVEL=INFO
# R2 has one region. This is a literal, not a secret, so it lives here rather than
# in 1Password: `op run` masks every value it resolves, and masking "auto" would
# corrupt ordinary output.
ENV AWS_REGION=auto
WORKDIR /work
