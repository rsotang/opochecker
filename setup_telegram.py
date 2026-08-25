#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup_telegram.py — configura el bot de Telegram para opochecker.

Te pide el token (de @BotFather) y tu chat_id, los VALIDA contra la API de
Telegram (con un mensaje de prueba) y los guarda en config.json.
"""
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def api(method, token, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
            desc = body.get("description", "")
        except Exception:
            desc = str(e)
        return None, f"HTTP {e.code}: {desc}"
    except Exception as e:
        return None, str(e)


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    print("=== Configuracion de Telegram para Opochecker ===\n")
    print("Paso 1: el token de @BotFather")
    print("  Si no tienes bot: habla con @BotFather en Telegram, envia /newbot,")
    print("  y copia el token que te da (algo como 123456789:AAE...).")
    token = input("  Token: ").strip()

    me, err = api("getMe", token)
    if not me:
        print(f"\n[ERROR] El token no es valido: {err}")
        print("Revisa que hayas copiado el token completo, sin espacios ni comillas.")
        sys.exit(1)
    username = me.get("result", {}).get("username", "?")
    print(f"\n[OK] Token valido. Bot identificado: @{username}\n")

    print("Paso 2: tu chat_id (lo busco automaticamente)")
    print("  Para que aparezca aqui, abre el chat con @%s en Telegram y envia /start" % username)
    chats = {}
    ok_up, err_up = api("getUpdates", token)
    if ok_up:
        for u in ok_up.get("result", []):
            m = u.get("message") or u.get("edited_message") or {}
            ch = m.get("chat")
            if ch:
                name = ch.get("username") or ch.get("first_name") or ch.get("title") or "?"
                chats.setdefault(ch["id"], name)
    if chats:
        print("\n  Chats detectados (el tuyo es normalmente el de tu nombre):")
        options = list(chats.items())
        for i, (cid, name) in enumerate(options, 1):
            print(f"    [{i}] chat_id={cid}  ({name})")
        pick = input("  Elige un numero de la lista o escribe tu chat_id: ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(options):
            chat = str(options[int(pick) - 1][0])
        else:
            chat = pick
    else:
        print("\n  No hay chats detectados todavia.")
        print("  Envia /start a tu bot, espera 3 segundos y vuelve a ejecutar este asistente.")
        print("  (O abre https://api.telegram.org/bot<TOKEN>/getUpdates y copia el numero del campo \"id\".)")
        chat = input("  chat_id: ").strip()

    ok, err2 = api("sendMessage", token, {"chat_id": chat,
                                          "text": "<b>Opochecker</b> - configuracion correcta. "
                                                  "A partir de ahora avisare aqui de novedades de oposiciones.",
                                          "parse_mode": "HTML"})
    if ok and ok.get("ok"):
        print("\n[OK] Mensaje de prueba enviado al chat. Comprueba Telegram.")
    else:
        print(f"\n[ERROR] No se pudo enviar el mensaje al chat {chat}: {err2}")
        print("Envia /start a tu bot, espera 2 segundos, y revisa el chat_id (a veces es negativo).")
        sys.exit(1)

    cfg["telegram"]["bot_token"] = token
    cfg["telegram"]["chat_id"] = chat
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("\nConfiguracion guardada en config.json.")
    print("Siguiente paso: python opochecker.py --install-schedule  (o --install-startup)")


if __name__ == "__main__":
    main()
