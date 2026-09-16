# syntax=docker/dockerfile:1
FROM python:3.13-slim

# El vigilante es Python puro (solo stdlib): no hay dependencias que instalar.
# - tzdata: las URLs de BOCyL/DOE y las fechas de los boletines se construyen con la
#   fecha local; sin zona horaria el contenedor pediria el dia anterior de madrugada.
# - ca-certificates: verificacion TLS de api.telegram.org y de los boletines.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Madrid \
    OPO_DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY opochecker.py setup_telegram.py especialidades.json config.json.example ./

# uid/gid 1000 = usuario rsa del servidor: los ficheros de ./data no quedan de root.
RUN groupadd -g 1000 opo \
 && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin opo \
 && mkdir -p /data \
 && chown -R opo:opo /data /app

USER opo
VOLUME ["/data"]

# El bucle de servicio reescribe /data/heartbeat cada ~25 s; si deja de hacerlo, el
# long polling esta colgado y conviene reiniciar (restart: unless-stopped lo hara).
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import os,sys,time; p=os.path.join(os.environ.get('OPO_DATA_DIR','/data'),'heartbeat'); sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 300 else 1)"

CMD ["python", "opochecker.py", "--serve", "--interval", "10"]
