FROM python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ITM_BIND_HOST=0.0.0.0 \
    ITM_PORT=8000 \
    ITM_QUANT_DATA_DIR=/data

WORKDIR /app
COPY requirements.bootstrap.lock.txt requirements.production.lock.txt ./
RUN python -m pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: -r requirements.bootstrap.lock.txt \
    && python -m pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: -r requirements.production.lock.txt \
    && python -m pip check \
    && useradd --create-home --uid 10001 itmquant \
    && mkdir -p /data /app/app/storage \
    && chown -R itmquant:itmquant /data /app

# Runtime mínimo: no tests, CI, release tooling, Rust sources ni Solid reference shell.
COPY --chown=itmquant:itmquant app ./app
COPY --chown=itmquant:itmquant frontend/three-adapter ./frontend/three-adapter
COPY --chown=itmquant:itmquant run_always_on.py VERSION.txt .python-version .itm_quant_product.json README.md THIRD_PARTY_NOTICES.md ./

USER itmquant
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)"
CMD ["python", "run_always_on.py"]
