FROM python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends git g++ make \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy==2.5.4 scipy==1.18.1 six==1.17.0 \
    'kickmix @ git+https://github.com/quantumlib/Qualtran.git@9a0c11a4ee7697c082fbf8f2e1e8bf4a71fcb453#subdirectory=kickmix'
ENV PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /candidate
