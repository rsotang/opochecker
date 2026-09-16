# Opochecker

Vigilante de los **boletines oficiales de las comunidades autonomas espanolas** en busca de
documentos sobre **oposiciones/concursos de facultativos especialistas** (medicos especialistas
del sistema sanitario publico). Cuando aparece algo nuevo, envia un aviso a **Telegram**.

Python puro (solo stdlib), sin dependencias que instalar. Funciona en Windows y en
Linux/Docker (servicio 24/7 recomendado, ver [Donde ejecutarlo](#donde-ejecutarlo)).

## Como funciona

- El servicio (`--serve`) escucha Telegram en continuo (long polling): los comandos se
  atienden al instante, no en la siguiente pasada.
- Cada 10 minutos consulta las fuentes activas de `config.json` (RSS o paginas de sumario
  del dia) y extrae los anuncios.
- Filtra por usuario: cada chat tiene sus keywords (base + especialidades + extras) y solo
  recibe lo que cumple las suyas.
- Compara con `state.json` (memoria por chat) para no repetir avisos.
- Cada documento nuevo se envia como mensaje de Telegram con titulo y enlace.
- El log rota solo (5 MB x 3) en el directorio de datos: `opochecker.log`.

## Fuentes cubiertas (activas)

| CC.AA. | Boletin | Tipo |
|--------|---------|------|
| Madrid | BOCM | sumario del dia |
| Castilla y Leon | BOCyL | sumario del dia |
| Pais Vasco | BOPV | RSS (ultimo boletin) |
| Galicia | DOG | RSS seccion "Oposicions e concursos" |
| Canarias | BOC | boletin del dia |
| Navarra | BON | sumario del dia |
| Castilla-La Mancha | DOCM | portada |
| Extremadura | DOE | boletin del dia |
| Asturias | BOPA | sumario del dia |
| Andalucia | BOJA | RSS secciones "Oposiciones, concursos" y "Anuncios" |
| Baleares | BOIB | secciones "Autoritats i personal" y "Anuncis" del ultimo numero |
| Murcia | BORM | API JSON del sumario diario |

**Pendientes** (en `config.json` con `"enabled": false`): Aragon (BOA) — tiene una API
JSON de datos abiertos descubierta (`SEC=OPENDATABOAJSONELI`) pero solo responde a
navegadores; Cataluna (DOGC) — API REST descubierta (`/eadop-rest/api/dogc/...`) pero
da 404 en produccion; C. Valenciana (DOGV), Cantabria y La Rioja (BOR) — aplicaciones
JavaScript con sesion/bloqueo anti-bot.

## Configuracion (una sola vez)

### 1. Crear el bot de Telegram (2 minutos)

1. En Telegram, habla con **@BotFather** y envia `/newbot`.
2. Elige un nombre y un usuario (terminado en `bot`). BotFather te dara un **token** como
   `123456789:AAE...`.
3. Habla con tu bot nuevo y enviate `/start` (o cualquier mensaje).

### 2. Configurar el token (asi de facil)

Ejecuta el asistente:

```bat
python setup_telegram.py
```

Te pedira el token, lo validara contra la API de Telegram (te dira el nombre de tu bot),
**detecta automaticamente tu chat_id** entre los chats que hayan hablado con el bot,
envia un mensaje de prueba y guarda todo en `config.json`.

> Importante: el `chat_id` es un **numero** (tu chat personal, normalmente negativo si es
> un grupo). No es el nombre de usuario del bot. Para que aparezca en el asistente, envia
> antes `/start` al bot desde tu Telegram.

Si prefieres editarlo a mano: `config.json` -> `telegram.bot_token` y `telegram.chat_id`.
Tambien puedes usar las variables de entorno `OPO_TELEGRAM_TOKEN` y `OPO_TELEGRAM_CHAT_ID`
(prioritarias sobre el archivo).

### 3. Comprobar

```bat
python opochecker.py --test         REM diagnostica el token y el chat_id con detalle
python opochecker.py --verify       REM muestra lo que extrae cada boletin (sin avisar)
python opochecker.py --check        REM comprueba y avisa por Telegram de novedades
```

`--test` te dice exactamente que falla: token invalido, chat_id incorrecto, o todo correcto.

## Comandos del bot (por Telegram)

Escribe estos comandos en el chat con tu bot (se atienden al instante: el servicio escucha
Telegram en continuo):

| Comando | Que hace |
|---------|----------|
| `/especialidades` | **Selector de especialidades**: envia la lista de 34 especialidades medicas y facultativas con botones para activar/desactivar cada una. Al pulsar un boton se muestra la keyword asociada (cardiologia -> "cardiolog", pediatria -> "pediatr", anestesiologia -> "anestesio", etc.). Las especialidades activas se suman a la monitorizacion |
| `/keywords` | Muestra las keywords base, las de tus especialidades activas y las tuyas anadidas |
| `/base on\|off` | Activa o desactiva las keywords base (las generales: "facultativo + especialista", etc.). Con `off` solo recibes lo de tus especialidades y extras, util si te interesa una especialidad concreta. No deja desactivarlas si no tienes ninguna propia (te quedarias sin avisos) |
| `/addkw termino1 termino2` | Anade tu propia keyword: un grupo donde el anuncio debe contener TODOS los terminos |
| `/delkw numero` | Elimina una keyword anadida por ti (usa /keywords para ver los numeros) |
| `/resetkw` | Borra todas tus keywords anadidas |
| `/backtrack [dias]` | Revisa los ultimos N dias (30 por defecto) de los boletines con historico (Castilla y Leon, Extremadura, Asturias, Navarra, Canarias, Murcia) y te envia los anuncios de facultativos que no te habian llegado. Los envios se marcan como vistos para no repetirlos. |
| `/misespecialidades` | Resumen de tus especialidades y keywords efectivas |
| `/status` | Estado del vigilante (fuentes activas, etc.) |
| `/ayuda` | Esta ayuda |

Tambien funciona en local: `python opochecker.py --backtrack --days 30` (envia por
Telegram) o `--dry-run` para ver el resultado sin enviarlo.

Nota: BOCM (Madrid), BOCyL si, etc. — el historico cubre los boletines con acceso por
fecha o archivo: BOCyL, DOE, BOPA, BON y BOC-Canarias. El resto (BOCM, BOPV, DOG, DOCM,
BOJA) se vigilan en tiempo real desde que el bot esta activo, pero no tienen acceso
historico sencillo para el retroceso.

## Donde ejecutarlo

### Opcion A (recomendada): tu servidor con Docker

El servicio corre 24/7 en el servidor: atiende Telegram al instante (long polling) y revisa
los boletines cada 10 minutos. No depende de tu PC ni de GitHub.

```text
/opt/docker/stacks/opochecker/     <- clon del repositorio
    compose.yaml
    Dockerfile
    .env                           <- token y chat_id (no se sube a git)
    data/                          <- estado, usuarios y log (volumen, no se sube a git)
```

Puesta en marcha (una sola vez):

```bash
ssh rsa@rsa-servidor
git clone https://github.com/rsotang/opochecker.git /opt/docker/stacks/opochecker
cd /opt/docker/stacks/opochecker
mkdir -p data
cp .env.example .env && nano .env        # rellena token y chat_id
cp state.json usuarios.json data/        # conserva la memoria de avisos ya enviados
docker compose up -d --build
docker compose logs -f
```

El contenedor arranca con `restart: unless-stopped`, asi que se levanta solo al reiniciar el
servidor. El `HEALTHCHECK` vigila el bucle de servicio (fichero `data/heartbeat`): si el long
polling se cuelga, `docker ps` lo marca como `unhealthy` y el contenedor se reinicia; tambien
lo puedes vigilar desde uptime-kuma.

Actualizar el codigo:

```bash
cd /opt/docker/stacks/opochecker
git pull
docker compose up -d --build
```

Datos y secretos:
- `data/state.json`: memoria de lo ya notificado, por chat (el formato antiguo se migra solo).
- `data/usuarios.json`: especialidades, keywords extras y ajustes de cada usuario.
- `.env`: token y chat_id. Equivalen a `telegram.bot_token` y `telegram.chat_id` del
  `config.json`; si el fichero los deja vacios, mandan las variables de entorno.
- Para editar fuentes o keywords en el servidor sin recompilar: copia `config.json.example`
  a `config.json` en la carpeta del stack y descomenta el volumen correspondiente en
  `compose.yaml`.
- Acceso: por defecto **cualquiera que encuentre el bot** puede suscribirse. Para cerrarlo,
  anade `"access": {"allowed_chats": ["<tu_chat_id>"]}` a `config.json` (o al `config.json`
  montado); si la lista esta vacia, no hay restriccion.

Importante: **solo un proceso puede llamar a `getUpdates` del mismo bot**. En produccion es
este contenedor (`--serve`). No dejes a la vez un cron en Windows o el flujo de GitHub
llamando al bot, o se robaran comandos entre ellos.

### Opcion B: tu PC con Windows (respaldo)

```bat
python opochecker.py --install-schedule      REM tarea cada 30 min (Programador de tareas)
python opochecker.py --uninstall-schedule
```

La tarea programada ejecuta `--check`: revisa boletines y avisa, pero **no atiende comandos**
(para eso esta `--serve`, en una sola instancia).

O, sin Programador, arranque oculto al iniciar sesion:

```bat
python opochecker.py --install-startup
python opochecker.py --uninstall-startup
```

Tambien puedes arrancarlo a mano:
- Atendiendo Telegram: `python opochecker.py --serve`
- Solo checks, sin atender Telegram: `python opochecker.py --loop --interval 30`
- Sin ventana: doble clic en `arrancar_oculto.vbs`

Nota: `--loop` no contesta comandos a proposito, para que no haya dos procesos hablando con
Telegram. Si el PC es tu unica instancia, usa `--serve`.

### Opcion C: GitHub Actions (descartada)

Se uso al principio, pero GitHub descarta la mayoria de las ejecuciones programadas: con
`cron: */10` solo se ejecutaron **48 de 1008** en una semana (~4,8%), de modo que un comando
de Telegram tardaba de media ~3,5 h en atenderse. Por eso
`.github/workflows/opochecker.yml` ya **no tiene `schedule`**: queda como diagnostico manual
(`--verify`, solo lectura) para detectar que un boletin ha cambiado su HTML.

## Monitorizacion (opcional)

El servicio se puede vigilar con uptime-kuma (o cualquier herramienta con monitores "push"):
`monitor/opochecker-check.sh` comprueba cada 5 minutos el contenedor, el heartbeat del
servicio y `last_check`, y publica el resultado en un monitor Push. Si el bot deja de
funcionar, kuma avisa por Telegram.

```bash
# en el servidor, dentro del clon del repositorio
cp monitor/opochecker-check.sh /home/rsa/opochecker-check.sh
chmod 750 /home/rsa/opochecker-check.sh
cat > /home/rsa/.opochecker-monitor.env <<'EOF'
OPOCHECKER_PUSH_TOKEN=<token del monitor Push de kuma>
#OPOCHECKER_EXTERNO=https://hc-ping.com/<uuid>      # vigilante externo (opcional)
EOF
chmod 600 /home/rsa/.opochecker-monitor.env
crontab -e     # */5 * * * * /home/rsa/opochecker-check.sh >/dev/null 2>&1
```

El token del monitor **no se versiona**: va en `~/.opochecker-monitor.env` porque es una
credencial de escritura del monitor (con el repo publico, cualquiera podria falsear tus
heartbeats). Variables que admite el script: `OPOCHECKER_PUSH_BASE` (por defecto
`http://127.0.0.1:3001/api/push`), `OPOCHECKER_PUSH_TOKEN`, `OPOCHECKER_EXTERNO`,
`OPOCHECKER_DATA` y `OPOCHECKER_CONT`.

Notas:
- `OPOCHECKER_EXTERNO` es para un dead-man's switch externo (healthchecks.io y similares):
  kuma vive en el mismo servidor, asi que no puede avisarte si se cae el servidor entero.
- Si el contenedor esta `unhealthy` y el heartbeat lleva mas de 10 minutos parado, el script
  reinicia el contenedor (maximo una vez cada 15 minutos).
- Un boletin que devuelve error **no** baja el monitor: aparece como `errores_log=N` en el
  mensaje del heartbeat, para no inundar de falsos positivos.

## Ajustar las palabras clave

En `config.json`, `keywords` es una lista de grupos. Un anuncio se notifica si **todas** las
palabras de algun grupo aparecen en su titulo o enlace (sin acentos):

```json
"keywords": [
  ["facultativo", "especialista"],
  ["medico", "especialista"],
  ["f.e.a"]
]
```

Para afinar a tu especialidad, anade grupos mas concretos, por ejemplo:
`["facultativo", "anestesio"]`, `["especialista", "cardiologia"]`.

Estos grupos son la **base**: se aplican a todos los usuarios. Cada usuario anade encima lo
suyo (`/especialidades`, `/addkw`) y puede excluir la base con `/base off` para recibir solo
lo de sus especialidades.

## Solucion de problemas

- **"Telegram no configurado"**: falta token o chat_id (`.env`, variables de entorno o `config.json`).
- **"chat_id no valido"**: el chat_id se consigue con `getUpdates` despues de haber enviado
  al bot un mensaje; si tu cuenta usa nombre de usuario, el chat_id aparece como numero negativo.
- **El bot no contesta a los comandos**: comprueba que solo hay un proceso llamando a Telegram
  (`docker compose logs -f` en el servidor). Dos instancias (servidor + cron en Windows, por
  ejemplo) se roban los updates entre ellas.
- **Un boletin da error en `--verify`**: puede ser temporal (el boletin no publica ese dia o el
  servidor esta caido). Vuelve a ejecutar `--verify` mas tarde. El registro de cada fallo queda
  en `opochecker.log` (dentro de `data/`).
- **Quieres dejar de recibir avisos de algo ya notificado**: por chat, `/resetkw` y ajusta
  especialidades. A nivel global, edita `data/state.json` y ejecuta `--check`.

## Archivos

| Archivo | Contenido |
|---------|-----------|
| `opochecker.py` | El vigilante (script principal) |
| `setup_telegram.py` | Asistente que valida token/chat_id y los guarda |
| `Dockerfile` | Imagen del servicio (Python 3.13 + tzdata, sin dependencias pip) |
| `compose.yaml` | Stack de Docker Compose (servicio `opochecker`) |
| `.env.example` | Plantilla de `.env` con token y chat_id |
| `config.json` | Telegram, palabras clave y fuentes (**NO subir a GitHub**) |
| `config.json.example` | Copia sin token, para subir a GitHub |
| `arrancar_oculto.vbs` | Lanzador sin ventana (doble clic o Inicio de Windows) |
| `.github/workflows/opochecker.yml` | Diagnostico manual (`--verify`); el `schedule` esta desactivado |
| `monitor/opochecker-check.sh` | Vigilancia del servicio desde el host (uptime-kuma Push) |
| `data/state.json` | Memoria de documentos ya notificados, por chat (se genera solo) |
| `data/usuarios.json` | Especialidades, keywords y ajustes de cada usuario |
| `data/opochecker.log` | Registro de ejecuciones (rota a los 5 MB, 3 copias) |
| `data/heartbeat` | Marca de vida del servicio (la usa el `HEALTHCHECK`) |
