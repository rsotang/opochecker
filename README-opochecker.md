# Opochecker

Vigilante de los **boletines oficiales de las comunidades autonomas espanolas** en busca de
documentos sobre **oposiciones/concursos de facultativos especialistas** (medicos especialistas
del sistema sanitario publico). Cuando aparece algo nuevo, envia un aviso a **Telegram**.

Python puro (solo stdlib), sin dependencias que instalar. Funciona en Windows.

## Como funciona

- Cada ejecucion consulta 11 fuentes oficiales (boletines con RSS o paginas de sumario del dia).
- Extrae los anuncios, los filtra con las palabras clave de `config.json` (busca "facultativo
  especialista", "licenciado/a especialista", "medico especialista", "F.E.A.", etc., ignorando
  acentos y mayusculas).
- Compara con `state.json` para no repetir avisos (memoria de lo ya notificado).
- Cada documento nuevo se envia como mensaje de Telegram con titulo y enlace.
- Un log queda en `opochecker.log`.

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

Escribe estos comandos en el chat con tu bot (se procesan en la siguiente ejecucion
programada, como mucho 30 minutos despues):

| Comando | Que hace |
|---------|----------|
| `/especialidades` | **Selector de especialidades**: envia la lista de 34 especialidades medicas y facultativas con botones para activar/desactivar cada una. Al pulsar un boton se muestra la keyword asociada (cardiologia -> "cardiolog", pediatria -> "pediatr", anestesiologia -> "anestesio", etc.). Las especialidades activas se suman a la monitorizacion |
| `/keywords` | Muestra las keywords base, las de tus especialidades activas y las tuyas anadidas |
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

## Programar cada 30 minutos

Tienes dos opciones:

### Opcion A (recomendada): Programador de tareas de Windows

```bat
python opochecker.py --install-schedule
```

Crea la tarea "Opochecker" que ejecuta `--check` cada 30 minutos, incluso sin sesion abierta.
Para desinstalarla: `python opochecker.py --uninstall-schedule`.

### Opcion B: sin Programador de tareas (arranque oculto al iniciar sesion)

```bat
python opochecker.py --install-startup
```

Copia `arrancar_oculto.vbs` a la carpeta de Inicio de Windows: cada vez que inicies sesion
se lanza el vigilante en segundo plano, **sin ventana** (usa `pythonw`), comprobando cada
30 minutos. Para quitarlo: `python opochecker.py --uninstall-startup`.

Tambien puedes arrancarlo tu mismo cuando quieras:
- Con ventana visible: `python opochecker.py --loop --interval 30`
- Sin ventana: doble clic en `arrancar_oculto.vbs`

Nota: la Opcion B solo funciona mientras tu sesion de Windows esta iniciada. Si apagas el
equipo, se reanuda al iniciar sesion. La Opcion A es la que funciona con el equipo cerrado
sesion pero encendido.

### Opcion C: sin usar tu PC (GitHub Actions, gratis)

El vigilante puede correr en los servidores de GitHub cada 30 minutos, sin depender de
ningun equipo tuyo. Ya esta preparado el flujo en `.github/workflows/opochecker.yml`.

1. Crea un repositorio en GitHub (recomendado: **publico** — las Actions son ilimitadas
   en repos publicos; en privados hay 2.000 minutos/mes de cortesia).
2. Sube todos los archivos de esta carpeta **menos** `config.json` (contiene tu token; ya
   esta en `.gitignore` y se usa `config.json.example` en su lugar).
3. En el repositorio: **Settings -> Secrets and variables -> Actions -> New repository secret**:
   - `OPO_TELEGRAM_TOKEN` = tu token de Telegram
   - `OPO_TELEGRAM_CHAT_ID` = tu chat_id (numero)
4. Ejecuta la primera comprobacion manual: pestaña **Actions -> Opochecker -> Run workflow**.
   Comprueba que el aviso llega a Telegram.
5. Desde entonces el flujo se ejecuta solo cada 30 minutos. La memoria de avisos ya enviados
   (`state.json`) se guarda en el propio repositorio, asi que no se repiten.

Notas:
- El horario usa la zona `Europe/Madrid` (configurada en el flujo), asi que los boletines
  diarios se consultan en su fecha correcta.
- GitHub puede retrasar o saltarse la ejecucion programada si el repositorio lleva mucho
  tiempo inactivo. Si quieres garantia total, el "Run workflow" manual siempre funciona.
- El token y el chat_id viajan como secrets, nunca se suben al repositorio.

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

## Solucion de problemas

- **"Telegram no configurado"**: falta token o chat_id en `config.json`.
- **"chat_id no valido"**: el chat_id se consigue con `getUpdates` despues de haber enviado
  al bot un mensaje; si tu cuenta usa nombre de usuario, el chat_id aparece como numero negativo.
- **Un boletin da error en `--verify`**: puede ser temporal (el boletin no publica ese dia o el
  servidor esta caido). Vuelve a ejecutar `--verify` mas tarde. El registro de cada fallo queda
  en `opochecker.log`.
- **Quieres dejar de recibir avisos de algo ya notificado**: borra `state.json` y ejecuta
  `--check` (no volvera a avisar de lo ya visto; solo de lo nuevo).

## Archivos

| Archivo | Contenido |
|---------|-----------|
| `opochecker.py` | El vigilante (script principal) |
| `setup_telegram.py` | Asistente que valida token/chat_id y los guarda |
| `config.json` | Telegram, palabras clave y fuentes (**NO subir a GitHub**) |
| `config.json.example` | Copia sin token, para subir a GitHub |
| `arrancar_oculto.vbs` | Lanzador sin ventana (doble clic o Inicio de Windows) |
| `.github/workflows/opochecker.yml` | Ejecucion cada 30 min en GitHub Actions |
| `state.json` | Memoria de documentos ya notificados (se genera solo) |
| `opochecker.log` | Registro de ejecuciones |
