# The web app, for hosting (e.g. TrueNAS). Data (database + secret.key) lives in /data: mount a volume there.

FROM python:3.14-slim AS wheel
WORKDIR /src
COPY pyproject.toml LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    UPLOADER_DB=/data/uploader.db

RUN --mount=type=bind,from=wheel,source=/wheels,target=/wheels \
    pip install --no-cache-dir /wheels/*.whl

# Not root. 568 is the "apps" user of TrueNAS, so a dataset given to apps is writable too.
RUN groupadd --system --gid 568 uploader \
    && useradd --system --uid 568 --gid 568 --no-create-home --shell /usr/sbin/nologin uploader \
    && mkdir /data && chown uploader:uploader /data
USER uploader
WORKDIR /data

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"]
CMD ["python", "-m", "firefly_uploader", "serve", "--host", "0.0.0.0", "--port", "8765"]
