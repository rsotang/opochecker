#!/bin/bash
# opochecker-check.sh - vigila el servicio Opochecker y reporta a uptime-kuma.
#
# Este script vive fuera del contenedor: inspecciona el contenedor, el heartbeat
# del bucle de servicio y el state.json, y publica el resultado en un monitor
# "Push" de uptime-kuma. Si deja de publicar, kuma marca el monitor como caido.
#
# Instalacion (ver README, seccion "Monitorizacion"):
#   mkdir -p ~/bin && cp monitor/opochecker-check.sh ~/bin/opochecker-check.sh
#   cat > ~/.opochecker-monitor.env <<'EOF'
#   OPOCHECKER_PUSH_TOKEN=<token del monitor push de kuma>
#   EOF
#   chmod 600 ~/.opochecker-monitor.env && chmod 750 ~/bin/opochecker-check.sh
#   crontab -e     # */5 * * * * /home/rsa/opochecker-check.sh >/dev/null 2>&1
#
# El token y el vigilante externo van en ~/.opochecker-monitor.env y NO se versionan:
# el token de push es una credencial de escritura del monitor.
PATH=/usr/local/bin:/usr/bin:/bin
set -u

[ -f "$HOME/.opochecker-monitor.env" ] && . "$HOME/.opochecker-monitor.env"

PUSH_BASE="${OPOCHECKER_PUSH_BASE:-http://127.0.0.1:3001/api/push}"
PUSH_TOKEN="${OPOCHECKER_PUSH_TOKEN:-}"
EXTERNO="${OPOCHECKER_EXTERNO:-}"            # dead-man's switch externo (opcional)
DATA="${OPOCHECKER_DATA:-/opt/docker/stacks/opochecker/data}"
CONT="${OPOCHECKER_CONT:-opochecker}"
MAX_HEARTBEAT=300                            # el servicio reescribe el heartbeat cada ~25 s
MAX_LASTCHECK=1800                           # los checks van cada 10 min: 3 sin ejecutarse = algo va mal

if [ -z "$PUSH_TOKEN" ]; then
  echo "opochecker-check: falta OPOCHECKER_PUSH_TOKEN (ver ~/.opochecker-monitor.env)" >&2
  exit 1
fi
PUSH="$PUSH_BASE/$PUSH_TOKEN"

ahora=$(date +%s)
problemas=""
extra=""

# 1) contenedor en marcha y healthcheck en verde
estado=$(docker inspect -f '{{.State.Status}}/{{.State.Health.Status}}' "$CONT" 2>/dev/null)
[ -n "$estado" ] || estado="ausente"
[ "$estado" = "running/healthy" ] || problemas="$problemas contenedor=$estado"

# 2) el bucle de servicio (long polling) sigue vivo: heartbeat reciente
if [ -f "$DATA/heartbeat" ]; then
  edad=$(( ahora - $(stat -c %Y "$DATA/heartbeat") ))
  [ "$edad" -le "$MAX_HEARTBEAT" ] || problemas="$problemas heartbeat=${edad}s"
else
  problemas="$problemas sin-heartbeat"
fi

# 3) los checks de boletines se ejecutan de verdad
ult=$(python3 - "$DATA/state.json" <<'PY' 2>/dev/null
import json, sys
print(json.load(open(sys.argv[1])).get("last_check", ""))
PY
)
if [ -n "$ult" ]; then
  edad=$(( ahora - $(date -d "$ult" +%s) ))
  [ "$edad" -le "$MAX_LASTCHECK" ] || problemas="$problemas last_check=${edad}s"
else
  problemas="$problemas sin-last_check"
fi

# 4) errores recientes del registro: informativo, NO baja el monitor
errs=$(tail -n 300 "$DATA/opochecker.log" 2>/dev/null | grep -c ERROR)
[ "${errs:-0}" -gt 0 ] && extra=" errores_log=$errs"

msg=$(printf '%s' "${problemas# }$extra" | tr ' ' '_' | tr -d '&?#')
if [ -z "$msg" ]; then msg=OK; fi
if [ -z "$problemas" ]; then st=up; else st=down; fi

curl -fsS --max-time 15 "$PUSH?status=$st&msg=$msg&ping=" -o /dev/null
[ -n "$EXTERNO" ] && curl -fsS --max-time 15 "$EXTERNO$([ "$st" = up ] && echo "" || echo "/fail")" -o /dev/null

# 5) recuperacion: si esta unhealthy y el bucle lleva >10 min parado, reiniciar (max. 1/15 min)
marca=/tmp/opochecker-restart
if [ "$st" = down ] && [ "$estado" != "running/healthy" ] && [ -f "$DATA/heartbeat" ] \
   && [ $(( ahora - $(stat -c %Y "$DATA/heartbeat") )) -gt 600 ] \
   && [ "$(cat "$marca" 2>/dev/null || echo 0)" -lt $(( ahora - 900 )) ]; then
  echo "$ahora" > "$marca"
  docker restart "$CONT" >/dev/null 2>&1
fi

exit 0
