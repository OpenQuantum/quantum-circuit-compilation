FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git g++ make && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy==2.5.4 scipy==1.18.1 six==1.17.0 \
    'kickmix @ git+https://github.com/quantumlib/Qualtran.git@9a0c11a4ee7697c082fbf8f2e1e8bf4a71fcb453#subdirectory=kickmix'
ENV PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /candidate
