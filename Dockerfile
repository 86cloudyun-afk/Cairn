FROM ghcr.io/astral-sh/uv:python3.13-trixie

COPY ./cairn/pyproject.toml /cairn/pyproject.toml
COPY ./cairn/uv.lock /cairn/uv.lock
WORKDIR /cairn
RUN --mount=type=secret,id=ca_bundle \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-install-project -i https://mirrors.aliyun.com/pypi/simple/

COPY ./cairn /cairn
RUN --mount=type=secret,id=ca_bundle \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen -i https://mirrors.aliyun.com/pypi/simple/

ENV TZ=Asia/Shanghai
