#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opochecker — vigila los boletines oficiales de las CC.AA. espanolas en busca
de convocatorias de facultativos especialistas y avisa por Telegram.

Uso:
  python opochecker.py --verify               Probar fuentes sin notificar
  python opochecker.py --test                 Enviar mensaje de prueba a Telegram
  python opochecker.py --check                Ejecutar una comprobacion (avisa novedades)
  python opochecker.py --install-schedule     Crear tarea en el Programador (cada 30 min)
  python opochecker.py --uninstall-schedule   Eliminar la tarea programada
  python opochecker.py --loop [--interval N]  Bucle continuo (N minutos, defecto 30)
"""

import argparse
import html
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "opochecker.log")
TASK_NAME = "Opochecker"
STARTUP_VBS = "arrancar_oculto.vbs"
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", BASE_DIR),
                           "Microsoft", "Windows", "Start Menu", "Programs", "Startup")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

log = logging.getLogger("opochecker")


# ---------------------------------------------------------------- utilidades

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def is_match(title: str, url: str, keyword_groups) -> bool:
    """True si title+url contiene alguna combinacion de keywords (todas las del grupo)."""
    blob = normalize(f"{title} {url}")
    return any(all(normalize(term) in blob for term in group) for group in keyword_groups)


def http_get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        raw = r.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


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


# ------------------------------------------------------------- config

def load_config() -> dict:
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else CONFIG_PATH + ".example"
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    token = cfg.get("telegram", {}).get("bot_token") or os.environ.get("OPO_TELEGRAM_TOKEN", "")
    chat = cfg.get("telegram", {}).get("chat_id") or os.environ.get("OPO_TELEGRAM_CHAT_ID", "")
    cfg["_token"] = token
    cfg["_chat"] = str(chat)
    return cfg


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"seen": {}}


def save_state(state: dict):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


# ----------------------------------------------------------- telegram

def telegram_api(method: str, token: str, params: dict = None) -> tuple:
    """Llama a la API de Telegram. Devuelve (json|None, error_descripcion|None)."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
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


def telegram_send(token: str, chat: str, text: str) -> bool:
    if not token or not chat:
        log.error("Telegram no configurado: falta bot_token/chat_id en config.json (o env OPO_TELEGRAM_*)")
        return False
    ok, err = telegram_api("sendMessage", token, {
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true"})
    if not ok:
        log.error("Telegram (sendMessage): %s", err)
        return False
    if not ok.get("ok"):
        log.error("Telegram respondio ok=false")
        return False
    return True


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


# ------------------------------------------------------------- chequeo

PARSERS = {
    "bocm": parse_bocm,
    "bocyl": parse_bocyl,
    "canarias": parse_canarias,
    "doe": parse_doe,
    "bopa": parse_bopa,
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
    elif stype in PARSERS:
        body = http_get(url)
        out = PARSERS[stype](body)
    else:
        raise ValueError(f"Tipo de fuente desconocido: {stype}")

    for a in out:
        a.source = src
    return out


def run_check(cfg: dict, verbose: bool = False) -> int:
    state = load_state()
    seen = state.setdefault("seen", {})
    keywords = cfg.get("keywords", [["facultativo", "especialista"]])
    new_items = []

    for src in cfg.get("sources", []):
        if not src.get("enabled", True):
            continue
        name = f"{src.get('ccaa')} ({src.get('boletin')})"
        try:
            anns = fetch_source(src)
            log.info("%s: %d anuncios", name, len(anns))
            for a in anns:
                if not a.title:
                    continue
                if not is_match(a.title, a.url, keywords):
                    continue
                if a.key in seen:
                    continue
                new_items.append(a)
        except Exception as e:
            log.error("%s: ERROR -> %s", name, e)
            if verbose:
                print(f"  [ERROR] {name}: {e}")

    if verbose:
        print(f"\nNovedades detectadas: {len(new_items)}")
        for a in new_items[:50]:
            print(f"  - [{a.source.get('ccaa')}] {a.title[:90]}\n    {a.url}")

    if not new_items:
        return 0

    token, chat = cfg["_token"], cfg["_chat"]
    ok_all = True
    for a in new_items:
        msg = (f"<b>{esc(a.source.get('ccaa'))}</b> - {esc(a.source.get('boletin'))}\n"
               f"{esc(a.title)}\n<a href=\"{html.escape(a.url, quote=True)}\">ver documento</a>")
        if telegram_send(token, chat, msg):
            seen[a.key] = datetime.now().isoformat(timespec="seconds")
        else:
            ok_all = False

    save_state(state)
    return 0 if ok_all else 1


# ----------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="Vigilante de oposiciones de facultativos especialistas")
    ap.add_argument("--verify", action="store_true", help="probar fuentes sin enviar Telegram")
    ap.add_argument("--test", action="store_true", help="enviar mensaje de prueba a Telegram")
    ap.add_argument("--check", action="store_true", help="ejecutar comprobacion y notificar novedades")
    ap.add_argument("--loop", action="store_true", help="bucle continuo")
    ap.add_argument("--interval", type=int, default=30, help="minutos entre comprobaciones (defecto 30)")
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
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
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

    if args.loop:
        while True:
            try:
                run_check(cfg)
            except Exception as e:
                log.exception("Error en la comprobacion: %s", e)
            time.sleep(args.interval * 60)
        return

    sys.exit(run_check(cfg, verbose=True))


if __name__ == "__main__":
    main()
