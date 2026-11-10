FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    HOME=/data/home \
    TMPDIR=/data/tmp \
    SAL_USE_VCLPLUGIN=svp \
    DATA_DIR=/data \
    INPUT_DIR=/input \
    OUTPUT_DIR=/output \
    ARGOS_PRELOAD_DIR=/opt/argos-preload \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=5463

# The -nogui packages pull the same converters without the X11/GUI stack.
# They only ship soffice.bin, so expose the usual "soffice" command ourselves.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
      tesseract-ocr-jpn tesseract-ocr-kor tesseract-ocr-tha tesseract-ocr-deu \
      tesseract-ocr-jpn-vert tesseract-ocr-chi-sim-vert \
      tesseract-ocr-chi-tra-vert tesseract-ocr-kor-vert \
      tesseract-ocr-spa tesseract-ocr-fra tesseract-ocr-ita tesseract-ocr-ara \
      tesseract-ocr-heb tesseract-ocr-por tesseract-ocr-rus tesseract-ocr-vie \
      tesseract-ocr-nld tesseract-ocr-pol tesseract-ocr-tur tesseract-ocr-ukr \
      tesseract-ocr-hin \
      fonts-wqy-microhei fonts-noto-cjk fonts-noto-core fonts-dejavu-core fontconfig \
      libgomp1 libglib2.0-0 tini ca-certificates tzdata \
 && ln -sf /usr/lib/libreoffice/program/soffice.bin /usr/local/bin/soffice

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY app ./app
COPY docker/entrypoint.sh /usr/local/bin/linguaflow-entrypoint
RUN chmod +x /usr/local/bin/linguaflow-entrypoint \
 && mkdir -p /data /output /opt/argos-preload

EXPOSE 5463
VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/linguaflow-entrypoint"]
