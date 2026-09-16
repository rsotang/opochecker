#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opochecker — vigila los boletines oficiales de las CC.AA. espanolas en busca
de convocatorias de facultativos especialistas y avisa por Telegram.

Uso:
  python opochecker.py --serve                Servicio: long polling de Telegram + checks
  python opochecker.py --verify               Probar fuentes sin notificar
  python opochecker.py --test                 Enviar mensaje de prueba a Telegram
  python opochecker.py --check                Ejecutar una comprobacion (avisa novedades)
  python opochecker.py --poll-once            Procesar los updates de Telegram una vez
  python opochecker.py --install-schedule     Crear tarea en el Programador (cada 30 min)
  python opochecker.py --uninstall-schedule   Eliminar la tarea programada
  python opochecker.py --loop [--interval N]  Bucle continuo de checks, sin atender Telegram

Solo un proceso puede llamar a getUpdates del mismo bot. En produccion ese proceso es
'--serve' (el contenedor); no se debe ejecutar --serve dos veces ni dejar un cron que
llame a getUpdates en paralelo.
"""

import argparse
import html
import json
import logging
import os
import re
import signal
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Directorio de datos (estado, usuarios, log). En Docker se monta un volumen en /data.
DATA_DIR = os.path.abspath(os.environ.get("OPO_DATA_DIR") or BASE_DIR)
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOG_PATH = os.path.join(DATA_DIR, "opochecker.log")
HEARTBEAT_PATH = os.path.join(DATA_DIR, "heartbeat")
USUARIOS_PATH = os.path.join(DATA_DIR, "usuarios.json")
TASK_NAME = "Opochecker"
STARTUP_VBS = "arrancar_oculto.vbs"
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", BASE_DIR),
                           "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _resolve(name: str, extra: str = None) -> str:
    """Primer fichero existente: ruta explicita, volumen de datos o junto al script."""
    for candidate in (extra, os.path.join(DATA_DIR, name), os.path.join(BASE_DIR, name)):
        if candidate and os.path.exists(candidate):
            return candidate
    return os.path.join(DATA_DIR, name)


CONFIG_PATH = _resolve("config.json", os.environ.get("OPO_CONFIG"))
ESPECIALIDADES_PATH = _resolve("especialidades.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()

log = logging.getLogger("opochecker")
STOP = False


# ---------------------------------------------------------------- utilidades

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def is_match(title: str, url: str, keyword_groups) -> bool:
    """True si title+url contiene alguna combinacion de keywords (todas las del grupo)."""
    blob = normalize(f"{title} {url}")
    return any(all(normalize(term) in blob for term in group) for group in keyword_groups)


def http_get(url: str, timeout: int = 25, tries: int = 3, accept: str = "*/*") -> str:
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                raw = r.read()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("latin-1", "replace")
        except Exception as e:
            last = e
            if attempt < tries - 1:
                time.sleep(3 * (attempt + 1))
    raise last


def abs_url(base: str, href: str) -> str:
    if not href:
        return ""
    href = html.unescape(href.strip())
    if href.startswith("//"):
        return "https:" + href
    return urllib.parse.urljoin(base, href)


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


class Announcement:
    __slots__ = ("title", "url", "source")

    def __init__(self, title, url, source=None):
        self.title = strip_tags(title)[:300]
        self.url = url
        self.source = source

    @property
    def key(self):
        return self.url or self.title


# ------------------------------------------------------- parsers por fuente

def parse_rss(base: str, body: str) -> list:
    out = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return out
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag in ("item", "entry"):
            title, link = "", ""
            for child in item.iter():
                ct = child.tag.rsplit("}", 1)[-1]
                if ct == "title" and not title:
                    title = (child.text or "").strip()
                if ct == "link" and not link:
                    link = (child.text or "").strip() or child.get("href", "")
            if title or link:
                out.append(Announcement(title, abs_url(base, link)))
    return out


def parse_bocm(body: str) -> list:
    """BOCM (Madrid): bloques views-row con <p>titulo</p> y enlace PDF CM_Orden_BOCM."""
    out = []
    seen = set()
    for block in re.split(r'<div class="views-row[^"]*">', body)[1:]:
        m = re.search(r"<p>(.*?)</p>", block, re.S)
        murl = re.search(r'href="([^"]*CM_Orden_BOCM[^"]*\.PDF)"', block, re.I)
        if not m or not murl:
            continue
        title = strip_tags(m.group(1))
        url = abs_url("https://www.bocm.es", murl.group(1))
        if not title or url in seen:
            continue
        seen.add(url)
        out.append(Announcement(title, url))
    return out


def parse_bocyl(body: str) -> list:
    """BOCyL (Castilla y Leon): <p>titulo</p> seguido de <ul class=descargaBoletin>."""
    out = []
    seen = set()
    for m in re.finditer(r"<a href='([^']*BOCYL-D-[^']*\.pdf)'[^>]*>", body, re.I):
        url = abs_url("https://bocyl.jcyl.es", m.group(1))
        before = body[max(0, m.start() - 1500): m.start()]
        pm = list(re.finditer(r"<p>(.*?)</p>", before, re.S))
        if not pm:
            continue
        title = strip_tags(pm[-1].group(1))
        if not title or url in seen:
            continue
        seen.add(url)
        out.append(Announcement(title, url))
    return out


def parse_canarias(body: str) -> list:
    """BOC Canarias: <li> <a href=pdf><b>num</b></a> <a href=pdf>titulo</a>.</li>"""
    out = []
    seen = set()
    for m in re.finditer(
            r'<a href="([^"]*boc-[ab]-\d{4}-\d+-\d+\.pdf)"[^>]*>\s*<b>\d+</b>\s*</a>\s*'
            r'<a href="\1"[^>]*>(.*?)</a>', body, re.I | re.S):
        url = m.group(1)
        title = strip_tags(m.group(2))
        if not title or url in seen:
            continue
        seen.add(url)
        out.append(Announcement(title, url))
    return out


def parse_doe(body: str) -> list:
    """DOE (Extremadura): <span DOE2>..</span><span DOE4>..</span> + <a enlace_dis href=pdf>."""
    out = []
    seen = set()
    for m in re.finditer(r'<a class="enlace_dis" href="([^"]*\.pdf)"', body, re.I):
        url = abs_url("https://doe.juntaex.es", m.group(1))
        before = body[max(0, m.start() - 2000): m.start()]
        p2 = re.findall(r'<span class="DOE2">((?:(?!class="DOE2").)*?)</span>\s*'
                        r'<span class="DOE4">(.*?)</span>', before, re.S)
        if not p2:
            continue
        title = strip_tags(" - ".join(p2[-1]))
        if not title or url in seen:
            continue
        seen.add(url)
        out.append(Announcement(title, url))
    return out


def parse_bopa(body: str) -> list:
    """BOPA (Asturias): <dt>titulo</dt> <dd>...enlace disposition...</dd>."""
    out = []
    seen = set()
    for m in re.finditer(r"<dt>(.*?)</dt>\s*<dd>.*?href=\"([^\"]*disposition[^\"]*)\"",
                         body, re.I | re.S):
        title = re.sub(r"\[Cód\..*?\]", "", strip_tags(m.group(1)), flags=re.I | re.S)
        title = re.sub(r"\s+", " ", title).strip()
        url = html.unescape(abs_url("https://miprincipado.asturias.es", m.group(2)))
        if not title or url in seen:
            continue
        seen.add(url)
        out.append(Announcement(title, url))
    return out


def parse_borm_sumario(body: str) -> list:
    """BORM (Murcia): API JSON del sumario de un dia."""
    out = []
    try:
        data = json.loads(body)
    except ValueError:
        return out
    fecha = data.get("fechaPublicacion", "")
    for an in data.get("anunciosBoletin", []):
        title = an.get("sumario", "").strip()
        numero = an.get("numero")
        if not title:
            continue
        url = f"https://www.borm.es/#/anuncio/{fecha}/{numero}"
        out.append(Announcement(title, url))
    return out


def fetch_boib(issue_url: str) -> list:
    """BOIB (Baleares): pagina del numero -> secciones II (personal) y V (anuncios)."""
    page = http_get(issue_url)
    sections = re.findall(
        r'href="(/eboibfront/[a-z]{2}/\d{4}/\d+/seccio-(?:ii-autoritats-i-personal|v-anuncis)/\d+)"',
        page)
    out = []
    seen = set()
    for s in sections:
        sec = http_get(abs_url("https://www.caib.es", s))
        for m in re.finditer(r"<li>\s*<p>(.*?)</p>\s*<p class=\"registre\">(.*?)</p>(.*?)</ul>",
                             sec, re.S | re.I):
            title = strip_tags(m.group(1))
            tail = m.group(3)
            murl = re.search(r'(?:class="html"[^>]*href="([^"]+)"|href="([^"]+)"[^>]*class="html")',
                             tail, re.I)
            if not murl or not title:
                continue
            url = html.unescape(murl.group(1) or murl.group(2))
            if url in seen:
                continue
            seen.add(url)
            out.append(Announcement(title, url))
    return out


def extract_links(base: str, body: str, link_re: str, max_links: int = 400) -> list:
    """Extrae anuncios de un HTML: enlaces cuyo href casa con link_re, con su texto."""
    out = []
    seen = set()
    for href, txt in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, re.S | re.I):
        url = abs_url(base, href)
        title = strip_tags(txt)
        if link_re and not re.search(link_re, url, re.I):
            continue
        if not title:
            continue
        key = url or title
        if key in seen:
            continue
        seen.add(key)
        out.append(Announcement(title, url))
        if len(out) >= max_links:
            break
    return out


# ------------------------------------------------------------- config

def load_config() -> dict:
    path = CONFIG_PATH
    if not os.path.exists(path):
        path = os.path.join(BASE_DIR, "config.json.example")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    token = cfg.get("telegram", {}).get("bot_token") or os.environ.get("OPO_TELEGRAM_TOKEN", "")
    chat = cfg.get("telegram", {}).get("chat_id") or os.environ.get("OPO_TELEGRAM_CHAT_ID", "")
    cfg["_token"] = token
    cfg["_chat"] = str(chat)
    cfg.setdefault("access", {"allowed_chats": []})
    return cfg


def new_state() -> dict:
    return {"seen": {}, "telegram_offset": 0, "last_check": ""}


def migrate_state(state: dict, owner_chat=None) -> dict:
    """Normaliza el formato antiguo a memoria por chat.

    Antes: {"seen": {"<url>": "<fecha>"}} (un solo destinatario).
    Ahora: {"seen": {"<chat_id>": {"<url>": "<fecha>"}}, "telegram_offset": N, ...}
    """
    seen = state.get("seen")
    if not isinstance(seen, dict):
        seen = {}
    if seen and not any(isinstance(v, dict) for v in seen.values()):
        seen = {str(owner_chat if owner_chat is not None else "shared"): seen}
    state["seen"] = seen
    state.setdefault("telegram_offset", 0)
    state.setdefault("last_check", "")
    return state


def load_state(owner_chat=None) -> dict:
    state = new_state()
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state.update(loaded)
        except (OSError, ValueError):
            pass
    return migrate_state(state, owner_chat)


def save_state(state: dict):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def load_especialidades() -> dict:
    if os.path.exists(ESPECIALIDADES_PATH):
        try:
            with open(ESPECIALIDADES_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def load_usuarios() -> dict:
    if os.path.exists(USUARIOS_PATH):
        try:
            with open(USUARIOS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def save_usuarios(data: dict):
    tmp = USUARIOS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USUARIOS_PATH)


def user_settings(data: dict, chat_id) -> dict:
    return data.setdefault(str(chat_id), {"especialidades": [], "extra_keywords": []})


def register_user(chat_id) -> bool:
    """Da de alta el chat si no estaba. Devuelve True si es nuevo."""
    data = load_usuarios()
    if str(chat_id) in data:
        return False
    user_settings(data, chat_id)
    save_usuarios(data)
    log.info("Usuario registrado: %s", chat_id)
    return True


def all_user_chats(cfg: dict) -> list:
    """Chats a los que notificar: el propietario (config/env) y todos los registrados."""
    owner = str(cfg.get("_chat") or "")
    chats = [owner] if owner else []
    for cid in load_usuarios():
        if str(cid) not in chats:
            chats.append(str(cid))
    return chats


def is_allowed(cfg: dict, chat_id) -> bool:
    """Si config.access.allowed_chats tiene elementos, solo esos chats (y el
    propietario) pueden usar el bot. Vacio = abierto."""
    allowed = [str(c) for c in (cfg.get("access", {}) or {}).get("allowed_chats", []) or []]
    if not allowed:
        return True
    return str(chat_id) in allowed or str(chat_id) == str(cfg.get("_chat") or "")


def effective_keywords(cfg: dict, chat_id) -> list:
    """Keywords del chat: base (config) + especialidades seleccionadas + extras del usuario.

    Las keywords base son las generales del vigilante (facultativo + especialista, etc.).
    Un usuario puede desactivarlas con /base off para recibir solo lo suyo; si lo hace sin
    tener especialidades ni extras se mantienen, para no dejarlo sin ningun aviso.
    """
    data = load_usuarios()
    us = user_settings(data, chat_id)
    propias = us.get("especialidades") or us.get("extra_keywords")
    kws = []
    if us.get("usar_base", True) or not propias:
        kws = [list(g) for g in cfg.get("keywords", [["facultativo", "especialista"]])]
    esp = load_especialidades()
    for sid in us.get("especialidades", []):
        for g in esp.get(sid, {}).get("keywords", []):
            kws.append(list(g))
    kws.extend(list(g) for g in us.get("extra_keywords", []))
    return kws


# ----------------------------------------------------------- telegram

def telegram_api(method: str, token: str, params: dict = None, timeout: int = 30) -> tuple:
    """Llama a la API de Telegram. Devuelve (json|None, error_descripcion|None)."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
            desc = body.get("description", "") or str(e.reason)
        except Exception:
            desc = str(e)
        return None, f"HTTP {e.code}: {desc}"
    except Exception as e:
        return None, str(e)


