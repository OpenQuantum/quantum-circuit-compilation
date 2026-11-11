FROM python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS worker

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends git g++ make \
    && rm -rf /var/lib/apt/lists/*

COPY validation/requirements-worker.txt /tmp/requirements-worker.txt
RUN python3.12 -m venv /opt/quantum-worker \
    && /opt/quantum-worker/bin/python -m pip install --no-cache-dir --no-binary=kickmix -r /tmp/requirements-worker.txt \
    && /opt/quantum-worker/bin/python -m pip check

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS world

# star_sdk is a named context containing vendor/star-protocol at c237172f.
COPY --from=star_sdk /pyproject.toml /README.md /build/star-protocol/
COPY --from=star_sdk /src/ /build/star-protocol/src/
COPY pyproject.toml /build/quantum-world/
COPY src/ /build/quantum-world/src/
RUN python3.13 -m venv /opt/quantum-world \
    && /opt/quantum-world/bin/python -m pip install --no-cache-dir /build/star-protocol '/build/quantum-world[http]' \
    && /opt/quantum-world/bin/python -m pip check

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY --from=world /opt/quantum-world/ /opt/quantum-world/
# Keep Python 3.12's versioned files alongside the World Python 3.13.
COPY --from=worker /usr/local/bin/python3.12 /usr/local/bin/python3.12
COPY --from=worker /usr/local/lib/libpython3.12.so.1.0 /usr/local/lib/libpython3.12.so.1.0
COPY --from=worker /usr/local/lib/python3.12/ /usr/local/lib/python3.12/
COPY --from=worker /opt/quantum-worker/ /opt/quantum-worker/

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && ldconfig \
    && /opt/quantum-worker/bin/python -c "import sys, numpy, pandas, kickmix; assert sys.version_info[:2] == (3, 12); assert numpy.__version__ == '2.5.4'; assert pandas.__version__ == '3.1.1'" \
    && /opt/quantum-world/bin/python -c "import sys, fastapi, star_protocol, uvicorn, quantum_world.operator_api; assert sys.version_info[:2] == (3, 13)"

ENV PATH="/opt/quantum-world/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
LABEL quantum.star-protocol.commit="c237172f788412a93a3647129572b546ca949603" \
      quantum.worker.kickmix.commit="9a0c11a4ee7697c082fbf8f2e1e8bf4a71fcb453"
EXPOSE 8321
CMD ["quantum-world", "--config", "/config/world.toml", "serve", "--operator-http", "--http-host", "0.0.0.0", "--http-port", "8321"]
