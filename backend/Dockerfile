# Backend image: FastAPI + Geo stack + onnxruntime for DEEPNESS inference.
# Versions are pinned in requirements.txt — that file is the reproducibility basis.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Geo stack system deps: GDAL, PROJ, GEOS (needed by rasterio/pyproj/shapely wheels
# carry their own libs, but we install OS-level GDAL too so QGIS-style tooling is
# possible without extra layers). QGIS-Server can be added later via PPA if needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
        proj-data \
        proj-bin \
        # weasyprint runtime deps (PDF export)
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libcairo2 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app /app/app

# Pre-flight runs at container start and aborts with a clear message
# if any required condition is missing (see app/preflight.py).
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