def telegram_send(token: str, chat: str, text: str, reply_markup: str = None) -> bool:
    if not token or not chat:
        log.error("Telegram no configurado: falta bot_token/chat_id en config.json (o env OPO_TELEGRAM_*)")
        return False
    params = {"chat_id": chat, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"}
    if reply_markup:
        params["reply_markup"] = reply_markup
    ok, err = telegram_api("sendMessage", token, params)
    if not ok:
        log.error("Telegram (sendMessage): %s", err)
        return False
    if not ok.get("ok"):
        log.error("Telegram respondio ok=false")
        return False
    return True


def telegram_edit_keyboard(token: str, chat: str, msg_id, reply_markup: str) -> bool:
    ok, err = telegram_api("editMessageReplyMarkup", token, {
        "chat_id": chat, "message_id": msg_id, "reply_markup": reply_markup})
    if not ok or not ok.get("ok"):
        log.error("Telegram (editMessageReplyMarkup): %s", err or "fallo")
        return False
    return True


def telegram_answer(token: str, cb_id: str, text: str = "") -> bool:
    ok, err = telegram_api("answerCallbackQuery", token, {
        "callback_query_id": cb_id, "text": text, "show_alert": "false"})
    return bool(ok and ok.get("ok"))


def telegram_test(token: str, chat: str) -> str:
    """Diagnostico de la configuracion de Telegram. Devuelve mensaje legible."""
    if not token:
        return "Falta el bot_token: edita config.json o ejecuta setup_telegram.py"
    me, err = telegram_api("getMe", token)
    if not me:
        return f"El bot_token parece incorrecto ({err}). Comprueba que copiaste el token completo de @BotFather."
    username = me.get("result", {}).get("username", "?")
    if not chat:
        return f"El token es valido (bot @{username}) pero falta el chat_id: edita config.json o ejecuta setup_telegram.py"
    ok, err = telegram_api("sendMessage", token, {"chat_id": chat, "text": "<b>Opochecker</b> - prueba de notificacion OK",
                                                  "parse_mode": "HTML"})
    if ok and ok.get("ok"):
        return f"Todo correcto: mensaje de prueba enviado al chat {chat} desde @{username}."
    return (f"El token es valido (bot @{username}) pero el chat_id {chat} parece incorrecto "
            f"({err}). Habla con tu bot y envia /start, y usa el numero que aparece en getUpdates.")


def esc(s: str) -> str:
    return html.escape(s, quote=False)


# --------------------------------------------------- especialidades y keywords

def keyword_text(g: list) -> str:
    return " + ".join(g)


def catalog_text(esp: dict, sel: list) -> str:
    lines = ["<b>Especialidades disponibles</b> (toca los botones para activar/desactivar):\n"]
    for sid in esp:
        marca = "[x]" if sid in sel else "[ ]"
        lines.append(f"{marca} {esc(esp[sid]['nombre'])}")
    lines.append("\nCada especialidad anade sus keywords a la monitorizacion. "
                 "Pulsa los botones de abajo y luego /misespecialidades.")
    return "\n".join(lines)


def specialty_keyboard(esp: dict, sel: list) -> str:
    rows = []
    for sid, info in esp.items():
        marca = "x" if sid in sel else " "
        rows.append([{"text": f"[{marca}] {info['nombre']}", "callback_data": f"esp:{sid}"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def keywords_report(cfg: dict, chat_id) -> str:
    esp = load_especialidades()
    us = user_settings(load_usuarios(), chat_id)
    usar_base = us.get("usar_base", True)
    lines = ["<b>Tus keywords</b> (un anuncio se avisa si cumple todas las de un grupo):\n"]
    lines.append("<b>Base:</b>" + ("" if usar_base else "  DESACTIVADAS (/base on para volver)"))
    if usar_base:
        for i, g in enumerate(cfg.get("keywords", []), 1):
            lines.append(f"  {i}. {keyword_text(g)}")
    sel = us.get("especialidades", [])
    if sel:
        lines.append("\n<b>Especialidades activas:</b>")
        for sid in sel:
            info = esp.get(sid, {})
            kws = " | ".join(keyword_text(g) for g in info.get("keywords", []))
            lines.append(f"  - {info.get('nombre', sid)}: {kws}")
    else:
        lines.append("\n<b>Especialidades activas:</b> ninguna")
    extra = us.get("extra_keywords", [])
    if extra:
        lines.append("\n<b>Extras tuyos:</b>")
        for i, g in enumerate(extra, 1):
            lines.append(f"  {i}. {keyword_text(g)}  (/delkw {i})")
    lines.append("\nEditar extras: /addkw termino1 termino2 ...  |  /resetkw")
    lines.append("Keywords base: /base on | /base off")
    return "\n".join(lines)


def set_base_keywords(chat_id, arg: str) -> str:
    """Activa o desactiva las keywords base (generales) para este chat."""
    data = load_usuarios()
    us = user_settings(data, chat_id)
    arg = (arg or "").strip().lower()
    if arg in ("on", "si", "1"):
        us["usar_base"] = True
    elif arg in ("off", "no", "0"):
        if not (us.get("especialidades") or us.get("extra_keywords")):
            return ("No puedes desactivar las keywords base sin tener antes alguna especialidad "
                    "o keyword propia: te quedarias sin ningun aviso. Usa /especialidades o /addkw.")
        us["usar_base"] = False
    else:
        estado = "activadas" if us.get("usar_base", True) else "desactivadas"
        return (f"Keywords base: <b>{estado}</b>.\n"
                f"Uso: /base on | /base off\n"
                f"Con las base desactivadas solo recibiras las de tus especialidades y tus extras.")
    save_usuarios(data)
    return f"Keywords base <b>{'activadas' if us['usar_base'] else 'desactivadas'}</b>."


def set_specialty(chat_id, sid: str, active: bool) -> str:
    esp = load_especialidades()
    data = load_usuarios()
    us = user_settings(data, chat_id)
    sel = us.setdefault("especialidades", [])
    if active and sid not in sel:
        sel.append(sid)
    if not active and sid in sel:
        sel.remove(sid)
    save_usuarios(data)
    info = esp.get(sid, {})
    kws = " | ".join(keyword_text(g) for g in info.get("keywords", []))
    estado = "activada" if active else "desactivada"
    return f"{info.get('nombre', sid)} {estado}. Keywords: {kws}"


def add_extra_keywords(chat_id, terms: list) -> str:
    if not terms:
        return "Uso: /addkw termino1 termino2 ... (el anuncio debe contener TODOS los terminos)"
    data = load_usuarios()
    us = user_settings(data, chat_id)
    extra = us.setdefault("extra_keywords", [])
    if terms in extra:
        return f"Esa keyword ya existe: {keyword_text(terms)}"
    extra.append(terms)
    save_usuarios(data)
    return f"Keyword anadida: {keyword_text(terms)}"


def del_extra_keyword(chat_id, idx: int) -> str:
    data = load_usuarios()
    us = user_settings(data, chat_id)
    extra = us.get("extra_keywords", [])
    if not 1 <= idx <= len(extra):
        return f"No hay keyword extra numero {idx}. Usa /keywords para ver la lista."
    g = extra.pop(idx - 1)
    save_usuarios(data)
    return f"Keyword extra eliminada: {keyword_text(g)}"


def reset_extra_keywords(chat_id) -> str:
    data = load_usuarios()
    us = user_settings(data, chat_id)
    n = len(us.get("extra_keywords", []))
    us["extra_keywords"] = []
    save_usuarios(data)
    return f"Keywords extras eliminadas ({n})."


def handle_update(upd: dict, cfg: dict, token: str) -> bool:
    """Procesa un update del bot. Devuelve True si era un comando/callback atendido."""
    cb = upd.get("callback_query")
    if cb:
        data = (cb.get("data") or "").strip()
        chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
        msg_id = (cb.get("message") or {}).get("message_id")
        cb_id = cb.get("id", "")
        if chat_id is None:
            return False
        if not is_allowed(cfg, chat_id):
            log.info("Callback ignorado de chat no autorizado: %s", chat_id)
            telegram_answer(token, cb_id, "No autorizado")
            return True
        register_user(chat_id)
        if data.startswith("esp:"):
            sid = data[4:]
            active = sid not in user_settings(load_usuarios(), chat_id)["especialidades"]
            texto = set_specialty(chat_id, sid, active)
            telegram_answer(token, cb_id, texto)
            if msg_id:
                sel = user_settings(load_usuarios(), chat_id)["especialidades"]
                telegram_edit_keyboard(token, chat_id, msg_id,
                                       specialty_keyboard(load_especialidades(), sel))
            return True
        telegram_answer(token, cb_id, "Accion no reconocida")
        return True

    msg = upd.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = (msg.get("chat") or {}).get("id")
    if not text or chat_id is None:
        return False
    if not is_allowed(cfg, chat_id):
        log.info("Mensaje ignorado de chat no autorizado: %s", chat_id)
        return True
    register_user(chat_id)
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower()
    log.info("Comando recibido: %s (chat %s)", cmd, chat_id)

    if cmd == "/backtrack":
        try:
            days = min(max(int(arg.strip()), 1), 60)
        except ValueError:
            days = 30
        telegram_send(token, chat_id,
                      f"Revisando los ultimos <b>{days} dias</b> de boletines "
                      f"(tarda unos minutos)...")
        items = run_backtrack(cfg, days, chat_id=chat_id)
        log.info("/backtrack: %d coincidencias enviadas", len(items))
        return True

    if cmd == "/especialidades":
        esp = load_especialidades()
        sel = user_settings(load_usuarios(), chat_id)["especialidades"]
        telegram_send(token, chat_id, catalog_text(esp, sel),
                      specialty_keyboard(esp, sel))
        return True

    if cmd == "/misespecialidades":
        telegram_send(token, chat_id, keywords_report(cfg, chat_id))
        return True

    if cmd == "/keywords":
        telegram_send(token, chat_id, keywords_report(cfg, chat_id))
        return True

    if cmd == "/addkw":
        terms = [t for t in re.split(r"[\s,;]+", arg.strip().lower()) if t]
        telegram_send(token, chat_id, add_extra_keywords(chat_id, terms))
        return True

    if cmd == "/delkw":
        try:
            idx = int(arg.strip())
        except ValueError:
            telegram_send(token, chat_id, "Uso: /delkw numero  (ver /keywords)")
            return True
        telegram_send(token, chat_id, del_extra_keyword(chat_id, idx))
        return True

    if cmd == "/resetkw":
        telegram_send(token, chat_id, reset_extra_keywords(chat_id))
        return True

    if cmd == "/base":
        telegram_send(token, chat_id, set_base_keywords(chat_id, arg))
        return True

    if cmd == "/status":
        n = len([s for s in cfg.get("sources", []) if s.get("enabled", True)])
        telegram_send(token, chat_id,
                      f"<b>Opochecker</b> activo.\n"
                      f"Fuentes activas: {n}\n"
                      f"Usuarios suscritos: {len(all_user_chats(cfg))}\n"
                      f"Comandos: /backtrack [dias], /especialidades, /keywords, /base, "
                      f"/status, /ayuda")
        return True

    if cmd in ("/ayuda", "/help", "/start") or not cmd.startswith("/"):
        telegram_send(token, chat_id, WELCOME_TEXT)
        return True

    telegram_send(token, chat_id,
                  f"Comando {esc(cmd)} no reconocido. Envia /ayuda para ver los comandos.")
    return True


def poll_telegram(cfg: dict, timeout: int = 25) -> int:
    """Una vuelta de getUpdates (long polling si timeout > 0).

    El offset se confirma y se guarda en disco ANTES de ejecutar cada comando: asi un
    reinicio a mitad de un comando largo (/backtrack) no lo reproduce al arrancar.
    Devuelve el numero de updates atendidos.
    """
    token = cfg["_token"]
    if not token:
        return 0
    state = load_state(cfg.get("_chat"))
    offset = int(state.get("telegram_offset") or 0)
    ok, err = telegram_api("getUpdates", token, {
        "offset": offset,
        "timeout": timeout,
        "limit": 20,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }, timeout=timeout + 15)
    if not ok:
        log.warning("getUpdates: %s", err)
        return 0

    atendidos = 0
    for upd in ok.get("result") or []:
        state["telegram_offset"] = int(upd.get("update_id", 0)) + 1
        save_state(state)
        try:
            if handle_update(upd, cfg, token):
                atendidos += 1
        except Exception as e:
            log.error("Error procesando update: %s", e)
    return atendidos


def touch_heartbeat():
    """Marca de vida para el HEALTHCHECK del contenedor."""
    try:
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat(timespec="seconds"))
    except OSError as e:
        log.warning("No se pudo escribir el heartbeat: %s", e)


def seconds_until_check(state: dict, interval: int) -> float:
    """Segundos que faltan para la siguiente comprobacion (0 si ya toca)."""
    last = state.get("last_check") or ""
    if not last:
        return 0.0
    try:
        transcurrido = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
    except ValueError:
        return 0.0
    return max(0.0, interval * 60 - transcurrido)


def serve(cfg: dict, interval: int, poll_timeout: int = 25):
    """Servicio: long polling continuo de Telegram + comprobacion cada `interval` min."""
    interval = max(1, int(interval))  # con 0 cada vuelta del bucle martillearia las fuentes
    poll_timeout = max(0, min(int(poll_timeout), 50))
    owner = cfg.get("_chat")
    if owner:
        register_user(owner)
    log.info("Servicio iniciado: checks cada %d min, long polling de %ds, datos en %s",
             interval, poll_timeout, DATA_DIR)
    proximo = time.time() + seconds_until_check(load_state(owner), interval)
    while not STOP:
        touch_heartbeat()
        try:
            poll_telegram(cfg, poll_timeout)
        except Exception as e:
            log.exception("Error en el long polling: %s", e)
            time.sleep(5)
        if time.time() >= proximo:
            try:
                run_check(cfg)
            except Exception as e:
                log.exception("Error en la comprobacion: %s", e)
            state = load_state(owner)
            state["last_check"] = datetime.now().isoformat(timespec="seconds")
            save_state(state)
            proximo = time.time() + interval * 60
    log.info("Servicio detenido")


WELCOME_TEXT = (
    "<b>Opochecker</b> - vigilante de oposiciones de facultativos especialistas\n\n"
    "Vigilo los boletines oficiales de las comunidades autonomas y te aviso aqui cuando "
    "publican algo sobre <b>oposiciones y concursos de facultativos especialistas</b> "
    "(medicos especialistas del sistema publico).\n\n"
    "<b>Comandos:</b>\n"
    "/especialidades - Elige que especialidades vigilar (medicina, cardiologia, "
    "pediatria...): toca los botones para activarlas y veras sus keywords\n"
    "/keywords - Muestra tus keywords y las de tus especialidades\n"
    "/base on|off - Activa o desactiva las keywords generales (facultativo + especialista); "
    "con 'off' recibes solo lo de tus especialidades\n"
    "/addkw termino1 termino2 - Anade tu propia keyword (grupo: deben cumplirse TODOS "
    "los terminos)\n"
    "/delkw numero - Elimina una keyword anadida por ti\n"
    "/resetkw - Borra todas tus keywords anadidas\n"
    "/backtrack [dias] - Revisa los ultimos dias de boletines por anuncios que se hayan "
    "escapado y te los envia\n"
    "/misespecialidades - Resumen de tus especialidades y keywords\n"
    "/status - Estado del vigilante\n"
    "/ayuda - Esta ayuda\n\n"
    "<b>Funciones:</b>\n"
    "- Aviso automatico de cada documento nuevo que coincida con tus keywords\n"
    "- Sin mensajes duplicados: cada documento se avisa una sola vez\n"
    "- Los avisos incluyen el titulo del documento y el enlace para verlo\n\n"
    "Empieza con /especialidades para configurar lo que te interesa."
)


# ------------------------------------------------------------- chequeo

PARSERS = {
    "bocm": parse_bocm,
    "bocyl": parse_bocyl,
    "canarias": parse_canarias,
    "doe": parse_doe,
    "bopa": parse_bopa,
    "borm": parse_borm_sumario,
}


def fetch_source(src: dict) -> list:
    stype = src.get("type")
    url_tpl = src.get("url", "")
    link_re = src.get("link_re")
    out = []

    url = url_tpl.format(fecha=date.today().strftime("%d/%m/%Y"),
                         fecha_diaria=date.today().strftime("%Y%m%d"))

    if stype == "rss":
        body = http_get(url)
        out = parse_rss(url, body)
    elif stype == "page":
        body = http_get(url)
        for href, txt in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, re.S | re.I):
            u = abs_url(url, href)
            title = strip_tags(txt)
            if link_re and not re.search(link_re, u, re.I):
                continue
            if not title:
                continue
            out.append(Announcement(title, u))
    elif stype == "bocm":
        toc = http_get(url)
        for href in re.findall(r'href="(/boletin-completo/bocm-\d{8}/\d+/[^"]*)"', toc):
            if not re.search(r"b%29-autoridades-y-personal|d%29-anuncios", href):
                continue
            body = http_get(abs_url("https://www.bocm.es", href))
            out.extend(parse_bocm(body))
    elif stype == "canarias":
        home = http_get(url)
        m = re.search(r'href="(/boc/\d{4}/\d+)"[^>]*>\s*BOC\s*N[º\u00ba]?', home, re.I | re.S)
        if not m:
            raise RuntimeError("Canarias: no se localizo el boletin actual en la portada")
        body = http_get(abs_url("https://www.gobiernodecanarias.org", m.group(1)))
        out = parse_canarias(body)
    elif stype == "borm":
        meta = json.loads(http_get(url, accept="application/json"))
        fecha = meta.get("fechaPublicacion")
        if not fecha:
            raise RuntimeError("BORM: sin fechaPublicacion")
        sumario = http_get(f"https://www.borm.es/services/boletin/fecha/{fecha}/sumario",
                           accept="application/json")
        out = parse_borm_sumario(sumario)
    elif stype == "boib":
        rss = http_get(url)
        items = parse_rss(url, rss)
        if not items:
            raise RuntimeError("BOIB: RSS sin numeros")
        out = fetch_boib(items[0].url)
    elif stype in PARSERS:
        body = http_get(url)
        out = PARSERS[stype](body)
    else:
        raise ValueError(f"Tipo de fuente desconocido: {stype}")

    for a in out:
        a.source = src
    return out


def collect_announcements(cfg: dict, verbose: bool = False) -> tuple:
    """Descarga todas las fuentes activas. Devuelve (anuncios, fuentes_ok, fuentes_total).

    No filtra por keywords: las fuentes se descargan una sola vez y el filtrado se hace
    despues por usuario (cada uno tiene sus especialidades y extras).
    """
    items, ok, total = [], 0, 0
    for src in cfg.get("sources", []):
        if not src.get("enabled", True):
            continue
        total += 1
        name = f"{src.get('ccaa')} ({src.get('boletin')})"
        try:
            anns = fetch_source(src)
            ok += 1
            log.info("%s: %d anuncios", name, len(anns))
            items.extend(a for a in anns if a.title)
        except Exception as e:
            log.error("%s: ERROR -> %s", name, e)
            if verbose:
                print(f"  [ERROR] {name}: {e}")
    return items, ok, total


def run_check(cfg: dict, verbose: bool = False) -> int:
    """Comprueba las fuentes y avisa a cada usuario de las novedades que cumplan SUS keywords."""
    state = load_state(cfg.get("_chat"))
    seen = state["seen"]
    items, ok_src, total_src = collect_announcements(cfg, verbose)
    chats = all_user_chats(cfg)

    if verbose:
        print(f"\nAnuncios recogidos: {len(items)}  (fuentes ok: {ok_src}/{total_src})")
        print(f"Usuarios a notificar: {len(chats)}")

    novedades = 0
    for chat in chats:
        kws = effective_keywords(cfg, chat)
        pend = [a for a in items
                if is_match(a.title, a.url, kws) and a.key not in seen.get(chat, {})]
        if not pend:
            continue
        log.info("Chat %s: %d novedades", chat, len(pend))
        if verbose:
            for a in pend[:50]:
                print(f"  - [{a.source.get('ccaa')}][chat {chat}] {a.title[:90]}\n    {a.url}")
        for a in pend:
            msg = (f"<b>{esc(a.source.get('ccaa'))}</b> - {esc(a.source.get('boletin'))}\n"
                   f"{esc(a.title)}\n<a href=\"{html.escape(a.url, quote=True)}\">ver documento</a>")
            if telegram_send(cfg["_token"], chat, msg):
                seen.setdefault(chat, {})[a.key] = datetime.now().isoformat(timespec="seconds")
                save_state(state)  # incremental: un corte a media tanda no reenvia avisos
                novedades += 1

    if verbose:
        print(f"Novedades notificadas: {novedades}")
    if total_src and not ok_src:
        log.error("Ninguna de las %d fuentes activas respondio", total_src)
        return 1
    return 0


# ------------------------------------------------------------ retroceso

MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
         "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
         "diciembre": 12}


def fmt_bt(url_tpl: str, day: date) -> str:
    return url_tpl.format(fecha=day.strftime("%d/%m/%Y"),
                          fecha_diaria=day.strftime("%Y%m%d"),
                          fecha_borm=day.strftime("%d-%m-%Y"),
                          fecha_url=urllib.parse.quote(day.strftime("%d/%m/%Y")))


def bt_fetch_day(src: dict, day: date) -> list:
    """Fuentes con URL por dia (bocyl, doe, bopa, borm...)."""
    items = PARSERS[src["type"]](http_get(fmt_bt(src["url"], day),
                                          accept=src.get("accept", "*/*")))
    for extra in src.get("extra_urls", []):
        items.extend(PARSERS[src["type"]](http_get(fmt_bt(extra, day),
                                                   accept=src.get("accept", "*/*"))))
    return items


def bt_walk_bon(limit: date) -> list:
    """Navarra: indice de boletines (numero | fecha) -> sumario con anuncios."""
    body = http_get("https://bon.navarra.es/es/indice-boletines")
    out = []
    for href, dia, mes, ano in re.findall(
            r'<a\s+href="(https://bon\.navarra\.es/es/boletin/-/sumario/\d+/\d+)"'
            r'[^>]*?title="N[º\u00ba]?\s*\d+\s*\|\s*(\d{1,2}) de (\w+) de (\d{4})"', body):
        d = date(int(ano), MESES[mes.lower()], int(dia))
        if d < limit:
            continue
        page = http_get(href)
        for a in extract_links(href, page, r"bon\.navarra\.es/es/anuncio/"):
            out.append(a)
    return out


def bt_walk_canarias(limit: date) -> list:
    """Canarias: archivo anual (numero | fecha ISO en title) -> boletin del numero."""
    out = []
    href_re = (r'<a\s+href="(/boc/\d{4}/\d+/index\.html)"[^>]*?title="[^"]*'
               r'\((\d{4}-\d{2}-\d{2})\)"')
    for year in range(limit.year, date.today().year + 1):
        body = http_get(f"https://www.gobiernodecanarias.org/boc/archivo/{year}/")
        for href, iso in re.findall(href_re, body):
            d = datetime.strptime(iso, "%Y-%m-%d").date()
            if d < limit:
                continue
            page = http_get(abs_url("https://www.gobiernodecanarias.org", href))
            out.extend(parse_canarias(page))
    return out


def bt_collect(cfg: dict, days: int, chat_id=None) -> list:
    """Recoge de las fuentes con historico los anuncios de los ultimos `days` dias."""
    chat_id = chat_id or cfg.get("_chat")
    keywords = effective_keywords(cfg, chat_id)
    seen = load_state(cfg.get("_chat"))["seen"].get(str(chat_id), {})
    found = []

    for src in cfg.get("backtrack", []):
        name = f"{src.get('ccaa')} ({src.get('boletin')})"
        dmax = min(src.get("days", days), days)
        try:
            anns = []
            stype = src.get("type")
            if stype == "bon_walk":
                anns = bt_walk_bon(date.today() - timedelta(days=dmax))
            elif stype == "canarias_walk":
                anns = bt_walk_canarias(date.today() - timedelta(days=dmax))
            else:
                day = date.today() - timedelta(days=dmax)
                while day <= date.today():
                    try:
                        anns.extend(bt_fetch_day(src, day))
                    except Exception:
                        pass  # dias sin boletin (fin de semana, festivo)
                    day += timedelta(days=1)
            for a in anns:
                if not a.title:
                    continue
                if not is_match(a.title, a.url, keywords):
                    continue
                if a.key in seen:
                    continue
                a.source = src
                found.append(a)
            log.info("Retroceso %s: %d anuncios, %d coinciden", name, len(anns),
                     sum(1 for a in anns if is_match(a.title, a.url, keywords)))
        except Exception as e:
            log.error("Retroceso %s: ERROR -> %s", name, e)
    return found


def bt_send(cfg: dict, items: list, days: int, chat_id=None) -> int:
    """Envia los resultados del retroceso agrupados por CCAA y los marca como vistos."""
    state = load_state(cfg.get("_chat"))
    chat = str(chat_id or cfg.get("_chat") or "")
    seen = state["seen"].setdefault(chat, {})
    token = cfg["_token"]
    by_src = {}
    for a in items:
        by_src.setdefault((a.source.get("ccaa"), a.source.get("boletin")), []).append(a)

    sent = 0
    for (ccaa, boletin), anns in by_src.items():
        chunks = [anns[i:i + 12] for i in range(0, len(anns), 12)]
        for ci, chunk in enumerate(chunks, 1):
            lines = [f"<b>Retroceso {days} dias: {esc(ccaa)} ({esc(boletin)})</b>"]
            if len(chunks) > 1:
                lines[0] += f"  [{ci}/{len(chunks)}]"
            for i, a in enumerate(chunk, 1):
                lines.append(f"{i}. {esc(a.title)}\n   <a href=\"{html.escape(a.url, quote=True)}\">ver documento</a>")
            if telegram_send(token, chat, "\n".join(lines)):
                for a in chunk:
                    seen[a.key] = datetime.now().isoformat(timespec="seconds")
                sent += len(chunk)
    if not items:
        telegram_send(token, chat,
                      f"<b>Retroceso {days} dias</b>: no se encontraron anuncios de facultativos "
                      f"especialistas en los boletines con historico disponible.")
    save_state(state)
    return sent


def run_backtrack(cfg: dict, days: int = 30, chat_id=None, dry_run: bool = False) -> list:
    items = bt_collect(cfg, days, chat_id)
    if dry_run:
        return items
    bt_send(cfg, items, days, chat_id)
    return items


def _on_stop(signum, frame):
    global STOP
    STOP = True
    log.info("Senal %s recibida: parando el servicio", signum)


# ----------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="Vigilante de oposiciones de facultativos especialistas")
    ap.add_argument("--serve", action="store_true",
                    help="servicio: long polling de Telegram + comprobacion periodica")
    ap.add_argument("--verify", action="store_true", help="probar fuentes sin enviar Telegram")
    ap.add_argument("--test", action="store_true", help="enviar mensaje de prueba a Telegram")
    ap.add_argument("--check", action="store_true", help="ejecutar comprobacion y notificar novedades")
    ap.add_argument("--poll-once", action="store_true",
                    help="procesar los updates pendientes de Telegram y salir")
    ap.add_argument("--backtrack", action="store_true",
                    help="revisar los ultimos N dias de boletines por anuncios no notificados")
    ap.add_argument("--days", type=int, default=30, help="dias a revisar en --backtrack (defecto 30)")
    ap.add_argument("--dry-run", action="store_true",
                    help="con --backtrack: mostrar resultados sin enviarlos por Telegram")
    ap.add_argument("--loop", action="store_true", help="bucle continuo de checks, sin atender Telegram")
    ap.add_argument("--interval", type=int, default=10,
                    help="minutos entre comprobaciones (defecto 10)")
    ap.add_argument("--install-schedule", action="store_true", help="crear tarea cada 30 min (Programador)")
    ap.add_argument("--uninstall-schedule", action="store_true", help="eliminar la tarea del Programador")
    ap.add_argument("--install-startup", action="store_true",
                    help="arranque oculto al iniciar sesion (sin Programador)")
    ap.add_argument("--uninstall-startup", action="store_true", help="quitar el arranque al iniciar sesion")
    args = ap.parse_args()

    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3,
                                      encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )

    if args.install_schedule:
        py = sys.executable
        cmd = f'"{py}" "{os.path.abspath(__file__)}" --check'
        r = subprocess.run(
            ["schtasks", "/create", "/tn", TASK_NAME, "/tr", cmd,
             "/sc", "minute", "/mo", "30", "/f"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        sys.exit(0 if r.returncode == 0 else 1)

    if args.uninstall_schedule:
        r = subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
        sys.exit(0 if r.returncode == 0 else 1)

    if args.install_startup or args.uninstall_startup:
        vbs = os.path.join(BASE_DIR, STARTUP_VBS)
        target = os.path.join(STARTUP_DIR, STARTUP_VBS)
        if args.install_startup:
            if not os.path.exists(vbs):
                print(f"No existe {vbs}. Generalo con: python {os.path.basename(__file__)} --loop (se crea solo)")
                sys.exit(1)
            os.makedirs(STARTUP_DIR, exist_ok=True)
            try:
                with open(target, "w", encoding="utf-8") as f:
                    f.write(open(vbs, encoding="utf-8").read())
                print(f"Arranque al iniciar sesion instalado en:\n  {target}")
                print("El vigilante se ejecutara oculto (sin ventana) cada vez que inicies sesion.")
            except OSError as e:
                print(f"No se pudo instalar: {e}")
                print("Opcion manual: copia arrancar_oculto.vbs a la carpeta de Inicio (Win+R -> shell:startup).")
                sys.exit(1)
        else:
            try:
                os.remove(target)
                print(f"Arranque al iniciar sesion eliminado: {target}")
            except OSError:
                print("No habia arranque instalado.")
        sys.exit(0)

    if args.loop:
        # genera arrancar_oculto.vbs si falta (para ejecucion oculta con pythonw)
        vbs = os.path.join(BASE_DIR, STARTUP_VBS)
        if not os.path.exists(vbs):
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pyw):
                pyw = "pythonw"
            with open(vbs, "w", encoding="utf-8") as f:
                f.write(f'Set sh = CreateObject("WScript.Shell")\r\n'
                        f'sh.Run "{pyw}" "{os.path.abspath(__file__)}" --loop --interval 30, 0, False\r\n')
            print(f"Generado {vbs} (ejecucion oculta). Doble clic para arrancar sin ventana.")

    cfg = load_config()

    if args.verify:
        print(f"Probando {len([s for s in cfg['sources'] if s.get('enabled', True)])} fuentes activas...\n")
        for src in cfg["sources"]:
            if not src.get("enabled", True):
                continue
            name = f"{src.get('ccaa')} ({src.get('boletin')})"
            try:
                anns = fetch_source(src)
                print(f"=== {name} ===  ({len(anns)} anuncios)")
                for a in anns[:6]:
                    print(f"   {a.title[:95]}")
                    print(f"   {a.url[:120]}")
            except Exception as e:
                print(f"=== {name} ===  ERROR: {e}")
        return

    if args.test:
        print(telegram_test(cfg["_token"], cfg["_chat"]))
        sys.exit(0)

    if args.poll_once:
        print(f"Updates atendidos: {poll_telegram(cfg, timeout=0)}")
        return

    if args.serve:
        signal.signal(signal.SIGTERM, _on_stop)
        signal.signal(signal.SIGINT, _on_stop)
        serve(cfg, args.interval)
        return

    if args.backtrack:
        items = run_backtrack(cfg, args.days, chat_id=cfg["_chat"], dry_run=args.dry_run)
        print(f"\nRetroceso de {args.days} dias: {len(items)} coincidencias")
        for a in items[:80]:
            print(f"  - [{a.source.get('ccaa')}] {a.title[:90]}\n    {a.url}")
        return

    if args.loop:
        while True:
            try:
                run_check(cfg)
            except Exception as e:
                log.exception("Error en la comprobacion: %s", e)
            time.sleep(max(1, args.interval) * 60)
        return

    sys.exit(run_check(cfg, verbose=True))


if __name__ == "__main__":
    main()
