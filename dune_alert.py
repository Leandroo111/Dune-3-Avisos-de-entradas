#!/usr/bin/env python3
"""
Bot que te avisa cuando AGREGAN FUNCIONES NUEVAS en la página de una película de
Showcase (entradas.todoshowcase.com). Las funciones IMAX se marcan con ⭐.

Solo avisa de funciones que nunca había visto: si una función desaparece y vuelve a
aparecer (por ejemplo, por una carga a medias de la página), no cuenta como nueva.
El estado "agotada" se ignora por completo.

La página carga las funciones con JavaScript y muestra un día a la vez (una pestaña por fecha),
así que el bot usa un navegador real (Playwright) y hace clic en cada fecha para leerlas todas.

Instalación local:
    pip install -r requirements.txt
    playwright install chromium        # Linux/VPS: playwright install --with-deps chromium

Cómo te avisa (configurá uno o los dos con variables de entorno):
  • Mail por SMTP:  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO  (y MAIL_FROM si hace falta)
        Gmail  → smtp.gmail.com / 465   (requiere contraseña de aplicación)
        Brevo  → smtp-relay.brevo.com / 587  (usuario = tu mail de Brevo, clave = SMTP key)
  • Notificación push al celu con ntfy:  NTFY_TOPIC  (y NTFY_SERVER si no usás ntfy.sh)

Ejemplos:
    python dune_alert.py --test-email                       # prueba los canales configurados
    python dune_alert.py                                    # loop infinito, cada 2 min
    python dune_alert.py --once --dry-run --dump texto.txt  # ver qué lee el bot
    python dune_alert.py --duration 28 --interval 60        # loop de 28 min (GitHub Actions)
"""
import argparse
import json
import os
import random
import re
import smtplib
import time
from collections import Counter, deque
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DEFAULT_URL = "https://entradas.todoshowcase.com/showcase/pelicula.aspx?filmid=6079"
TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")
DATE_RE = re.compile(
    r"\b\d{1,2}/\d{1,2}\b|\b(?:lun|mar|mi[eé]|jue|vie|s[aá]b|dom)\w*\.?\s+\d{1,2}\b",
    re.I,
)
DATE_ONLY_RE = re.compile(r"^(?:lun|mar|mi[eé]|jue|vie|s[aá]b|dom)\w*\.?\s+\d{1,2}/\d{1,2}$", re.I)
FORMAT_RE = re.compile(r"subtitulad|doblad|castellano|^\s*[234]d\b|^\s*imax\s*[-–]", re.I)
# Palabras de "agotada" que se borran de las líneas para que no afecten la detección
SOLDOUT_RE = re.compile(r"agotad\w*|sold[\s_-]?out|sin\s+(?:entradas|disponibilidad|lugares|butacas)", re.I)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9",
}
FAIL_ALERT_THRESHOLD = 10  # fallos seguidos (entre corridas) antes de avisarte
MAX_LINE = 80  # ignora párrafos largos (sinopsis, etc.)
STATE_VERSION = 2
BROWSER_WAIT_MS = 5000  # espera extra para que termine "cargando funciones..."


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------- obtención de la página ----------
def fetch_plain(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    if "charset" not in r.headers.get("content-type", "").lower():
        r.encoding = "utf-8"
    return r.text


def page_lines(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return [l for l in soup.get_text("\n", strip=True).split("\n") if l]


def click_date(page, label):
    """Hace clic en la pestaña de una fecha y espera a que carguen sus funciones."""
    try:
        for loc in page.get_by_text(label, exact=True).all():
            if not loc.is_visible():
                continue
            loc.click(timeout=5000)
            page.wait_for_timeout(400)
            for waiter in (
                lambda: page.wait_for_load_state("networkidle", timeout=10000),
                lambda: page.wait_for_function(
                    "() => !document.body.innerText.toLowerCase().includes('cargando funciones')", timeout=10000
                ),
            ):
                try:
                    waiter()
                except Exception:  # noqa: BLE001
                    pass
            page.wait_for_timeout(500)
            return True
    except Exception as e:  # noqa: BLE001
        log(f"No se pudo abrir la fecha {label}: {e}")
    return False


def fetch_pages(url, args):
    """Devuelve [(None, html_inicial), (fecha, html_de_esa_fecha), ...]."""
    if args.no_browser:
        html = fetch_plain(url)
        return [(None, html), ("(día mostrado)", html)]
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(extra_http_headers=HEADERS)
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(BROWSER_WAIT_MS)
            first = page.content()
            pages = [(None, first)]
            for label in dict.fromkeys(l for l in page_lines(first) if DATE_ONLY_RE.match(l)):
                if click_date(page, label):
                    pages.append((label, page.content()))
                else:
                    log(f"Fecha omitida: {label}")
            return pages
        finally:
            browser.close()


# ---------- lectura de la página ----------
def extract_showtimes(lines, date, only=None):
    """Devuelve un Counter {"fecha | complejo / formato → horario": cantidad}.
    Si `only` (regex) está definido, solo cuenta las funciones cuyo complejo/formato lo cumple."""
    items = Counter()
    complejo = formato = ""
    for line in lines:
        line = SOLDOUT_RE.sub("", line).strip(" -–·|")
        if not line or len(line) > MAX_LINE or DATE_ONLY_RE.match(line):
            continue
        if TIME_RE.search(line):
            ctx = " / ".join(x for x in (complejo, formato) if x)
            if only and not only.search(ctx):
                continue
            items[f"{date} | {ctx} → {line}" if ctx else f"{date} → {line}"] += 1
        elif FORMAT_RE.search(line):
            formato = line
        else:
            complejo, formato = line, ""
    return items


def read_items(url, args, expect):
    pages = fetch_pages(url, args)
    first_lines = page_lines(pages[0][1])
    if expect and not any(expect in l.lower() for l in first_lines):
        raise RuntimeError(f"la página no contiene '{expect}' (¿bloqueo o error de carga?)")
    tabs = list(dict.fromkeys(l for l in first_lines if DATE_ONLY_RE.match(l)))
    if not tabs:
        raise RuntimeError("no se encontraron las fechas (pestañas) de la página")
    only = re.compile(args.only, re.I) if args.only else None
    items, dump = Counter(), []
    leidas = 0
    for date, html in pages[1:]:
        lines = page_lines(html)
        items.update(extract_showtimes(lines, date, only))
        dump.append(f"=== {date} ===\n" + "\n".join(lines))
        leidas += 1
    if not leidas:
        raise RuntimeError("no se pudo abrir ninguna fecha")
    if leidas < len(tabs):
        log(f"Ojo: se leyeron {leidas} de {len(tabs)} fechas.")
    return items, "\n\n".join(dump)


# ---------- avisos ----------
def send_email(subject, body):
    user, pwd, to = (os.environ.get(k, "") for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 465)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ.get("MAIL_FROM") or user, to, subject
    msg.set_content(body)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:  # 587 / 2525: STARTTLS
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls()
                s.ehlo()
            s.login(user, pwd)
            s.send_message(msg)


def send_ntfy(subject, body, click=None, priority=4):
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    payload = {"topic": os.environ["NTFY_TOPIC"], "title": subject, "message": body, "priority": priority, "tags": ["clapper"]}
    if click:
        payload["click"] = click
    requests.post(server, json=payload, timeout=30).raise_for_status()


def notify(subject, body, dry_run=False, click=None, priority=4):
    if dry_run:
        log(f"[dry-run] AVISO → {subject}\n{body}\n")
        return
    channels = []
    if all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO")):
        channels.append(("mail", lambda: send_email(subject, body)))
    if os.environ.get("NTFY_TOPIC"):
        channels.append(("ntfy", lambda: send_ntfy(subject, body, click, priority)))
    if not channels:
        raise SystemExit("No hay ningún canal de aviso configurado (mail por SMTP y/o NTFY_TOPIC).")
    ok = 0
    for name, fn in channels:
        try:
            fn()
            ok += 1
            log(f"Aviso enviado por {name}: {subject}")
        except Exception as e:  # noqa: BLE001
            log(f"ERROR enviando por {name}: {e}")
    if not ok:
        raise RuntimeError("fallaron todos los canales de aviso")


# ---------- estado ----------
def load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))


def find_new(items, seen):
    """Funciones cuya cantidad supera la máxima que ya habíamos visto."""
    return {k: c - seen.get(k, 0) for k, c in items.items() if c > seen.get(k, 0)}


# ---------- chequeo ----------
def check(url, args, state, expect):
    fails = state.setdefault("_fails", {})
    try:
        items, dump = read_items(url, args, expect)
    except Exception as e:  # noqa: BLE001
        fails[url] = fails.get(url, 0) + 1
        log(f"Error leyendo la página: {e} ({fails[url]} seguidos)")
        if fails[url] == FAIL_ALERT_THRESHOLD:
            notify(
                "⚠️ El bot de Duna no puede leer la página",
                f"Van {FAIL_ALERT_THRESHOLD} chequeos seguidos con error en {url}.\nÚltimo error: {e}\n"
                "Puede que Showcase esté bloqueando al bot.",
                args.dry_run,
            )
        return
    fails[url] = 0
    if args.dump:
        Path(args.dump).write_text(dump, encoding="utf-8")

    prev = state.get(url) or {}
    seen = prev.get("seen") if prev.get("v") == STATE_VERSION else None  # estado de otra versión → línea base nueva
    if seen is None:
        state[url] = {"v": STATE_VERSION, "seen": dict(items)}
        log(f"Línea base guardada: {sum(items.values())} horarios/fechas detectados.")
        if not items:
            log("Ojo: no se detectó ninguna función. Revisá con --dump qué ve el bot.")
        return

    new = find_new(items, seen)
    if new:
        log("Posibles funciones nuevas, confirmando...")
        time.sleep(args.confirm_wait)
        try:
            items2, _ = read_items(url, args, expect)
        except Exception as e:  # noqa: BLE001
            log(f"No se pudo confirmar: {e}")
            return
        new2 = find_new(items2, seen)
        new = {k: min(v, new2[k]) for k, v in new.items() if k in new2}
        if not new:
            log("No se confirmó (carga inestable); descarto.")
            return
        items = items2

    for k, c in items.items():
        seen[k] = max(seen.get(k, 0), c)
    state[url] = {"v": STATE_VERSION, "seen": seen}
    if not new:
        log(f"Sin funciones nuevas ({sum(items.values())} detectadas).")
        return

    body = (
        "Funciones nuevas:\n"
        + "\n".join(f"  ⭐ {k}" + (f" (x{v})" if v > 1 else "") for k, v in sorted(new.items()))
        + f"\n\nComprá acá: {url}\n"
    )
    notify("🎬 Duna: parte tres — ¡funciones nuevas en IMAX!" if args.only else "🎬 Duna: parte tres — ¡funciones nuevas!", body, args.dry_run, click=url, priority=5)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", action="append", default=[])
    ap.add_argument("--interval", type=int, default=120, help="segundos entre chequeos")
    ap.add_argument("--duration", type=float, default=0, help="minutos que dura el loop (0 = infinito)")
    ap.add_argument("--state", default="dune_state.json")
    ap.add_argument("--dump", default="", help="guarda el texto que ve el bot, para depurar")
    ap.add_argument("--expect", default=None, help="texto que debe aparecer en la página (si no, se cuenta como error)")
    ap.add_argument("--confirm-wait", type=int, default=10, help="segundos antes de re-confirmar una función nueva")
    ap.add_argument("--only", default="IMAX", help='solo vigila funciones cuyo complejo/formato coincide con este texto (regex; "" = todas)')
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-email", "--test", dest="test", action="store_true", help="manda un aviso de prueba")
    args = ap.parse_args()

    if args.test:
        notify("✅ Bot de Duna funcionando", "Si ves esto, el aviso llega bien.", args.dry_run)
        return

    urls = args.url or [DEFAULT_URL]
    expect = args.expect if args.expect is not None else ("duna" if urls == [DEFAULT_URL] else "")
    state = load_state(args.state)
    end = time.time() + args.duration * 60 if args.duration else None
    while True:
        for url in urls:
            check(url, args, state, expect.lower())
        save_state(args.state, state)
        if args.once or (end and time.time() + args.interval > end):
            break
        time.sleep(args.interval + random.randint(0, 10))


if __name__ == "__main__":
    main()
