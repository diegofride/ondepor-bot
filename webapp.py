# -*- coding: utf-8 -*-
"""
==========================================================================
  webapp.py  -  Version WEB del bot de reservas OnDepor
==========================================================================
Una aplicacion web (Flask) con:
  - Login con contraseña propia (para que solo entres vos).
  - Pestañas: Reservas, Jugadores, Reservadores.
  - Un RELOJ INTERNO (hilo de fondo) que dispara cada reserva al segundo
    exacto de su apertura (turno -24h +1min). Reemplaza al Programador de
    tareas de Windows -> sirve para dejarlo en un servidor Linux 24/7.

Corre igual en Windows (para probar) y en Linux (Oracle Cloud / VPS).
Requisitos:  pip install flask requests ntplib
Arranque:    python webapp.py     (y abrís http://localhost:8080)
==========================================================================
"""
import os
import re
import json
import time
import shutil
import secrets
import threading
import datetime
import unicodedata
from functools import wraps

from flask import (Flask, request, redirect, url_for, session,
                   jsonify, render_template_string, flash, abort)
from markupsafe import escape
from werkzeug.middleware.proxy_fix import ProxyFix

from ondepor_core import (OnDepor, calcular_apertura, hora_real, sincronizar_reloj,
                          _epoch_ar, hoy_ar, ahora_ar)
import reservar_bot as rb
import encuesta as enc

AQUI = os.path.dirname(os.path.abspath(__file__))
CLUB_DEFAULT = 1102
# Las dos "sedes" del club, que en OnDepor son clubes distintos:
#   Día (1102, 8-17h, 11 canchas)  y  Noche (504, 18-23h, 8 canchas, sin CHACRA).
# Se puede tener 1 turno en cada una el mismo día (por eso el conflicto se mira por club).
def _canchas(pares):
    return [(str(n), f"{n} · {g}") for n, g in pares]

SEDES = {
    "dia": {
        "label": "Día (8 a 17 h)", "club_id": 1102,
        "horas": [f"{h:02d}:00" for h in range(8, 18)],
        "canchas": _canchas([(1, "QUINTA"), (2, "QUINTA"), (3, "QUINTA"), (4, "QUINTA"),
                             (5, "KINERET"), (6, "KINERET"), (7, "KINERET"), (8, "KINERET"),
                             (9, "CHACRA"), (10, "CHACRA"), (11, "CHACRA")]),
        "zonas": [("QUINTA", "Zona QUINTA (canchas 1-4)"), ("KINERET", "Zona KINERET (canchas 5-8)"),
                  ("CHACRA", "Zona CHACRA (canchas 9-11)")],
    },
    "noche": {
        "label": "Noche (18 a 23 h)", "club_id": 504,
        "horas": [f"{h:02d}:00" for h in range(18, 24)],
        "canchas": _canchas([(1, "QUINTA"), (2, "QUINTA"), (3, "QUINTA"), (4, "QUINTA"),
                             (5, "KINERET"), (6, "KINERET"), (7, "KINERET"), (8, "KINERET")]),
        "zonas": [("QUINTA", "Zona QUINTA (canchas 1-4)"), ("KINERET", "Zona KINERET (canchas 5-8)")],
    },
}
SEDE_POR_CLUB = {v["club_id"]: k for k, v in SEDES.items()}


# ----------------------------------------------------------------------
#  Configuracion (contraseña + secret key) en webconfig.json
# ----------------------------------------------------------------------
def cargar_webconfig():
    ruta = os.path.join(AQUI, "webconfig.json")
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    cfg = {"password": "cambiame", "secret_key": secrets.token_hex(16)}
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


WEBCFG = cargar_webconfig()
PASSWORD = os.environ.get("PANEL_PASSWORD", WEBCFG.get("password", "cambiame"))

# Dominio propio para la ENCUESTA (ej: "encuestareservas.duckdns.org"). Sirve para que
# el link que se pega en WhatsApp no revele la app de reservas. Si está vacío, la
# encuesta usa el mismo dominio que el panel.
DOMINIO_ENCUESTA = (os.environ.get("DOMINIO_ENCUESTA")
                    or WEBCFG.get("dominio_encuesta", "")).strip().lower()

app = Flask(__name__)
app.secret_key = WEBCFG.get("secret_key", secrets.token_hex(16))

# Detrás de Caddy (reverse proxy): tomar la IP y el esquema reales de los headers
# X-Forwarded-* (así el rate-limit ve la IP del cliente y no 127.0.0.1, y Flask sabe
# que la conexión es HTTPS para poder marcar la cookie como Secure).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.register_blueprint(enc.bp)   # encuesta pública (sin login, sin rastro de la app)


@app.before_request
def _aislar_dominio_encuesta():
    """En el dominio de la encuesta solo existe la encuesta: cualquier otra ruta da 404.
    Así el que recibe el link no puede llegar al panel de reservas ni saber que existe."""
    if not DOMINIO_ENCUESTA:
        return None
    host = (request.host or "").split(":")[0].lower()
    if host != DOMINIO_ENCUESTA:
        return None
    if request.path.startswith("/e/"):
        return None
    if request.path == "/":                       # la raíz lleva a la encuesta abierta
        e = enc.cargar_encuesta()
        if e and e.get("token"):
            return redirect(f"/e/{e['token']}")
    abort(404)

# Cookie de sesión endurecida. SECURE exige HTTPS -> en pruebas locales por HTTP se
# desactiva con PANEL_COOKIE_INSEGURA=1 (para poder loguearse en localhost).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(os.environ.get("PANEL_COOKIE_INSEGURA") != "1"),
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=7),
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,   # 1 MB: cortar payloads absurdos
)


# ----------------------------------------------------------------------
#  Rate limiting simple en memoria (hay una sola instancia), por IP
# ----------------------------------------------------------------------
_rl_lock = threading.Lock()
_rl_hits = {}   # clave -> [timestamps]


def rate_ok(clave, maximo, ventana_seg, contar=True):
    """True si 'clave' hizo menos de 'maximo' intentos en los últimos 'ventana_seg'.
    contar=False solo consulta (sirve para que un login EXITOSO no gaste cupo: si no,
    varios usuarios detrás de la misma IP se bloquean entre ellos)."""
    ahora = time.time()
    with _rl_lock:
        # purga de claves viejas (IPs que no vuelven): si no, el dict crece para siempre
        if len(_rl_hits) > 500:
            for k in [k for k, v in _rl_hits.items() if not v or ahora - v[-1] > 3600]:
                _rl_hits.pop(k, None)
        q = [t for t in _rl_hits.get(clave, []) if ahora - t < ventana_seg]
        if len(q) >= maximo:
            _rl_hits[clave] = q
            return False
        if contar:
            q.append(ahora)
        _rl_hits[clave] = q
        return True


# ----------------------------------------------------------------------
#  Usuarios del PANEL (para login con usuarios distintos) en usuarios.json
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
#  ROLES y PERMISOS
#  Cada usuario tiene un rol; el rol define qué puede hacer. Antes solo existía
#  "admin sí/no": un usuario con admin=True se lee como rol "admin" (compatibilidad).
# ----------------------------------------------------------------------
PERMISOS = {
    "usuarios":        "Administrar usuarios del panel",
    "todas_reservas":  "Ver y borrar reservas de todos (no solo las propias)",
    "cambiar_ondepor": "Cambiar jugadores de una reserva ya confirmada",
    "agregar_jugadores": "Agregar jugadores nuevos a la base",
    "gestionar_bases": "Editar y borrar jugadores y reservadores",
    "encuesta":        "Abrir y cerrar la encuesta semanal del grupo",
}
# Config de fábrica. Los permisos de cada rol se pueden EDITAR desde el panel de Usuarios
# y quedan guardados en roles.json; esto es lo que se usa si ese archivo no existe.
ROLES_BASE = {
    "admin":    {"etiqueta": "Administrador",
                 "ayuda": "Control total: usuarios, reservas de todos y cambios en OnDepor.",
                 "permisos": {"usuarios", "todas_reservas", "cambiar_ondepor",
                              "gestionar_bases", "encuesta", "agregar_jugadores"}},
    "operador": {"etiqueta": "Operador",
                 "ayuda": "Maneja reservas de todos, jugadores y reservadores. No toca usuarios.",
                 "permisos": {"todas_reservas", "cambiar_ondepor", "gestionar_bases",
                              "encuesta", "agregar_jugadores"}},
    "jugador":  {"etiqueta": "Jugador",
                 "ayuda": "Crea y maneja sus propias reservas, y puede sumar jugadores nuevos.",
                 "permisos": {"agregar_jugadores"}},
}
ROL_POR_DEFECTO = "jugador"
ROLES = {}


def cargar_roles():
    """Arma ROLES combinando la config de fábrica con lo guardado en roles.json.
    SIEMPRE deja que 'admin' pueda administrar usuarios: si no, nadie podría volver a
    entrar al panel para arreglarlo."""
    global ROLES
    guardado = rb.leer_json("roles.json", {}).get("roles", {})
    nuevo = {}
    for k, base in ROLES_BASE.items():
        permisos = set(guardado[k]) & set(PERMISOS) if k in guardado else set(base["permisos"])
        nuevo[k] = {"etiqueta": base["etiqueta"], "ayuda": base["ayuda"], "permisos": permisos}
    nuevo["admin"]["permisos"].add("usuarios")     # candado de seguridad
    ROLES = nuevo
    return ROLES


def rol_de(u):
    """Rol de un usuario del archivo (soporta el formato viejo con admin: true/false)."""
    r = str(u.get("rol", "")).strip().lower()
    if r in ROLES:
        return r
    return "admin" if u.get("admin") else ROL_POR_DEFECTO


def puede_rol(rol, permiso):
    if not ROLES:
        cargar_roles()
    return permiso in ROLES.get(rol, ROLES[ROL_POR_DEFECTO])["permisos"]


def puede(permiso):
    """¿El usuario logueado tiene este permiso?"""
    rol = session.get("rol") or ("admin" if session.get("admin") else ROL_POR_DEFECTO)
    return puede_rol(rol, permiso)


def cargar_usuarios():
    ruta = os.path.join(AQUI, "usuarios.json")
    if os.path.exists(ruta):
        try:
            us = json.load(open(ruta, encoding="utf-8")).get("usuarios", [])
        except Exception:
            return []
        # seguridad/migración: siempre tiene que haber al menos un administrador
        if us and not any(rol_de(u) == "admin" for u in us):
            us[0]["rol"], us[0]["admin"] = "admin", True
            try:
                guardar_usuarios(us)
            except Exception:
                pass
        return us
    # primera vez: sembrar con un usuario ADMIN usando la contraseña actual del panel
    us = [{"usuario": "diego", "clave": PASSWORD, "nombre": "Diego", "rol": "admin", "admin": True}]
    rb.escribir_json("usuarios.json", {"usuarios": us})
    return us


def guardar_usuarios(lista):
    rb.escribir_json("usuarios.json", {"usuarios": lista})


def validar_usuario(usuario, clave):
    for u in cargar_usuarios():
        if str(u.get("usuario", "")).strip().lower() == str(usuario).strip().lower() \
           and u.get("clave") == clave:
            return u
    return None


cargar_usuarios()  # asegura que exista usuarios.json al arrancar


# ----------------------------------------------------------------------
#  Helpers de datos (usa las mismas 3 bases JSON)
# ----------------------------------------------------------------------
def cargar(nombre, clave):
    return rb._cargar(nombre, clave)


def guardar(nombre, clave, lista, ayuda=None):
    # lee+modifica+escribe bajo el mismo lock (atómico) para no perder datos ni corromper el archivo
    with rb._io_lock:
        data = rb.leer_json(nombre, {})
        if ayuda and "_ayuda" not in data:
            data["_ayuda"] = ayuda
        data[clave] = lista
        rb.escribir_json(nombre, data)


def reservador_con_clave():
    for r in cargar("db_reservadores.json", "reservadores"):
        if r.get("clave") and not str(r["clave"]).startswith("PONER"):
            return r
    return None


def esc(x):
    """Escapa datos para insertarlos SEGUROS dentro del HTML embebido (anti-XSS).
    Todo dato de usuario (nombres, usuarios, mensajes) debe pasar por acá al mostrarse."""
    return escape("" if x is None else str(x))


def limpiar_nombre(s, maxlen=60):
    """Sanea un nombre/usuario al GUARDAR: saca < > y caracteres de control y recorta.
    Es defensa en profundidad junto con esc() al mostrar."""
    s = str(s or "").strip()
    s = re.sub(r"[<>\r\n\t\x00-\x1f]", "", s)
    return s[:maxlen]


def _slug(s):
    """Slug ascii para armar ids únicos (dario-waingarten != dario-zelione)."""
    s = unicodedata.normalize("NFKD", str(s or "").strip().lower())
    s = s.encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "x"


def _css_ver():
    """Versión del CSS = mtime del archivo, para romper el caché del navegador solo
    cuando el CSS realmente cambia (sin tener que subir un número a mano)."""
    try:
        return str(int(os.path.getmtime(os.path.join(AQUI, "static", "estilo.css"))))
    except OSError:
        return "1"


# ---- Íconos de línea (SVG inline, sin dependencias externas; heredan color y tamaño) ----
_ICON_PATHS = {
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/>',
    "moon": '<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/>',
    "bolt": '<path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "calendar": '<rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-.5 4"/><path d="M20 4v6h-6"/>',
    "alert": '<path d="M12 3 2 20h20L12 3z"/><path d="M12 9v5M12 17.4v.2"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    "x": '<path d="M6 6l12 12M6 18L18 6"/>',
    "trash": '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    "edit": '<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>',
    "users": '<circle cx="9" cy="8" r="3.2"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 5a3 3 0 0 1 0 6M21.5 20a6.5 6.5 0 0 0-4-6"/>',
    "pin": '<path d="M12 22s7-6.2 7-12a7 7 0 1 0-14 0c0 5.8 7 12 7 12z"/><circle cx="12" cy="10" r="2.5"/>',
    "check": '<path d="M5 12l4 4L19 6"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "logout": '<path d="M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 17l-5-5 5-5M5 12h11"/>',
    "login": '<path d="M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4M14 17l5-5-5-5M19 12H9"/>',
    "check-circle": '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-5"/>',
    "x-circle": '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "save": '<path d="M5 3h11l3 3v15H5z"/><path d="M8 3v6h7M8 21v-7h8v7"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    "inbox": '<path d="M3 13h5l1 3h6l1-3h5M4 4h16v16H4z"/>',
    "chart": '<path d="M3 21h18M7 21V10M12 21V4M17 21v-7"/>',
    "bell": '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 8-3 8h18s-3-1-3-8"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
    "eye": '<path d="M1.5 12S5 5.5 12 5.5 22.5 12 22.5 12 19 18.5 12 18.5 1.5 12 1.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "repeat": '<path d="M17 2l4 4-4 4"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
}


def icon(nombre, extra=""):
    """Devuelve un <svg> de línea (24x24, currentColor). Uso: {icon('bolt')} Texto."""
    p = _ICON_PATHS.get(nombre, "")
    return (f'<svg class="ic {extra}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{p}</svg>')


def fmt_fecha(f):
    """Convierte '2026-07-14' -> '14/07/2026' (formato argentino)."""
    try:
        return datetime.date.fromisoformat(str(f)).strftime("%d/%m/%Y")
    except Exception:
        return str(f)


def zona_de(n):
    """Zona de una cancha por su número (1-4 QUINTA, 5-8 KINERET, 9-11 CHACRA)."""
    try:
        n = int(n)
    except Exception:
        return ""
    if 1 <= n <= 4:
        return "QUINTA"
    if 5 <= n <= 8:
        return "KINERET"
    if 9 <= n <= 11:
        return "CHACRA"
    return ""


def desc_cancha(c):
    """Texto lindo de la cancha elegida: '5 (KINERET)', 'zona KINERET', 'cualquiera'."""
    c = str(c or "").strip()
    if c == "":
        return "cualquiera"
    if c.isdigit():
        z = zona_de(c)
        return f"{c} ({z})" if z else c
    return f"zona {c.upper()}"


# ----------------------------------------------------------------------
#  Estados de las reservas (programada / en_curso / reservada / fallada / vencida)
# ----------------------------------------------------------------------
ESTADOS_PATH = os.path.join(AQUI, "estados.json")
_estados = {}
_estados_lock = threading.Lock()

# knobs del reloj interno (_manager)
VENTANA_PRE_SEG = 210       # lanza el disparo cuando faltan <= estos segundos para la apertura
VENTANA_VENCIDA_SEG = 180   # si pasaron más de estos segundos desde la apertura, se venció
MANAGER_CICLO_SEG = 12      # cada cuánto revisa el manager
LIMPIEZA_CONFIRMADA_SEG = 3600   # una confirmada se borra sola 1 h después de la hora del turno
CAMBIO_PROG_ANTES_SEG = 600      # el cambio de nombres programado se ejecuta 10 min antes del turno
BACKUP_DIR = os.path.join(AQUI, "backups")
BACKUP_DIAS = 14                 # cuántos días de backup diario se conservan
ARCHIVOS_DATOS = ("reservas.json", "estados.json", "db_jugadores.json",
                  "db_reservadores.json", "usuarios.json", "historial.json")


def _backup_diario():
    """Copia los JSON a backups/AAAA-MM-DD/ una vez por día y rota los viejos.
    Es la única red contra un archivo corrupto o un borrado de más."""
    dia = hoy_ar().isoformat()
    destino = os.path.join(BACKUP_DIR, dia)
    if os.path.isdir(destino):
        return False                      # el de hoy ya está hecho
    os.makedirs(destino, exist_ok=True)
    for f in ARCHIVOS_DATOS:
        src = os.path.join(AQUI, f)
        if os.path.exists(src):
            try:
                shutil.copy2(src, os.path.join(destino, f))
            except OSError as e:
                print(f"[backup] no pude copiar {f}: {e}", flush=True)
    try:                                  # rotar: dejar solo los últimos BACKUP_DIAS
        dias = sorted(d for d in os.listdir(BACKUP_DIR)
                      if os.path.isdir(os.path.join(BACKUP_DIR, d)))
        for viejo in dias[:-BACKUP_DIAS]:
            shutil.rmtree(os.path.join(BACKUP_DIR, viejo), ignore_errors=True)
    except OSError:
        pass
    print(f"[backup] copia diaria {dia} lista", flush=True)
    return True


# ----------------------------------------------------------------------
#  NOTIFICACIONES PUSH (Web Push / VAPID) — al celular, aunque la app esté cerrada.
#  Cada suscripción queda atada al USUARIO del panel que la creó, así el aviso de
#  una reserva le llega SOLO al que la programó.
# ----------------------------------------------------------------------
try:
    from pywebpush import webpush, WebPushException
except ImportError:                       # sin la librería, la app funciona igual (sin push)
    webpush = None
    WebPushException = Exception

_VAPID = {}
_VAPID_PEM = os.path.join(AQUI, "vapid_private.pem")
try:
    with open(os.path.join(AQUI, "vapid.json"), encoding="utf-8") as _f:
        _VAPID = json.load(_f)
    # pywebpush quiere la clave privada como ARCHIVO .pem (pasarle el texto falla con
    # "Could not deserialize key data"); si no está, se escribe desde el json.
    if _VAPID.get("private_pem") and not os.path.exists(_VAPID_PEM):
        with open(_VAPID_PEM, "w", encoding="utf-8") as _p:
            _p.write(_VAPID["private_pem"])
        os.chmod(_VAPID_PEM, 0o600)
except (FileNotFoundError, json.JSONDecodeError, OSError):
    pass
PUSH_ACTIVO = bool(webpush and _VAPID.get("public_key") and os.path.exists(_VAPID_PEM))


def _subs_de(usuario=None):
    """Suscripciones push guardadas. Si se pasa 'usuario', solo las de ese usuario."""
    data = rb.leer_json("push_subs.json", {"subs": {}}).get("subs", {})
    return data.get(usuario, []) if usuario else data


def notificar(usuario, titulo, cuerpo, url="/reservas"):
    """Manda un push SOLO a los dispositivos del usuario indicado (el que creó la reserva).
    Nunca lanza: si el push falla, la reserva ya está hecha igual."""
    if not PUSH_ACTIVO or not usuario:
        return 0
    subs = _subs_de(usuario)
    if not subs:
        return 0
    payload = json.dumps({"titulo": titulo, "cuerpo": cuerpo, "url": url}, ensure_ascii=False)
    enviados, muertas = 0, []
    for s in subs:
        try:
            webpush(subscription_info=s, data=payload,
                    vapid_private_key=_VAPID_PEM,     # el PATH del .pem, no su contenido
                    vapid_claims={"sub": _VAPID.get("sub", "mailto:admin@example.com")})
            enviados += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):        # suscripción vencida: el navegador la dio de baja
                muertas.append(s)
            else:
                print(f"[push] error enviando a {usuario}: {str(e)[:120]}", flush=True)
        except Exception as e:
            print(f"[push] error inesperado: {str(e)[:120]}", flush=True)
    if muertas:                            # limpiar las que ya no existen
        with rb._io_lock:
            data = rb.leer_json("push_subs.json", {"subs": {}})
            quedan = [x for x in data.get("subs", {}).get(usuario, []) if x not in muertas]
            data.setdefault("subs", {})[usuario] = quedan
            rb.escribir_json("push_subs.json", data)
    return enviados


# ----------------------------------------------------------------------
#  HISTORIAL: qué pasó con cada reserva (antes se perdía al limpiarla)
# ----------------------------------------------------------------------
def _al_historial(job, est, cancha_real="", detalle="", creador=""):
    """Guarda una reserva terminada en historial.json (append). Se llama justo antes
    de que la limpieza la borre, así queda el registro de qué se ganó y qué no."""
    reg = {
        "id": job.get("id"), "fecha": job.get("fecha"), "hora": job.get("hora_desde"),
        "club_id": job.get("club_id"), "cancha_pedida": str(job.get("cancha", "")),
        "cancha_real": cancha_real, "estado": est,
        "reservador": job.get("reservador"), "creado_por": creador,
        "jugadores": [x.get("nombre") for x in job.get("jugadores_full", [])],
        "detalle": str(detalle)[:200],
        "registrado": ahora_ar().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with rb._io_lock:
        h = rb.leer_json("historial.json", {"historial": []})
        lista = h.get("historial", [])
        if any(x.get("id") == reg["id"] and x.get("fecha") == reg["fecha"] for x in lista):
            return                        # ya estaba registrada
        lista.append(reg)
        h["historial"] = lista[-500:]     # tope: últimas 500
        rb.escribir_json("historial.json", h)


def _cargar_estados():
    global _estados
    try:
        with open(ESTADOS_PATH, encoding="utf-8") as f:
            _estados = json.load(f)
    except Exception:
        _estados = {}
    # al arrancar, cualquiera "en_curso" (quedó a medias por un reinicio) vuelve a programarse
    for k in list(_estados):
        if _estados[k].get("estado") == "en_curso":
            del _estados[k]


def _guardar_estados():
    with _estados_lock:
        rb.escribir_json("estados.json", _estados)   # escritura atómica


def set_estado(rid, estado, detalle=""):
    with _estados_lock:
        _estados[rid] = {"estado": estado, "detalle": detalle,
                         "cuando": ahora_ar().strftime("%Y-%m-%d %H:%M:%S")}   # hora ARG
    _guardar_estados()


def get_estado(rid):
    return _estados.get(rid, {}).get("estado", "programada")


def _leer_log(rid):
    """Contenido del log de una reserva (o '' si no existe)."""
    try:
        with open(os.path.join(AQUI, "logs", f"{rid}.log"), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def cancha_reservada(rid, txt=None):
    """En qué cancha ENTRÓ finalmente la reserva (ej '05-KINERET'), o '' si no se sabe.
    'txt' opcional = contenido del log ya leído (para no releer el archivo)."""
    txt = _leer_log(rid) if txt is None else txt
    ms = re.findall(r"RESERVA CONFIRMADA \(OK\)\s+cancha=(\S+)", txt)
    return ms[-1].strip("*") if ms else ""


def resumen_log(rid, txt=None):
    """Frase corta con lo último importante que pasó (para mostrar en la lista).
    'txt' opcional = contenido del log ya leído (para no releer el archivo)."""
    txt = _leer_log(rid) if txt is None else txt
    claves = ("RESERVA CONFIRMADA", "NO SE PUDO", "Motivo", "RECHAZO", "ERROR de login")
    for ln in reversed(txt.splitlines()):
        for k in claves:
            if k in ln:
                return ln.split("] ", 2)[-1].strip()[:160]   # sacar prefijo [hora] [id]
    return ""


# ----------------------------------------------------------------------
#  RELOJ INTERNO: dispara las reservas a su hora
# ----------------------------------------------------------------------
def _registrar_resultado(job, est, detalle=""):
    """Al terminar un disparo: guarda en el historial y avisa por push AL QUE LA CREÓ."""
    rid = job["id"]
    try:
        logtxt = _leer_log(rid)
        cancha_real = cancha_reservada(rid, logtxt)
        if not detalle:
            detalle = resumen_log(rid, logtxt)
        rsv = next((x for x in cargar("reservas.json", "reservas") if x.get("id") == rid), {})
        creador = rsv.get("creado_por", "")
        _al_historial(job, est, cancha_real, detalle, creador)
        # aviso push SOLO al usuario que programó la reserva
        cuando = f"{fmt_fecha(job['fecha'])} {job['hora_desde']}"
        if est == "reservada":
            titulo = "✅ Cancha confirmada"
            cuerpo = f"{cuando} · {desc_cancha(cancha_real.split('-')[0].lstrip('0') if '-' in cancha_real else job['cancha'])}"
            pedida = str(job.get("cancha", "")).strip()
            real_num = cancha_real.split("-")[0].lstrip("0") if "-" in cancha_real else ""
            if pedida.isdigit() and real_num and real_num != pedida:
                cuerpo += f" (no era la {pedida})"
        else:
            titulo = "❌ No se pudo reservar"
            cuerpo = f"{cuando} · {detalle or 'revisá el detalle en la app'}"
        notificar(creador, titulo, cuerpo)
    except Exception as e:
        print(f"[historial] no pude registrar {rid}: {e}", flush=True)


def _disparar_job(job, esperar=True):
    rid = job["id"]
    set_estado(rid, "en_curso")
    try:
        ok = rb.disparar(job, esperar=esperar)   # loguea a logs/<id>.log
        est = "reservada" if ok else "fallada"
        set_estado(rid, est)
        _registrar_resultado(job, est)
        return ok
    except Exception as e:
        set_estado(rid, "fallada", str(e)[:200])
        _registrar_resultado(job, "fallada", str(e)[:200])
        return False


def _limpiar_confirmadas_viejas(jobs):
    """Las reservas CONFIRMADAS se borran solas 1 h después de la hora del turno
    (turno 15:00 -> a las 16:00 desaparece del listado, junto con su estado y su log)."""
    ahora = hora_real()
    viejas = []
    for j in jobs:
        if get_estado(j["id"]) != "reservada":
            continue
        try:
            turno = datetime.datetime.fromisoformat(f"{j['fecha']}T{j['hora_desde']}:00")
        except (ValueError, TypeError):
            continue
        if ahora > _epoch_ar(turno) + LIMPIEZA_CONFIRMADA_SEG:
            viejas.append(j["id"])
    if not viejas:
        return
    with rb._io_lock:
        lista = [x for x in cargar("reservas.json", "reservas") if x.get("id") not in viejas]
        guardar("reservas.json", "reservas", lista)
    with _estados_lock:
        for rid in viejas:
            _estados.pop(rid, None)
    _guardar_estados()
    for rid in viejas:
        try:
            os.remove(os.path.join(AQUI, "logs", f"{rid}.log"))
        except OSError:
            pass
    print(f"[manager] limpieza: {len(viejas)} reserva(s) confirmada(s) ya jugadas, borradas", flush=True)


DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
RECURRENTE_ANTICIPO_DIAS = 3     # con cuántos días de anticipación se carga cada semana


def _generar_recurrentes():
    """Convierte las reservas RECURRENTES (ej: 'todos los martes 22 hs') en reservas concretas
    cuando se acerca el día. Se hace una sola vez por ocurrencia: si ya existe la reserva
    de esa fecha, no la duplica."""
    recs = rb.leer_json("recurrentes.json", {"recurrentes": []}).get("recurrentes", [])
    if not recs:
        return
    hoy = hoy_ar()
    creadas = []
    with rb._io_lock:
        reservas = cargar("reservas.json", "reservas")
        existentes = {r.get("id") for r in reservas}
        for rec in recs:
            if not rec.get("activa", True):
                continue
            try:
                dw = int(rec["dia_semana"])                  # 0 = lunes
                faltan = (dw - hoy.weekday()) % 7
                fecha = (hoy + datetime.timedelta(days=faltan)).isoformat()
                if faltan > RECURRENTE_ANTICIPO_DIAS:
                    continue                                  # todavía falta para ese día
                club = int(rec.get("club_id", 1102))
                hora = rec["hora"]
                hh, mm = map(int, hora.split(":"))
                fin = f"{hh + 1:02d}:00"
                cancha = str(rec.get("cancha", ""))
                rid = (f"{_slug(rec['reservador'])}-{fecha}-{hora.replace(':', '')}"
                       f"-{(cancha or 'lib').lower()[:8]}-c{club}")
                if rid in existentes:
                    continue                                  # ya está cargada
                reservas.append({
                    "id": rid, "club_id": club, "cancha": cancha, "fecha": fecha,
                    "hora_desde": hora, "hora_hasta": fin, "single_double": 2,
                    "reservador": rec["reservador"], "jugadores": list(rec.get("jugadores", [])),
                    "creado_por": rec.get("creado_por", ""), "apertura_iso": None,
                    "horas_antes": 24, "minutos_extra": 1, "reintentos": 40,
                    "espera_entre_reintentos_seg": 0.3, "de_recurrente": rec.get("id", ""),
                })
                existentes.add(rid)
                creadas.append((rid, rec))
            except Exception as e:
                print(f"[recurrente] {rec.get('id')}: {e}", flush=True)
        if creadas:
            guardar("reservas.json", "reservas", reservas)
    for rid, rec in creadas:
        print(f"[recurrente] cargada {rid}", flush=True)
        notificar(rec.get("creado_por", ""), "🔁 Reserva recurrente cargada",
                  f"{DIAS_SEMANA[int(rec['dia_semana'])]} {rec['hora']} · ya quedó programada, "
                  f"se dispara sola a la apertura.")


AVISO_PROBLEMA_ANTES_SEG = 24 * 3600   # avisar si la reserva rota dispara dentro de 24 h


def _marcar_en_reserva(rid, campo, valor):
    """Setea un campo suelto de una reserva en reservas.json (atómico)."""
    with rb._io_lock:
        lista = cargar("reservas.json", "reservas")
        for x in lista:
            if x.get("id") == rid:
                x[campo] = valor
                break
        else:
            return False
        guardar("reservas.json", "reservas", lista)
    return True


def _avisar_reservas_rotas(jobs, reservas_raw):
    """Push PREVENTIVO: si una reserva no va a poder dispararse (falta la clave del
    reservador, jugador sin player_id, datos mal), avisar al que la creó ANTES de que
    llegue la hora, con tiempo de arreglarla. Se avisa UNA sola vez por reserva."""
    ahora = hora_real()
    por_id = {r.get("id"): r for r in reservas_raw}
    for j in jobs:
        rid = j["id"]
        rsv = por_id.get(rid, {})
        if rsv.get("aviso_problema"):          # ya se avisó
            continue
        if get_estado(rid) != "programada":
            continue
        # problema duro (no dispara) o jugadores sin resolver (dispara incompleta)
        motivo = j.get("problema")
        if not motivo and j.get("faltan_jugadores"):
            motivo = f"no encuentro a: {', '.join(str(x) for x in j['faltan_jugadores'])}"
        if not motivo:
            continue
        falta = j["apertura_epoch"] - ahora
        if falta <= 0 or falta > AVISO_PROBLEMA_ANTES_SEG:
            continue                           # todavía lejos (o ya pasó)
        creador = rsv.get("creado_por", "")
        cuando = f"{fmt_fecha(j['fecha'])} {j['hora_desde']}" if j.get("fecha") else "(sin fecha)"
        horas = max(1, int(falta // 3600))
        notificar(creador, "⚠️ Revisá una reserva",
                  f"La de {cuando} NO se va a poder tomar: {motivo}. "
                  f"Se dispara en {horas} h.")
        _marcar_en_reserva(rid, "aviso_problema", True)
        print(f"[aviso] reserva rota {rid}: avisado a {creador or '(sin creador)'}", flush=True)


VIGILANCIA_CADA_SEG = 240        # cada cuánto se revisa si se liberó una cancha (4 min)
VIGILANCIA_CORTE_SEG = 1800      # se deja de vigilar 30 min antes del turno
_ultima_vigilancia = {}          # rid -> epoch del último chequeo


def _cazar_cancelacion(rid):
    """Corre en un hilo: mira si se liberó una cancha para un turno que NO conseguimos y,
    si aparece, la toma al instante. La gente cancela seguido -> es una segunda chance."""
    reservas = cargar("reservas.json", "reservas")
    rsv = next((x for x in reservas if x.get("id") == rid), None)
    if not rsv or not rsv.get("vigilar"):
        return
    jug = cargar("db_jugadores.json", "jugadores")
    res = cargar("db_reservadores.json", "reservadores")
    job = rb.armar_trabajo(rsv, jug, res)
    if job.get("problema"):
        return
    try:
        api = rb._login_y_tokens(job)
        # el turno YA está abierto (por eso vigilamos): abrir form + ver si hay canchas
        jid = api.abrir_formulario(job["fecha"], job["hora_desde"])
        if not jid:
            return
        time.sleep(0.35)
        canchas = api.canchas_disponibles(job["fecha"], job["hora_desde"], job["hora_hasta"],
                                          jid, job["single_double"])
        if not canchas:
            return                       # sigue todo ocupado: se reintenta en el próximo ciclo
        # ¡se liberó algo! tomarla con el flujo completo y seguro
        rb.log(rid, f"VIGILANCIA: se liberó/liberaron {len(canchas)} cancha(s) "
                    f"({', '.join(c['nombre'] for c in canchas[:4])}) -> intento tomarla")
        ok = rb.disparar(job, esperar=False)
        if ok:
            set_estado(rid, "reservada")
            _marcar_en_reserva(rid, "vigilar", False)
            _registrar_resultado(job, "reservada")
            logtxt = _leer_log(rid)
            real = cancha_reservada(rid, logtxt)
            notificar(rsv.get("creado_por", ""), "🎾 ¡Se liberó una cancha!",
                      f"Te tomé {fmt_fecha(job['fecha'])} {job['hora_desde']} · "
                      f"{desc_cancha(real.split('-')[0].lstrip('0') if '-' in real else '')}")
            print(f"[vigilancia] {rid}: CAZADA", flush=True)
    except Exception as e:
        print(f"[vigilancia] {rid}: {str(e)[:120]}", flush=True)


def _revisar_vigilancias(jobs, reservas_raw):
    """Lanza el chequeo de las reservas marcadas para vigilar (turno ya abierto y todavía
    no jugado). Espaciado para no martillar OnDepor."""
    ahora = hora_real()
    por_id = {r.get("id"): r for r in reservas_raw}
    for j in jobs:
        rid = j["id"]
        rsv = por_id.get(rid, {})
        if not rsv.get("vigilar") or j.get("problema"):
            continue
        if get_estado(rid) == "reservada":
            continue
        try:
            turno_epoch = _epoch_ar(datetime.datetime.fromisoformat(
                f"{j['fecha']}T{j['hora_desde']}:00"))
        except (ValueError, TypeError):
            continue
        if ahora < j["apertura_epoch"]:
            continue                     # el turno todavía no abrió: no hay nada que cazar
        if ahora > turno_epoch - VIGILANCIA_CORTE_SEG:
            _marcar_en_reserva(rid, "vigilar", False)   # ya es tarde, dejar de vigilar
            continue
        if ahora - _ultima_vigilancia.get(rid, 0) < VIGILANCIA_CADA_SEG:
            continue                     # todavía no toca revisar esta
        _ultima_vigilancia[rid] = ahora
        threading.Thread(target=_cazar_cancelacion, args=(rid,), daemon=True).start()


def _set_cambio_prog(rid, estado, detalle=""):
    """Actualiza el estado del cambio de nombres programado de una reserva (atómico)."""
    with rb._io_lock:
        lista = cargar("reservas.json", "reservas")
        for x in lista:
            if x.get("id") == rid and x.get("cambio_prog"):
                x["cambio_prog"]["estado"] = estado
                x["cambio_prog"]["detalle"] = detalle
                break
        guardar("reservas.json", "reservas", lista)


def _ejecutar_cambio_prog(rid):
    """Corre en un hilo: hace en OnDepor el cambio de nombres que estaba programado."""
    rsv = next((x for x in cargar("reservas.json", "reservas") if x.get("id") == rid), None)
    cp = (rsv or {}).get("cambio_prog") or {}
    if not rsv or cp.get("estado") != "en_curso":
        return
    st, msg = _hacer_cambio_jugadores(rsv, cp.get("jugadores", []), cp.get("reservador", ""))
    estado = {"success": "hecho", "revertida": "revertido", "perdida": "perdido"}.get(st, "fallado")
    _set_cambio_prog(rid, estado, str(msg)[:200])
    print(f"[manager] cambio programado {rid}: {estado} ({str(msg)[:120]})", flush=True)


def _revisar_cambios_prog(reservas=None):
    """Dispara los cambios de nombres programados a su hora. Cada cambio puede traer su
    propia hora (`ej_epoch`, la usa el cronograma: corre apenas se liberan los carnets
    de los que entran); si no la trae, va al viejo default de turno - 10 min.
    'reservas' = lista ya cargada por el manager (evita releer el JSON en cada ciclo)."""
    ahora = hora_real()
    for rsv in (cargar("reservas.json", "reservas") if reservas is None else reservas):
        cp = rsv.get("cambio_prog")
        if not cp or cp.get("estado") != "pendiente":
            continue
        rid = rsv.get("id")
        if get_estado(rid) != "reservada":
            continue                       # solo tiene sentido sobre una reserva confirmada
        try:
            turno_epoch = _epoch_ar(datetime.datetime.fromisoformat(
                f"{rsv['fecha']}T{rsv['hora_desde']}:00"))
        except (ValueError, TypeError):
            continue
        if ahora < (cp.get("ej_epoch") or turno_epoch - CAMBIO_PROG_ANTES_SEG):
            continue                       # todavía no es hora
        if ahora >= turno_epoch:           # el turno ya empezó y no corrió (¿app caída?): no cambiar
            _set_cambio_prog(rid, "vencido", "no se ejecutó a tiempo (el turno ya había empezado)")
            continue
        _set_cambio_prog(rid, "en_curso")  # marcar ANTES de largar el hilo (evita doble disparo)
        threading.Thread(target=_ejecutar_cambio_prog, args=(rid,), daemon=True).start()


_manager_vivo = 0.0   # último latido del reloj interno (lo mira /salud)


def _manager():
    sincronizar_reloj()   # sincroniza el reloj una vez al arrancar (después se refresca solo)
    while True:
        try:
            # los 3 JSON se leen UNA sola vez por ciclo y se reparten (antes cada paso
            # los releía por su cuenta)
            datos = rb.cargar_todo()
            jobs = rb.buscar_trabajos("all", datos=datos)
            for job in jobs:
                rid = job["id"]
                if job.get("problema"):
                    continue
                est = get_estado(rid)
                if est in ("en_curso", "reservada", "fallada", "vencida"):
                    continue
                falta = job["apertura_epoch"] - hora_real()
                if falta > VENTANA_PRE_SEG:        # todavía lejos
                    continue
                if falta < -VENTANA_VENCIDA_SEG:   # ya pasó hace rato: se venció
                    set_estado(rid, "vencida")
                    continue
                # dentro de la ventana: disparar (disparar() espera el segundo exacto)
                set_estado(rid, "en_curso")
                threading.Thread(target=_disparar_job, args=(job,), daemon=True).start()
            _revisar_cambios_prog(datos[2])     # cambios de nombres agendados (turno - 10 min)
            _avisar_reservas_rotas(jobs, datos[2])   # push preventivo si algo no va a andar
            _revisar_vigilancias(jobs, datos[2])     # cazar canchas que se liberan
            _generar_recurrentes()                   # cargar las reservas recurrentes de la semana
            _limpiar_confirmadas_viejas(jobs)   # va DESPUÉS del loop: no borrar bajo sus pies
            _backup_diario()                    # copia de los JSON (una vez por día)
            global _manager_vivo
            _manager_vivo = time.time()         # latido para /salud
        except Exception as e:
            print("[manager] error:", e, flush=True)
        time.sleep(MANAGER_CICLO_SEG)


# ----------------------------------------------------------------------
#  Auth
# ----------------------------------------------------------------------
def login_requerido(f):
    @wraps(f)
    def wrap(*a, **k):
        if not session.get("ok"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrap


def permiso_requerido(permiso):
    """Protege una ruta con un permiso concreto del rol (ver ROLES)."""
    def deco(f):
        @wraps(f)
        def wrap(*a, **k):
            if not session.get("ok"):
                return redirect(url_for("login"))
            if not puede(permiso):
                flash("No tenés permiso para esa sección.")
                return redirect(url_for("reservas"))
            return f(*a, **k)
        return wrap
    return deco


def admin_requerido(f):
    """Atajo histórico: equivale a exigir el permiso de administrar usuarios."""
    return permiso_requerido("usuarios")(f)


# ----------------------------------------------------------------------
#  Plantillas  (el CSS vive en static/estilo.css, servido por Flask)
# ----------------------------------------------------------------------
HOJA = ('<link rel="stylesheet" href="{{ url_for(\'static\', filename=\'estilo.css\') }}?v=%s">'
        % _css_ver())  # ?v = mtime del CSS: el caché se rompe solo al cambiar el archivo

# PWA: manifest + iconos + color de barra (Android) y registro del service worker
PWA_HEAD = """
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#0e2038">
<link rel="icon" type="image/png" href="/static/icon-192.png">
<link rel="apple-touch-icon" href="/static/icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Reservame">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">"""
SW_REG = """
<script>
if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js');}

// el degradado del menú solo si de verdad no entra (si no, difumina el último ítem al pedo)
function marcarNav(){
  var n = document.querySelector('header nav');
  if(n) n.classList.toggle('hay-mas', n.scrollWidth > n.clientWidth + 4);
}
window.addEventListener('resize', marcarNav);
document.addEventListener('DOMContentLoaded', marcarNav);

// --- feedback en acciones lentas: spinner + evita el doble click ---
document.addEventListener('submit', function(ev){
  var f = ev.target;
  if(!f || f.dataset.nospin) return;
  var b = f.querySelector('button[type=submit], button:not([type])');
  if(b && !b.disabled){
    var txt = b.dataset.wait || 'Procesando…';
    setTimeout(function(){ b.disabled=true; b.classList.add('cargando'); b.innerHTML='<span class="spin"></span> '+txt; }, 0);
  }
}, true);

// --- notificaciones push: se suscribe al usuario logueado (una vez que aceptó) ---
function b64ToU8(s){
  var pad='='.repeat((4-s.length%4)%4), b=(s+pad).replace(/-/g,'+').replace(/_/g,'/');
  var raw=atob(b), arr=new Uint8Array(raw.length);
  for(var i=0;i<raw.length;i++) arr[i]=raw.charCodeAt(i);
  return arr;
}
async function activarAvisos(silencioso){
  try{
    if(!('serviceWorker' in navigator) || !('PushManager' in window)) return false;
    var r = await fetch('/api/push/clave'); var d = await r.json();
    if(!d.activo || !d.clave) return false;
    if(Notification.permission === 'denied') return false;
    if(Notification.permission === 'default'){
      if(silencioso) return false;                       // no molestar sin que lo pidan
      if(await Notification.requestPermission() !== 'granted') return false;
    }
    var reg = await navigator.serviceWorker.ready;
    var sub = await reg.pushManager.getSubscription();
    if(!sub) sub = await reg.pushManager.subscribe({userVisibleOnly:true, applicationServerKey:b64ToU8(d.clave)});
    await fetch('/api/push/suscribir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(sub)});
    return true;
  }catch(e){ console.log('push:', e); return false; }
}
// si ya dio permiso antes, re-registra la suscripción en cada visita (por si cambió)
document.addEventListener('DOMContentLoaded', function(){
  if(window.Notification && Notification.permission === 'granted') activarAvisos(true);
});
</script>"""

BASE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reservame ésta! - {{titulo}}</title>""" + HOJA + PWA_HEAD + """</head><body>
<header>
  <b><svg width="20" height="20" viewBox="0 0 100 100" style="vertical-align:-3px;display:inline-block"><circle cx="50" cy="50" r="46" fill="#c7e400" stroke="#9bbb00" stroke-width="2"/><path d="M28,12 Q56,50 28,88" fill="none" stroke="#fff" stroke-width="5"/><path d="M72,12 Q44,50 72,88" fill="none" stroke="#fff" stroke-width="5"/></svg> Reservame ésta!</b>
  <nav aria-label="Secciones">
    <a href="{{url_for('reservas')}}" class="{{'activo' if tab=='reservas' else ''}}" {{'aria-current=page' if tab=='reservas'}}>Reservas</a>
    <a href="{{url_for('jugadores')}}" class="{{'activo' if tab=='jugadores' else ''}}" {{'aria-current=page' if tab=='jugadores'}}>Jugadores</a>
    <a href="{{url_for('reservadores')}}" class="{{'activo' if tab=='reservadores' else ''}}" {{'aria-current=page' if tab=='reservadores'}}>Reservadores</a>
    <a href="{{url_for('encuesta_panel')}}" class="{{'activo' if tab=='encuesta' else ''}}" {{'aria-current=page' if tab=='encuesta'}}>Encuesta</a>
    <a href="{{url_for('recurrentes')}}" class="{{'activo' if tab=='recurrentes' else ''}}" {{'aria-current=page' if tab=='recurrentes'}}>Recurrentes</a>
    <a href="{{url_for('historial')}}" class="{{'activo' if tab=='historial' else ''}}" {{'aria-current=page' if tab=='historial'}}>Historial</a>
    {% if session.get('admin') %}<a href="{{url_for('usuarios')}}" class="{{'activo' if tab=='usuarios' else ''}}" {{'aria-current=page' if tab=='usuarios'}}>Usuarios</a>{% endif %}
  </nav>
  <span class="sp"></span>
  <span class="quien">{{ ic('user')|safe }} {{session.get('usuario','')}}</span>
  <a class="salir" href="{{url_for('logout')}}">{{ ic('logout')|safe }} Salir</a>
</header>
<div class="wrap">
  {% with m = get_flashed_messages() %}{% if m %}<div class="msg">{{m|join(' · ')}}</div>{% endif %}{% endwith %}
  {{cuerpo|safe}}
</div>""" + SW_REG + """</body></html>"""


def pagina(titulo, tab, cuerpo):
    return render_template_string(BASE, titulo=titulo, tab=tab, cuerpo=cuerpo, ic=icon)


# ----------------------------------------------------------------------
#  LOGIN
# ----------------------------------------------------------------------
LOGIN_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Entrar</title>
""" + HOJA + PWA_HEAD + """</head><body>
<div class="login-fondo">
  <div class="login-caja">
    <div class="login-marca"><svg width="30" height="30" viewBox="0 0 100 100" style="vertical-align:-6px;display:inline-block"><circle cx="50" cy="50" r="46" fill="#c7e400" stroke="#9bbb00" stroke-width="2"/><path d="M28,12 Q56,50 28,88" fill="none" stroke="#fff" stroke-width="5"/><path d="M72,12 Q44,50 72,88" fill="none" stroke="#fff" stroke-width="5"/></svg> Reservame ésta!</div>
    <div class="card">
      <h2>{{ ic('user')|safe }} Entrar al panel</h2>
      {% with m = get_flashed_messages() %}{% if m %}<div class="msg warn">{{m|join(' ')}}</div>{% endif %}{% endwith %}
      <form method="post">
        <label>Usuario</label>
        <input name="usuario" autofocus autocapitalize="off">
        <label>Contraseña</label>
        <input type="password" name="password">
        <div style="height:16px"></div>
        <button style="width:100%">{{ ic('login')|safe }} Entrar</button>
      </form>
    </div>
  </div>
</div>""" + SW_REG + """</body></html>"""


@app.route("/salud")
def salud():
    """Chequeo de vida (sin login, no expone datos). Sirve para monitoreo externo:
    avisa si el RELOJ INTERNO dejó de latir, que es el que dispara las reservas."""
    edad = time.time() - _manager_vivo if _manager_vivo else None
    reloj_ok = edad is not None and edad < MANAGER_CICLO_SEG * 5
    try:
        pendientes = sum(1 for r in cargar("reservas.json", "reservas")
                         if get_estado(r.get("id")) in ("programada", "en_curso"))
    except Exception:
        pendientes = None
    estado = {"ok": bool(reloj_ok), "reloj_interno": "vivo" if reloj_ok else "CAÍDO",
              "ultimo_latido_seg": round(edad, 1) if edad is not None else None,
              "reservas_pendientes": pendientes, "push": PUSH_ACTIVO,
              "hora_ar": ahora_ar().strftime("%Y-%m-%d %H:%M:%S")}
    return jsonify(estado), (200 if reloj_ok else 503)


@app.route("/api/push/clave")
@login_requerido
def api_push_clave():
    """Clave pública VAPID + si el usuario YA tiene avisos en algún dispositivo (para no
    insistir con el cartel en la compu cuando ya los activó en el celular)."""
    return jsonify({"activo": PUSH_ACTIVO, "clave": _VAPID.get("public_key", ""),
                    "ya_tiene": len(_subs_de(session.get("usuario", ""))) > 0})


@app.route("/api/push/suscribir", methods=["POST"])
@login_requerido
def api_push_suscribir():
    """Guarda la suscripción del navegador ATADA al usuario logueado (así el aviso de
    una reserva le llega SOLO a quien la programó)."""
    sub = request.get_json(silent=True) or {}
    if not sub.get("endpoint"):
        return jsonify({"ok": False, "msg": "suscripción inválida"}), 400
    usuario = session.get("usuario", "")
    with rb._io_lock:
        data = rb.leer_json("push_subs.json", {"subs": {}})
        subs = data.setdefault("subs", {})
        mias = [x for x in subs.get(usuario, []) if x.get("endpoint") != sub["endpoint"]]
        mias.append(sub)
        subs[usuario] = mias[-5:]          # hasta 5 dispositivos por usuario
        rb.escribir_json("push_subs.json", data)
    return jsonify({"ok": True})


@app.route("/api/push/probar", methods=["POST"])
@login_requerido
def api_push_probar():
    n = notificar(session.get("usuario", ""), "🎾 Reservame ésta!",
                  "Las notificaciones están funcionando.")
    return jsonify({"ok": n > 0, "enviados": n})


@app.route("/sw.js")
def sw_js():
    # el service worker se sirve desde la RAÍZ para que su alcance cubra toda la app
    resp = app.send_static_file("sw.js")
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"   # que las actualizaciones lleguen rápido
    return resp


INSTALAR_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Instalar Reservame ésta!</title>
""" + HOJA + PWA_HEAD + """</head><body>
<div class="login-fondo">
  <div class="login-caja">
    <div class="login-marca"><svg width="30" height="30" viewBox="0 0 100 100" style="vertical-align:-6px;display:inline-block"><circle cx="50" cy="50" r="46" fill="#c7e400" stroke="#9bbb00" stroke-width="2"/><path d="M28,12 Q56,50 28,88" fill="none" stroke="#fff" stroke-width="5"/><path d="M72,12 Q44,50 72,88" fill="none" stroke="#fff" stroke-width="5"/></svg> Reservame ésta!</div>

    <div class="card" id="paso-android" style="display:none">
      <h2>Instalar la app</h2>
      <p><small class="hint">Se agrega a tu pantalla de inicio y se abre como una app:
        entra directo, a pantalla completa, y te llegan los avisos de tus reservas.</small></p>
      <div style="height:10px"></div>
      <button style="width:100%" onclick="instalar(this)">Instalar aplicación</button>
    </div>

    <div class="card" id="paso-ios" style="display:none">
      <h2>Instalar en tu iPhone</h2>
      <p><small class="hint">Son 10 segundos. Tiene que ser desde <b>Safari</b>:</small></p>
      <ol style="margin:10px 0 0 -8px;line-height:1.9">
        <li>Tocá <b>Compartir</b> <span style="font-size:1.1em">&#x2934;</span> (abajo en el centro)</li>
        <li>Bajá y tocá <b>“Agregar a inicio”</b></li>
        <li>Tocá <b>Agregar</b> arriba a la derecha</li>
        <li>Abrí la app desde el ícono nuevo 🎾</li>
      </ol>
      <div class="msg warn" style="margin-top:12px">Si estás en Chrome, abrí este mismo link en Safari.</div>
    </div>

    <div class="card" id="paso-listo" style="display:none">
      <h2>¡Ya está instalada!</h2>
      <p><small class="hint">Estás usando la app instalada. Entrá con tu usuario y contraseña.</small></p>
      <div style="height:10px"></div>
      <a class="btn" style="width:100%;justify-content:center" href="/login">Entrar</a>
    </div>

    <div class="card" id="paso-escritorio" style="display:none">
      <h2>Abrilo en el celular</h2>
      <p><small class="hint">La app se instala desde el teléfono. Abrí este link en tu celular:</small></p>
      <div class="msg" style="word-break:break-all"><b id="urlaqui"></b></div>
      <a class="btn sec" style="width:100%;justify-content:center;margin-top:8px" href="/login">Seguir en el navegador</a>
    </div>

    <div style="text-align:center;margin-top:14px">
      <a href="/login" style="color:rgba(255,255,255,.85);font-size:.9rem">Entrar sin instalar</a>
    </div>
  </div>
</div>
<script>
var promptDiferido = null;
window.addEventListener('beforeinstallprompt', function(e){
  e.preventDefault(); promptDiferido = e;
  mostrar('paso-android');
});
function mostrar(id){
  ['paso-android','paso-ios','paso-listo','paso-escritorio'].forEach(function(x){
    var el=document.getElementById(x); if(el) el.style.display = (x===id?'block':'none');
  });
}
async function instalar(btn){
  if(!promptDiferido){ alert('Usá el menú del navegador (⋮) → "Instalar aplicación".'); return; }
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Instalando…';
  promptDiferido.prompt();
  var r = await promptDiferido.userChoice;
  promptDiferido = null;
  if(r.outcome === 'accepted'){ mostrar('paso-listo'); }
  else { btn.disabled=false; btn.textContent='Instalar aplicación'; }
}
(function(){
  var ua = navigator.userAgent || '';
  var instalada = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  var esIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  var esMovil = esIOS || /Android/i.test(ua);
  document.getElementById('urlaqui').textContent = location.host + '/instalar';
  if(instalada) mostrar('paso-listo');
  else if(esIOS) mostrar('paso-ios');
  else if(!esMovil) mostrar('paso-escritorio');
  // en Android esperamos el evento beforeinstallprompt; si no llega en 1,5 s, guiamos a mano
  else setTimeout(function(){ if(!promptDiferido && !document.querySelector('#paso-android[style*="block"]')) mostrar('paso-android'); }, 1500);
})();
</script>
</body></html>"""


@app.route("/instalar")
def instalar():
    """Página pública para compartir por WhatsApp: instala la app en 1 toque (Android)
    o guía paso a paso (iPhone). No pide login: primero se instala, después se entra."""
    return render_template_string(INSTALAR_HTML)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # el cupo lo gastan los intentos FALLIDOS: entrar bien no descuenta (si no, varios
        # usuarios detrás de la misma IP/WiFi se bloquean entre ellos)
        clave_rl = "login:" + (request.remote_addr or "?")
        if not rate_ok(clave_rl, maximo=10, ventana_seg=300, contar=False):
            flash("Demasiados intentos fallidos. Esperá unos minutos e intentá de nuevo.")
            return render_template_string(LOGIN_HTML, ic=icon)
        u = validar_usuario(request.form.get("usuario", ""), request.form.get("password", ""))
        if not u:
            rate_ok(clave_rl, maximo=10, ventana_seg=300)     # registra el intento fallido
        if u:
            session.permanent = True
            session["ok"] = True
            session["usuario"] = u.get("nombre") or u.get("usuario")
            session["rol"] = rol_de(u)
            session["admin"] = puede_rol(session["rol"], "usuarios")   # compat: "admin" = panel usuarios
            return redirect(url_for("reservas"))
        flash("Usuario o contraseña incorrectos.")
    return render_template_string(LOGIN_HTML, ic=icon)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ----------------------------------------------------------------------
#  USUARIOS del panel
# ----------------------------------------------------------------------
@app.route("/usuarios")
@admin_requerido
def usuarios():
    us = cargar_usuarios()
    # ¿estamos editando uno? (?editar=<idx>)
    edit_idx = request.args.get("editar")
    ed = None
    if edit_idx is not None and edit_idx.isdigit() and int(edit_idx) < len(us):
        ed = us[int(edit_idx)]

    filas = ""
    # orden alfabético solo para MOSTRAR; 'i' sigue siendo el índice real (editar/borrar lo usan)
    for i, u in sorted(enumerate(us), key=lambda t: str(t[1].get("nombre") or t[1].get("usuario") or "").lower()):
        yo = " (vos)" if (u.get("nombre") or u.get("usuario")) == session.get("usuario") else ""
        rol = rol_de(u)
        color = {"admin": "programada", "operador": "en_curso"}.get(rol, "vencida")
        chip = f'<span class="pill {color}">{ROLES[rol]["etiqueta"]}</span>'
        filas += f"""<li><div class="sp"><b>{esc(u.get('nombre') or u.get('usuario'))}</b>{yo} {chip}
          <br><small class="hint">entra como: {esc(u.get('usuario'))}</small></div>
          <div class="acc">
          <a class="btn sec btn-ico" aria-label="Editar" title="Editar" href="{url_for('usuarios')}?editar={i}">{icon('edit')}</a>
          <form method="post" action="{url_for('usuario_borrar', idx=i)}"
                onsubmit="return confirm('¿Borrar este usuario del panel?')">
            <button class="danger btn-ico" aria-label="Borrar usuario" title="Borrar usuario">{icon('trash')}</button></form>
          </div></li>"""
    if not filas:
        filas = f'<li class="vacio">{icon("users")}<div>No hay usuarios.</div></li>'

    titulo_form = f"Editar usuario: {esc(ed.get('usuario'))}" if ed else "Agregar usuario del panel"
    v_nombre = esc(ed.get("nombre", "")) if ed else ""
    v_usuario = esc(ed.get("usuario", "")) if ed else ""
    v_orig = esc(ed.get("usuario", "")) if ed else ""
    rol_ed = rol_de(ed) if ed else ROL_POR_DEFECTO
    opts_rol = "".join(
        f'<option value="{k}"{" selected" if k == rol_ed else ""}>{v["etiqueta"]}</option>'
        for k, v in ROLES.items())
    ayuda_roles = "".join(
        f'<div style="margin-top:4px"><b>{v["etiqueta"]}:</b> {v["ayuda"]}</div>'
        for v in ROLES.values())
    # tabla EDITABLE de permisos: tildá lo que puede hacer cada tipo de usuario
    cabecera = "".join(f"<th>{v['etiqueta']}</th>" for v in ROLES.values())
    cuerpo_tabla = ""
    for perm, desc in PERMISOS.items():
        celdas = ""
        for k, v in ROLES.items():
            tildado = " checked" if perm in v["permisos"] else ""
            fijo = (k == "admin" and perm == "usuarios")     # candado: no dejarse afuera
            # OJO: sin backslashes dentro del f-string (rompe en el Python 3.10 del server)
            extra = ' disabled title="El administrador siempre puede entrar acá"' if fijo else ""
            oculto = f'<input type="hidden" name="p:{k}:{perm}" value="on">' if fijo else ""
            celdas += (f'<td><input type="checkbox" name="p:{k}:{perm}"{tildado}{extra}>'
                       f'{oculto}</td>')
        cuerpo_tabla += f"<tr><td class='que'>{desc}</td>{celdas}</tr>"
    cuerpo_tabla += ("<tr><td class='que'>Crear y manejar sus propias reservas</td>"
                     + "".join(f'<td class="si" title="Todos pueden">{icon("check")}</td>' for _ in ROLES)
                     + "</tr>")
    tabla_permisos = f"""
    <div class="card">
      <h2>{icon('check-circle')} Qué puede hacer cada tipo</h2>
      <p><small class="hint">Tildá lo que querés permitir. Los cambios valen para todos los
        usuarios de ese tipo, al instante.</small></p>
      <form method="post" action="{url_for('permisos_guardar')}">
        <div style="overflow-x:auto">
          <table class="permisos"><thead><tr><th>Permiso</th>{cabecera}</tr></thead>
          <tbody>{cuerpo_tabla}</tbody></table>
        </div>
        <div style="height:14px"></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          <button>{icon('save')} Guardar permisos</button>
          <button class="sec" formaction="{url_for('permisos_restaurar')}"
                  onclick="return confirm('¿Volver a los permisos originales?')">Restaurar</button>
        </div>
      </form>
    </div>"""
    ph_clave = "(dejá vacío para no cambiarla)" if ed else "contraseña"
    cancelar = f'<a class="btn sec" style="text-decoration:none" href="{url_for("usuarios")}">Cancelar edición</a>' if ed else ""

    cuerpo = f"""
    <div class="card">
      <h2>{icon('edit') if ed else icon('plus')} {titulo_form}</h2>
      <form method="post" action="{url_for('usuario_guardar')}">
        <input type="hidden" name="orig" value="{v_orig}">
        <div class="row">
          <div><label>Nombre (para mostrar)</label><input name="nombre" value="{v_nombre}"></div>
          <div><label>Usuario (para entrar)</label><input name="usuario" value="{v_usuario}" autocapitalize="off"></div>
        </div>
        <label>Contraseña</label><input name="clave" type="password" placeholder="{ph_clave}">
        <label>Tipo de usuario</label>
        <select name="rol">{opts_rol}</select>
        <div class="nota" style="margin-top:8px;font-size:.85rem">{ayuda_roles}</div>
        <div style="height:14px"></div>
        <button>{icon('save')} {'Guardar cambios' if ed else 'Agregar usuario'}</button> {cancelar}
      </form>
    </div>
    {tabla_permisos}
    <div class="card">
      <h2>{icon('users')} Usuarios del panel</h2>
      <p><small class="hint">Cada uno entra con su propio usuario y contraseña. Al crear una reserva, queda registrado quién la hizo. Solo los <b>administradores</b> ven esta pestaña.</small></p>
      <ul class="lista compacta">{filas}</ul>
    </div>"""
    return pagina("Usuarios", "usuarios", cuerpo)


@app.route("/usuarios/guardar", methods=["POST"])
@admin_requerido
def usuario_guardar():
    f = request.form
    nombre = limpiar_nombre(f.get("nombre", ""))
    usuario = limpiar_nombre(f.get("usuario", ""))
    clave = f.get("clave", "").strip()
    rol = f.get("rol", ROL_POR_DEFECTO).strip().lower()
    if rol not in ROLES:
        rol = ROL_POR_DEFECTO
    orig = f.get("orig", "").strip().lower()
    if not usuario:
        flash("El usuario es obligatorio.")
        return redirect(url_for("usuarios"))

    with rb._io_lock:
        lista = cargar_usuarios()
        idx = None
        for i, u in enumerate(lista):
            uu = str(u.get("usuario", "")).strip().lower()
            if (orig and uu == orig) or (not orig and uu == usuario.lower()):
                idx = i
                break

        # 'admin' se sigue guardando por compatibilidad con lo que ya estaba escrito
        nuevo = {"usuario": usuario, "nombre": nombre or usuario,
                 "rol": rol, "admin": puede_rol(rol, "usuarios")}
        if idx is not None:
            nuevo["clave"] = clave if clave else lista[idx].get("clave", "")  # sin clave nueva = conservar
            lista[idx] = nuevo
        else:
            if not clave:
                flash("Para un usuario nuevo la contraseña es obligatoria.")
                return redirect(url_for("usuarios"))
            nuevo["clave"] = clave
            lista.append(nuevo)

        if not any(rol_de(u) == "admin" for u in lista):   # nunca quedarse sin admin
            lista[0]["rol"], lista[0]["admin"] = "admin", True
            flash("Ojo: dejé al menos un administrador para no quedar sin acceso.")
        guardar_usuarios(lista)
    flash(f"Usuario guardado: {usuario}")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/permisos", methods=["POST"])
@permiso_requerido("usuarios")
def permisos_guardar():
    """Guarda qué puede hacer cada tipo de usuario (roles.json)."""
    nuevos = {}
    for rol in ROLES_BASE:
        nuevos[rol] = sorted(p for p in PERMISOS if request.form.get(f"p:{rol}:{p}"))
    nuevos["admin"] = sorted(set(nuevos["admin"]) | {"usuarios"})   # nunca quedarse afuera
    with rb._io_lock:
        rb.escribir_json("roles.json", {"roles": nuevos})
    cargar_roles()
    # refrescar la sesión propia por si me saqué (o me di) permisos a mí mismo
    session["admin"] = puede("usuarios")
    flash("Permisos actualizados.")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/permisos/restaurar", methods=["POST"])
@permiso_requerido("usuarios")
def permisos_restaurar():
    with rb._io_lock:
        rb.escribir_json("roles.json", {"roles": {k: sorted(v["permisos"])
                                                  for k, v in ROLES_BASE.items()}})
    cargar_roles()
    session["admin"] = puede("usuarios")
    flash("Permisos restaurados a los originales.")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/borrar/<int:idx>", methods=["POST"])
@admin_requerido
def usuario_borrar(idx):
    with rb._io_lock:
        lista = cargar_usuarios()
        if len(lista) <= 1:
            flash("No podés borrar el único usuario (te quedarías afuera del panel).")
            return redirect(url_for("usuarios"))
        if 0 <= idx < len(lista):
            if rol_de(lista[idx]) == "admin" and sum(1 for u in lista if rol_de(u) == "admin") <= 1:
                flash("No podés borrar al único administrador. Hacé admin a otro primero.")
                return redirect(url_for("usuarios"))
            quitado = lista.pop(idx)
            guardar_usuarios(lista)
            flash(f"Usuario borrado: {quitado.get('usuario')}")
    return redirect(url_for("usuarios"))


@app.route("/")
@login_requerido
def home():
    return redirect(url_for("reservas"))


# ----------------------------------------------------------------------
#  RESERVAS
# ----------------------------------------------------------------------
def _conflictos_y_slots(jobs, reservadores, jugadores, foco_id):
    """Calcula, para avisar conflictos en el formulario:
      ocupados = {club: {fecha: [player_ids que ya juegan ese día en ese club]}}
      slots    = {"club|fecha|hora|cancha", ...} (para avisar turno repetido)
    Día (1102) y noche (504) son clubes distintos: se puede jugar en los dos el mismo
    día, por eso el conflicto se mira POR CLUB. Se ignora la reserva en edición."""
    res_pid = {}
    for r in reservadores:
        pid = r.get("player_id")
        if not pid:
            m = next((jj for jj in jugadores
                      if str(jj.get("nombre", "")).lower() == str(r.get("nombre", "")).lower()), None)
            pid = m.get("player_id") if m else None
        if pid:
            res_pid[r.get("nombre")] = pid

    ocupados, slots = {}, set()
    for j in jobs:
        if j["id"] == foco_id:        # no chocar contra la que estoy editando/cambiando
            continue
        cid = str(j.get("club_id", 1102))
        ids = set(j.get("player_ids", []))
        rp = res_pid.get(j["reservador"])
        if rp:
            ids.add(rp)
        ocupados.setdefault(cid, {}).setdefault(j["fecha"], set()).update(ids)
        slots.add(cid + "|" + j["fecha"] + "|" + j["hora_desde"] + "|" + str(j.get("cancha", "")))
    return ({c: {f: sorted(v) for f, v in d.items()} for c, d in ocupados.items()},
            slots, res_pid)


def _fila_reserva(j, est, cp, creador, nom_por_id, vigilando=False):
    """HTML de UNA fila del listado de reservas (título, chip de estado, metadatos,
    notas y botones según estado/permisos). 'cp' = cambio de nombres programado."""
    logtxt = _leer_log(j["id"]) if est in ("fallada", "reservada", "en_curso") else ""
    detalle = resumen_log(j["id"], logtxt) if logtxt else ""
    acomp = [x["nombre"] for x in j.get("jugadores_full", [])] + [str(f) for f in j.get("faltan_jugadores", [])]
    acomp_txt = ", ".join(acomp) if acomp else "sin acompañantes"
    # OJO: no llamar "puede" a una variable local, tapa la función puede() de permisos
    puede_ondepor = puede("cambiar_ondepor")      # cambiar/programar jugadores en OnDepor
    es_suya = puede("todas_reservas") or (creador and creador == session.get("usuario"))

    # chip de estado (ícono + etiqueta amable)
    pic, plab = {"programada": ("clock", "Programada"), "reservada": ("check-circle", "Reservada"),
                 "fallada": ("x-circle", "Falló"), "en_curso": ("refresh", "En curso"),
                 "vencida": ("clock", "Vencida")}.get(est, ("clock", est))
    pill = f'<span class="pill {est}">{icon(pic)} {plab}</span>'
    sede_ic, sede_lab = ("moon", "Noche") if j.get("club_id") == 504 else ("sun", "Día")

    # cancha a mostrar: si ya está reservada, la que ENTRÓ (avisando si no es la pedida)
    cancha_disp = desc_cancha(j["cancha"])
    cancha_warn = ""
    if est == "reservada":
        real = cancha_reservada(j["id"], logtxt)
        if real:
            real_num = real.split("-")[0].lstrip("0") if "-" in real else real
            pedida = str(j.get("cancha", "")).strip()
            cancha_disp = desc_cancha(real_num)
            if pedida.isdigit() and real_num and real_num != pedida:
                cancha_warn = f' <span class="nota warn">{icon("alert")} no era la {esc(pedida)}</span>'
    cancha_disp = (cancha_disp[:1].upper() + cancha_disp[1:]) if cancha_disp else cancha_disp

    # notas (una por línea): disparo / problema / detalle del log / cambio programado
    notas = []
    if j.get("problema"):
        notas.append(f'<div class="nota err">{icon("alert")} No se va a disparar: {esc(j["problema"])}</div>')
    elif est == "reservada":
        if creador:
            notas.append(f'<small class="hint">creada por {esc(creador)}</small>')
    else:
        # data-t = epoch de la apertura -> el JS muestra "faltan 3 h 20 m" y lo actualiza solo
        notas.append(f'<small class="hint">{icon("clock")} se dispara {j["apertura_dt"].strftime("%d/%m %H:%M")}'
                     f'<span class="cuenta" data-t="{int(j.get("apertura_epoch") or 0)}"></span>'
                     f'{(" · por " + str(esc(creador))) if creador else ""}</small>')
    if vigilando:
        notas.append(f'<div class="nota info">{icon("eye")} <b>Vigilando cancelaciones</b> · '
                     f'si se libera una cancha te la tomo y te aviso</div>')
    if detalle and est == "fallada":
        notas.append(f'<div class="nota err">{esc(detalle)}</div>')
    elif detalle and est == "reservada":
        notas.append(f'<div class="nota ok">{icon("check")} {esc(detalle)}</div>')
    if cp:
        nombres_cp = ", ".join(str(esc(nom_por_id.get(str(p), str(p)))) for p in cp.get("jugadores", []))
        quien = esc(cp.get("reservador", ""))
        hh = esc(cp.get("hora_ej", ""))
        estcp = cp.get("estado", "")
        if estcp == "pendiente":
            extra = ""
            if puede_ondepor:
                extra = (f' · <a href="{url_for("reservas")}?programar={j["id"]}">editar</a>'
                         f' <form method="post" style="display:inline" action="{url_for("cambio_prog_borrar", rid=j["id"])}"'
                         f' onsubmit="return confirm(\'¿Anular el cambio de nombres programado?\')">'
                         f'<button class="sec" style="min-height:auto;padding:3px 10px;font-size:.8rem">anular</button></form>')
            notas.append(f'<div class="nota info">{icon("refresh")} Cambio de nombres a las <b>{hh}</b> → {quien}, {nombres_cp}{extra}</div>')
        elif estcp == "en_curso":
            notas.append(f'<div class="nota warn">{icon("refresh")} Ejecutando el cambio de nombres…</div>')
        elif estcp == "hecho":
            notas.append(f'<div class="nota ok">{icon("check")} Nombres cambiados a las {hh}</div>')
        elif estcp == "revertido":
            notas.append(f'<div class="nota warn">{icon("alert")} Cambio revertido (quedaron los originales): {esc(cp.get("detalle", ""))}</div>')
        elif estcp == "vencido":
            notas.append(f'<div class="nota">{icon("x")} Cambio no ejecutado: {esc(cp.get("detalle", ""))}</div>')
        else:
            notas.append(f'<div class="nota err">{icon("alert")} Cambio falló: {esc(cp.get("detalle", ""))}</div>')
    notas_html = "".join(notas)

    # botones (según estado y permisos)
    botones = ""
    if es_suya:
        if est == "reservada":
            cambiar_btn = (f'<a class="btn dark" href="{url_for("reservas")}?cambiar={j["id"]}" title="Cambiar los jugadores en OnDepor (cancela y vuelve a tomar la misma cancha)">{icon("users")} Cambiar</a>'
                           if puede_ondepor else "")
            prog_btn = (f'<a class="btn sec" href="{url_for("reservas")}?programar={j["id"]}" title="Programar el cambio para 10 min antes del turno">{icon("clock")} Programar cambio</a>'
                        if puede_ondepor and not cp else "")
            borrar = (f'<form method="post" action="{url_for("reserva_borrar", rid=j["id"])}"'
                      f' onsubmit="return confirm(\'Esto CANCELA el turno en OnDepor y lo saca de la lista. ¿Seguro?\')">'
                      f'<input type="hidden" name="cancelar_ml" value="1">'
                      f'<button class="danger" title="Cancelar el turno en OnDepor">{icon("trash")} Cancelar turno</button></form>')
            botones = cambiar_btn + prog_btn + borrar
        else:
            editar_btn = f'<a class="btn sec" href="{url_for("reservas")}?editar={j["id"]}">{icon("edit")} Editar</a>'
            # cazar cancelaciones: solo tiene sentido si el turno YA abrió y no entró
            vig_btn = ""
            if est in ("fallada", "vencida") or vigilando:
                vig_btn = (f'<form method="post" action="{url_for("reserva_vigilar", rid=j["id"])}" style="display:inline">'
                           f'<button class="{"dark" if vigilando else "sec"}" '
                           f'title="Si alguien cancela, te tomo la cancha y te aviso">'
                           f'{icon("eye")} {"Dejar de vigilar" if vigilando else "Vigilar cancelaciones"}</button></form>')
            borrar = (f'<form method="post" action="{url_for("reserva_borrar", rid=j["id"])}"'
                      f' onsubmit="return confirm(\'¿Quitar esta reserva de la lista? (todavía no reservó nada en OnDepor)\')">'
                      f'<button class="sec btn-ico" aria-label="Quitar de la lista" title="Quitar de la lista">{icon("x")}</button></form>')
            botones = editar_btn + vig_btn + borrar

    acciones = f'<div class="acciones">{botones}</div>' if botones else ""
    return f"""<li class="reserva">
          <div class="sp">
            <div class="titulo"><span>{fmt_fecha(j['fecha']) if j['fecha'] else '(sin fecha)'} · {esc(j['hora_desde'])}–{esc(j['hora_hasta'])}</span>{pill}</div>
            <div class="meta">
              <span>{icon(sede_ic)} {sede_lab}</span>
              <span>{icon('pin')} {cancha_disp}{cancha_warn}</span>
              <span>{icon('users')} {esc(j['reservador'])}, {esc(acomp_txt)}</span>
            </div>
            {notas_html}
            {acciones}
          </div>
        </li>"""


@app.route("/reservas")
@login_requerido
def reservas():
    reservadores = cargar("db_reservadores.json", "reservadores")
    jugadores = cargar("db_jugadores.json", "jugadores")
    reservas_raw = cargar("reservas.json", "reservas")
    jobs = rb.buscar_trabajos("all", datos=(jugadores, reservadores, reservas_raw))
    creado_map = {r.get("id"): r.get("creado_por", "") for r in reservas_raw}

    # ¿estamos editando (?editar=), cambiando jugadores YA (?cambiar=) o programando un
    # cambio de nombres para 10 min antes del turno (?programar=)? (los últimos 2, solo admin)
    editar_id = request.args.get("editar", "")
    cambiar_id = request.args.get("cambiar", "") if puede("cambiar_ondepor") else ""
    programar_id = request.args.get("programar", "") if puede("cambiar_ondepor") else ""
    modo_cambiar = bool(cambiar_id)
    modo_programar = bool(programar_id) and not modo_cambiar
    foco_id = cambiar_id or programar_id or editar_id
    reserva_ed = next((r for r in reservas_raw if r.get("id") == foco_id), None) if foco_id else None
    job_ed = next((j for j in jobs if j["id"] == foco_id), None) if foco_id else None
    ids_edit = set(job_ed["player_ids"]) if job_ed else set()
    cp_ed = (reserva_ed or {}).get("cambio_prog") if modo_programar else None
    if cp_ed:                      # editar un cambio ya programado: precargar SUS valores
        ids_edit = set(cp_ed.get("jugadores", []))

    def _ck(c):  # ordena canchas: numéricas por número, luego con nombre, "cualquiera" al final
        c = str(c or "").strip()
        return (0, int(c), "") if c.isdigit() else ((2, 0, "") if c == "" else (1, 0, c.lower()))
    jobs.sort(key=lambda j: (j["fecha"], j["hora_desde"], _ck(j["cancha"])))

    # conflictos de jugador por club+día y slots repetidos (para los avisos del form)
    ocupados, slots, res_pid = _conflictos_y_slots(jobs, reservadores, jugadores, foco_id)

    # ---- listas de reservas: programadas (aún no dispararon) y confirmadas (ya tomadas) ----
    cambio_map = {r.get("id"): r["cambio_prog"] for r in reservas_raw if r.get("cambio_prog")}
    vigilar_map = {r.get("id"): r.get("vigilar") for r in reservas_raw}
    nom_por_id = {str(x.get("player_id")): x.get("nombre") for x in jugadores}
    filas_prog, filas_conf = "", ""
    for j in jobs:
        est = get_estado(j["id"])
        fila = _fila_reserva(j, est, cambio_map.get(j["id"]),
                             creado_map.get(j["id"], ""), nom_por_id,
                             vigilando=bool(vigilar_map.get(j["id"])))
        if est == "reservada":
            filas_conf += fila
        else:
            filas_prog += fila
    if not filas_prog:
        filas_prog = (f'<li class="vacio">{icon("inbox")}<div>No tenés reservas programadas.<br>'
                      f'Cargá una arriba y el bot la toma sola a la apertura.</div></li>')
    if not filas_conf:
        filas_conf = f'<li class="vacio">{icon("check-circle")}<div>Todavía no hay turnos confirmados.</div></li>'

    # ---- valores del formulario (con o sin edición) ----
    manana = (hoy_ar() + datetime.timedelta(days=1)).isoformat()   # fecha ARG, no UTC
    editando = reserva_ed is not None and not modo_cambiar and not modo_programar
    sede_ini = SEDE_POR_CLUB.get(reserva_ed.get("club_id", 1102), "dia") if reserva_ed else "dia"
    sede_cfg = SEDES[sede_ini]
    sel_res = reserva_ed["reservador"] if reserva_ed else ""
    if cp_ed and cp_ed.get("reservador"):
        sel_res = cp_ed["reservador"]
    v_fecha = reserva_ed["fecha"] if reserva_ed else manana
    sel_hora = reserva_ed["hora_desde"] if reserva_ed else sede_cfg["horas"][0]
    sel_cancha = str(reserva_ed.get("cancha", "")) if reserva_ed else ""

    res_ord = sorted(reservadores, key=lambda r: str(r.get("nombre", "")).lower())
    # si es una reserva nueva no se preselecciona a nadie: hay que elegir a propósito
    # (antes quedaba el primero de la lista y era fácil reservar con el equivocado)
    opts_res = ("" if sel_res else '<option value="" selected disabled>Seleccioná el reservador…</option>') \
        + "".join(f"<option{' selected' if r['nombre'] == sel_res else ''}>{esc(r['nombre'])}</option>" for r in res_ord)
    jugadores_ord = sorted(jugadores, key=lambda j: str(j.get("nombre", "")).lower())
    checks = "".join(
        f"<label class='jugitem' data-n='{esc(str(j['nombre']).lower())}' "
        f"style='display:flex;gap:8px;align-items:center;font-size:1rem;color:#222'>"
        f"<input type='checkbox' name='jug' value='{esc(j['player_id'])}'"
        f"{' checked' if j['player_id'] in ids_edit else ''} onchange='onCheck(event)'> "
        f"{esc(j['nombre'])}</label>"
        for j in jugadores_ord) or "<small class='hint'>No hay jugadores. Cargalos en la pestaña Jugadores.</small>"

    opts_hora = "".join(f"<option{' selected' if h == sel_hora else ''}>{h}</option>" for h in sede_cfg["horas"])

    def _selc(v):
        return " selected" if v == sel_cancha else ""
    opts_cancha = ("<optgroup label='Cancha específica'>"
        + "".join(f"<option value='{v}'{_selc(v)}>{lbl}</option>" for v, lbl in sede_cfg["canchas"])
        + "</optgroup><optgroup label='Por zona (la primera libre de la zona)'>"
        + "".join(f"<option value='{zv}'{_selc(zv)}>{zl}</option>" for zv, zl in sede_cfg["zonas"])
        + "</optgroup>"
        + f"<option value=''{_selc('')}>Cualquiera libre</option>")

    radios_sede = "".join(
        "<label style='display:inline-flex;align-items:center;gap:6px;margin-right:18px;font-size:1rem;color:var(--texto)'>"
        f"<input type='radio' name='sede' value='{k}'{' checked' if k == sede_ini else ''} "
        f"onchange='cambiarSede()'> {icon('moon' if k == 'noche' else 'sun')} {v['label']}</label>"
        for k, v in SEDES.items())

    if modo_cambiar:
        tit = "Cambiar jugadores (reserva confirmada)"
    elif modo_programar:
        tit = "Programar cambio de nombres (se hace solo 10 min antes del turno)"
    elif editando:
        tit = "Editar reserva"
    else:
        tit = "Nueva reserva"
    hidden_edit = f'<input type="hidden" name="edit_orig" value="{editar_id}">' if editando else ""
    btn_prog = f"{icon('save')} Guardar cambios" if editando else f"{icon('calendar')} Programar reserva"
    cancelar_ed = f'<a class="btn sec" href="{url_for("reservas")}">Cancelar</a>' if (editando or modo_cambiar or modo_programar) else ""

    # ---- botonera del formulario (según modo: nueva / editar / cambiar / programar) ----
    # Acción principal = "Programar" (verde). "Reservar ahora" = secundaria (mismo día).
    bloque_botones = f"""
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
          <button class="js-loader">{btn_prog}</button>
          <button class="sec js-loader" formaction="{url_for('reserva_ya')}">{icon('bolt')} Reservar ahora</button>
          {cancelar_ed}
        </div>
        <div style="margin-top:10px"><small class="hint">
          <b>Programar:</b> para turnos futuros; se dispara sola a la apertura (turno −24 h +1 min).<br>
          <b>Reservar ahora:</b> para turnos del mismo día que ya están abiertos (reserva al instante).
        </small></div>"""
    if modo_cambiar:
        conf = ("return confirm('Esto CANCELA la reserva en OnDepor y la vuelve a tomar en la misma "
                "cancha con los jugadores nuevos. Hay un riesgo mínimo de perder la cancha en el "
                "cambio. ¿Seguir?')")
        bloque_botones = f"""
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
          <button class="dark js-loader" formaction="{url_for('reserva_cambiar_jugadores')}" onclick="{conf}">{icon('users')} Cambiar jugadores en OnDepor</button>
          {cancelar_ed}
        </div>
        <div style="margin-top:10px"><small class="hint">
          Cambiá el <b>reservador (jugador 1)</b> y/o los <b>jugadores acompañantes</b> (la fecha, hora y
          cancha quedan igual). Al confirmar, el bot <b>cancela y vuelve a tomar la misma cancha</b> con
          los datos nuevos. Si no entra, recupera la reserva original. Si cambiás el reservador, la nueva
          la toma su cuenta.
        </small></div>"""
    if modo_programar:
        try:
            hora_ej_form = (datetime.datetime.fromisoformat(
                f"{reserva_ed['fecha']}T{reserva_ed['hora_desde']}:00")
                - datetime.timedelta(seconds=CAMBIO_PROG_ANTES_SEG)).strftime("%H:%M")
        except (ValueError, TypeError, KeyError):
            hora_ej_form = "?"
        bloque_botones = f"""
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
          <button class="dark js-loader" formaction="{url_for('reserva_programar_cambio')}">{icon('clock')} Programar cambio (se hace a las {hora_ej_form})</button>
          {cancelar_ed}
        </div>
        <div style="margin-top:10px"><small class="hint">
          El cambio <b>NO se hace ahora</b>: queda agendado y el bot lo ejecuta solo <b>10 minutos antes
          del turno</b> (a las <b>{hora_ej_form}</b>): cancela y vuelve a tomar la misma cancha con los
          nombres nuevos; si no entra, recupera la reserva original. Podés editarlo o anularlo desde la
          lista hasta ese momento.
        </small></div>"""

    # ---- JS (string aparte para no chocar con las llaves del f-string) ----
    script_block = """
    <script>
    var OCUP = __OCUP__;      // { "1102": {fecha:[ids]}, "504": {fecha:[ids]} }
    var RESPID = __RESPID__;  // { "Nombre del reservador": player_id }
    var SLOTS = __SLOTS__;    // ["club|fecha|hora|cancha", ...]
    var SEDES = __SEDES__;
    function sedeSel(){ var r=document.querySelector('input[name=sede]:checked'); return r?r.value:'dia'; }
    function clubSel(){ return String(SEDES[sedeSel()].club_id); }
    function fechaSel(){ var e=document.querySelector('input[name=fecha]'); return e?e.value:''; }
    function filtrarJug(){
      var q=(document.getElementById('jugfiltro').value||'').toLowerCase();
      document.querySelectorAll('#juglista .jugitem').forEach(function(el){
        el.style.display = el.getAttribute('data-n').indexOf(q)>-1 ? 'flex':'none';
      });
    }
    function rebuildDropdowns(){
      var s=SEDES[sedeSel()];
      var ho=document.querySelector('select[name=hora]');
      var ca=document.querySelector('select[name=cancha]');
      if(ho){ ho.innerHTML = s.horas.map(function(h){ return "<option>"+h+"</option>"; }).join(''); }
      if(ca){
        var o = "<optgroup label='Cancha especifica'>" + s.canchas.map(function(c){ return "<option value='"+c[0]+"'>"+c[1]+"</option>"; }).join('') + "</optgroup>";
        o += "<optgroup label='Por zona'>" + s.zonas.map(function(z){ return "<option value='"+z[0]+"'>"+z[1]+"</option>"; }).join('') + "</optgroup>";
        o += "<option value=''>Cualquiera libre</option>";
        ca.innerHTML = o;
      }
    }
    function cambiarSede(){ rebuildDropdowns(); marcarJugadores(); chequearSlot(); }
    // avisa (NO bloquea) si el reservador elegido ya está ese día en ese club, sea como
    // reservador de otra reserva o como jugador de otra
    function avisarReservador(){
      var sel = document.querySelector('select[name=reservador]');
      var caja = document.getElementById('avisoRes');
      if(!sel || !caja) return;
      var pid = RESPID[sel.value];
      var ocup = ((OCUP[clubSel()]||{})[fechaSel()]) || [];
      if(pid && ocup.indexOf(pid) > -1){
        caja.innerHTML = '&#9888; <b>' + sel.value + '</b> ya figura en otra reserva de ese día'
                       + ' (como reservador o como jugador). OnDepor puede rechazarla.';
        caja.style.display = 'block';
      } else {
        caja.style.display = 'none';
      }
    }
    function marcarJugadores(){
      avisarReservador();
      var ocup = ((OCUP[clubSel()]||{})[fechaSel()]) || [];
      document.querySelectorAll('#juglista .jugitem').forEach(function(el){
        var id = parseInt(el.querySelector('input').value,10);
        var nota = el.querySelector('.nota-ocup');
        if(ocup.indexOf(id)>-1){
          el.style.color='#9a6a12';
          if(!nota){ nota=document.createElement('small'); nota.className='nota-ocup'; nota.style.marginLeft='6px'; nota.style.color='#9a6a12'; nota.style.fontSize='.8rem'; nota.style.whiteSpace='nowrap'; nota.textContent='· ya juega ese día'; el.appendChild(nota); }
        } else { el.style.color='var(--texto)'; if(nota) nota.remove(); }
      });
    }
    function contar(){
      var c=document.querySelectorAll('#juglista input:checked').length;
      var cont=document.getElementById('cont');
      if(cont){ cont.textContent=c+'/3'; cont.style.color = c===3?'#157f3b':(c>3?'#cf3b3b':'#7a828e'); }
    }
    function limpiarBusqueda(){
      // al elegir un jugador se borra lo buscado y queda listo para buscar el siguiente
      var f=document.getElementById('jugfiltro');
      if(f && f.value){ f.value=''; filtrarJug(); f.focus(); }
    }
    function onCheck(e){
      if(document.querySelectorAll('#juglista input:checked').length>3 && e.target.checked){
        e.target.checked=false;
        alert('Para dobles son 3 acompanantes (vos sos el jugador 1). Ya tenes 3 elegidos.');
      } else if(e.target.checked){
        limpiarBusqueda();
      }
      contar();
    }
    function chequearSlot(){
      var ho=(document.querySelector('select[name=hora]')||{}).value||'';
      var ca=(document.querySelector('select[name=cancha]')||{}).value||'';
      var w=document.getElementById('avisoSlot'); if(!w) return;
      w.style.display = (ca!=='' && SLOTS.indexOf(clubSel()+'|'+fechaSel()+'|'+ho+'|'+ca)>-1) ? 'block':'none';
    }
    // cuenta regresiva viva ("faltan 3 h 20 m") + auto-refresco de la lista
    function txtFalta(seg){
      if(seg <= 0) return ' · disparando…';
      var d=Math.floor(seg/86400), h=Math.floor(seg%86400/3600), m=Math.floor(seg%3600/60), s=Math.floor(seg%60);
      if(d>0) return ' · faltan '+d+' d '+h+' h';
      if(h>0) return ' · faltan '+h+' h '+m+' m';
      if(m>0) return ' · faltan '+m+' m '+s+' s';
      return ' · faltan '+s+' s';
    }
    function tick(){
      var prox = Infinity, ahora = Date.now()/1000;
      document.querySelectorAll('.cuenta').forEach(function(el){
        var t = parseInt(el.getAttribute('data-t')||'0', 10);
        if(!t) return;
        var falta = t - ahora;
        el.textContent = txtFalta(falta);
        el.style.color = (falta > 0 && falta < 300) ? '#b42318' : '';
        if(falta > -5 && falta < prox) prox = falta;
      });
      // si algo está por dispararse (o recién disparó), refrescar para ver el resultado
      var editando = location.search.indexOf('editar=')>-1 || location.search.indexOf('cambiar=')>-1
                     || location.search.indexOf('programar=')>-1;
      if(!editando && prox < 20){ setTimeout(function(){ location.reload(); }, 12000); }
    }
    document.addEventListener('DOMContentLoaded', function(){
      marcarJugadores(); contar(); chequearSlot(); tick(); setInterval(tick, 1000);
    });
    document.addEventListener('submit', function(){ var o=document.getElementById('ov'); if(o) o.classList.add('on'); });
    </script>""".replace("__OCUP__", json.dumps(ocupados)).replace("__RESPID__", json.dumps(res_pid)).replace("__SLOTS__", json.dumps(sorted(slots))).replace("__SEDES__", json.dumps({k: {"club_id": v["club_id"], "horas": v["horas"], "canchas": v["canchas"], "zonas": v["zonas"]} for k, v in SEDES.items()}))

    hidden_cambiar = f'<input type="hidden" name="cambiar_id" value="{cambiar_id}">' if modo_cambiar else ""
    if modo_programar:
        hidden_cambiar = f'<input type="hidden" name="programar_id" value="{programar_id}">'
    dis = "disabled" if (modo_cambiar or modo_programar) else ""
    tit_ic = ("users" if modo_cambiar else "clock" if modo_programar else "edit" if editando else "plus")
    # aviso para activar las notificaciones (se oculta solo si ya están activas o no se soportan)
    caja_avisos = f"""
    <div class="avisos-box" id="cajaAvisos" style="display:none">
      {icon('bell')}
      <div class="txt"><b>Activá los avisos</b><br>
        Te llega una notificación al celular cuando tus reservas se confirman o fallan.</div>
      <button type="button" id="btnAvisos" onclick="pedirAvisos(this)">Activar</button>
    </div>
    <script>
    (async function(){{
      try{{
        if(!('Notification' in window) || !('serviceWorker' in navigator)) return;
        var r = await fetch('/api/push/clave'); var d = await r.json();
        if(!d.activo) return;
        if(d.ya_tiene) return;            // ya los activó en otro dispositivo: no insistir
        if(Notification.permission === 'default') document.getElementById('cajaAvisos').style.display='flex';
      }}catch(e){{}}
    }})();
    async function pedirAvisos(b){{
      b.disabled = true; b.innerHTML = '<span class="spin"></span> Activando…';
      var ok = await activarAvisos(false);
      if(ok){{
        await fetch('/api/push/probar', {{method:'POST'}});
        document.getElementById('cajaAvisos').innerHTML =
          '<div class="txt">✅ <b>Avisos activados.</b> Te mandamos uno de prueba.</div>';
      }} else {{
        b.disabled = false; b.textContent = 'Activar';
        alert('No se pudieron activar. Revisá que el navegador tenga permitidas las notificaciones.');
      }}
    }}
    </script>"""

    cuerpo = f"""
    {caja_avisos}
    <div class="card">
      <h2>{icon(tit_ic)} {tit}</h2>
      <form method="post" action="{url_for('reserva_crear')}">
        {hidden_edit}{hidden_cambiar}
        <label>Turno</label>
        <div style="margin:2px 0 10px">{radios_sede}</div>
        <div class="row">
          <div><label>Reservador (jugador 1)</label><select name="reservador" required onchange="avisarReservador()">{opts_res}</select>
            <div id="avisoRes" class="nota warn" style="display:none;margin-top:6px"></div></div>
          <div><label>Fecha del turno</label><input type="date" name="fecha" value="{v_fecha}" {dis} onchange="marcarJugadores();chequearSlot()"></div>
        </div>
        <div class="row">
          <div><label>Hora (turnos de 1 h)</label><select name="hora" {dis} onchange="chequearSlot()">{opts_hora}</select></div>
          <div><label>Cancha</label><select name="cancha" {dis} onchange="chequearSlot()">{opts_cancha}</select></div>
        </div>
        <div id="avisoSlot" class="msg warn" style="display:none">{icon('alert')} Ya hay una reserva cargada para esa fecha, hora y cancha.</div>
        <label>Jugadores acompañantes (elegí 3) — <span id="cont" style="color:#7a828e">0/3</span></label>
        <input id="jugfiltro" placeholder="Buscar jugador…" oninput="filtrarJug()" style="margin:4px 0 8px">
        <div id="juglista" style="display:flex;flex-direction:column;gap:2px;max-height:260px;overflow:auto;border:1px solid var(--borde);border-radius:10px;padding:8px">{checks}</div>
        <div style="height:16px"></div>
        {bloque_botones}
      </form>
    </div>
    <div class="card">
      <h2>{icon('list')} Reservas programadas</h2>
      <ul class="lista">{filas_prog}</ul>
    </div>
    <div class="card">
      <h2>{icon('check-circle')} Reservas confirmadas</h2>
      <ul class="lista">{filas_conf}</ul>
    </div>
    <div style="text-align:center;margin:4px 0 18px">
      <a href="#" id="linkAvisos" style="display:none;font-size:.85rem;color:var(--texto-suave)"
         onclick="pedirAvisos(this);return false">{icon('bell')} Activar avisos también en este dispositivo</a>
    </div>
    <script>
    // el permiso de notificaciones es POR dispositivo: si en ÉSTE no está dado, ofrecerlo
    // discretamente (la caja grande solo aparece si el usuario no los tiene en ninguno)
    (function(){{
      if(!('Notification' in window) || Notification.permission !== 'default') return;
      fetch('/api/push/clave').then(r=>r.json()).then(function(d){{
        if(d.activo && d.ya_tiene) document.getElementById('linkAvisos').style.display='inline-block';
      }}).catch(function(){{}});
    }})();
    </script>
    <div id="ov" class="overlay"><div class="spinner"></div><div>Procesando…</div></div>
    {script_block}"""
    return pagina("Reservas", "reservas", cuerpo)


def _guardar_reserva_desde_form(f):
    """Lee el formulario, VALIDA en el backend (no solo en el navegador), guarda/actualiza
    la reserva en reservas.json y devuelve su id. Lanza ValueError si los datos están mal."""
    sede = f.get("sede", "dia")
    if sede not in SEDES:
        sede = "dia"
    cfg = SEDES[sede]
    club_id = cfg["club_id"]

    fecha = f.get("fecha", "").strip()
    try:
        d = datetime.date.fromisoformat(fecha)
    except ValueError:
        raise ValueError("la fecha no es válida")
    # fecha de HOY en Argentina (el server corre en UTC; de noche difiere del día local)
    hoy_ar = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)).date()
    if d < hoy_ar:
        raise ValueError("la fecha ya pasó")

    hora = f.get("hora", "").strip()
    if hora not in cfg["horas"]:
        raise ValueError("esa hora no corresponde al turno elegido (día/noche)")
    hh, mm = map(int, hora.split(":"))
    fin = datetime.datetime(2000, 1, 1, hh, mm) + datetime.timedelta(hours=1)  # turnos de 1 h

    reservador = f.get("reservador", "").strip()
    if not reservador:
        raise ValueError("elegí un reservador")

    cancha = f.get("cancha", "").strip()
    canchas_validas = {v for v, _ in cfg["canchas"]} | {z for z, _ in cfg["zonas"]} | {""}
    if cancha not in canchas_validas:
        raise ValueError("la cancha no corresponde al turno elegido")

    jugadores = [int(x) for x in f.getlist("jug") if str(x).strip().isdigit()]
    jugadores = list(dict.fromkeys(jugadores))   # sin duplicados, conserva el orden
    if len(jugadores) != 3:
        raise ValueError("elegí exactamente 3 jugadores acompañantes")

    single_double = 2  # siempre dobles (4 jugadores)
    edit_orig = f.get("edit_orig", "").strip()
    # id ÚNICO por reservador (nombre completo -> slug): evita que dos "Dario" distintos
    # con mismo día/hora/cancha generen el mismo id y se pisen (M2).
    rid = f"{_slug(reservador)}-{fecha}-{hora.replace(':','')}-{(cancha or 'lib').lower()[:8]}-c{club_id}"

    # leer-modificar-escribir bajo el lock (atómico) para evitar lost-update entre requests
    with rb._io_lock:
        lista = cargar("reservas.json", "reservas")
        # si es edición: chequear permiso y conservar el creador original
        orig_obj = next((x for x in lista if x.get("id") == edit_orig), None) if edit_orig else None
        if orig_obj and not puede("todas_reservas") and orig_obj.get("creado_por") != session.get("usuario"):
            raise ValueError("solo podés editar las reservas que creaste vos")
        creado_por = (orig_obj.get("creado_por") if orig_obj else None) or session.get("usuario", "")

        remove_ids = {rid} | ({edit_orig} if edit_orig else set())
        lista = [x for x in lista if x.get("id") not in remove_ids]
        lista.append({
            "id": rid, "club_id": club_id, "cancha": cancha,
            "fecha": fecha, "hora_desde": hora, "hora_hasta": fin.strftime("%H:%M"),
            "single_double": single_double, "reservador": reservador,
            "jugadores": jugadores, "creado_por": creado_por, "apertura_iso": None,
            "horas_antes": 24, "minutos_extra": 1,
            "reintentos": 40, "espera_entre_reintentos_seg": 0.3,
        })
        guardar("reservas.json", "reservas", lista,
                ayuda="Reservas precargadas; se disparan a la apertura (turno -24h +1min).")
    with _estados_lock:
        for _id in remove_ids:
            _estados.pop(_id, None)
    _guardar_estados()
    return rid


@app.route("/reservas/crear", methods=["POST"])
@login_requerido
def reserva_crear():
    try:
        _guardar_reserva_desde_form(request.form)
    except Exception as e:
        flash(f"Revisá los datos: {e}")
        return redirect(url_for("reservas"))
    flash("Reserva guardada y programada. Se disparará sola a la apertura.")
    return redirect(url_for("reservas"))


@app.route("/reservas/ya", methods=["POST"])
@login_requerido
def reserva_ya():
    try:
        rid = _guardar_reserva_desde_form(request.form)
    except Exception as e:
        flash(f"Revisá los datos: {e}")
        return redirect(url_for("reservas"))
    jobs = rb.buscar_trabajos(rid)
    if not jobs or jobs[0].get("problema"):
        flash("No pude armar la reserva: " + (jobs[0]["problema"] if jobs else "revisá los datos"))
        return redirect(url_for("reservas"))
    # SINCRÓNICO: reserva ahora mismo y espera el resultado (son pocos segundos)
    ok = _disparar_job(jobs[0], esperar=False)
    detalle = resumen_log(rid)
    if ok:
        flash("Reserva confirmada. " + detalle)
    else:
        flash("No se pudo reservar. " + (detalle or "Fijate el detalle en la lista de abajo."))
    return redirect(url_for("reservas"))


def _hacer_cambio_jugadores(rsv, nuevos, nuevo_nombre):
    """Ejecuta EN OnDepor el cambio de jugadores/reservador de una reserva confirmada
    (cancela + re-toma la misma cancha; red de seguridad adentro del motor). Devuelve
    (status, mensaje). Si sale bien, actualiza la reserva en reservas.json. Lo usan el
    cambio inmediato (botón) y el cambio programado (manager, 10 min antes del turno)."""
    rid = rsv.get("id")
    jug_db = cargar("db_jugadores.json", "jugadores")
    res_db = cargar("db_reservadores.json", "reservadores")
    viejos = [rb.resolver_player_id(x, jug_db, res_db) for x in rsv.get("jugadores", [])]
    viejos = [v for v in viejos if v]

    # reservador ORIGINAL (dueño, el que cancela) y NUEVO (el que re-toma; puede ser el mismo)
    orig_nombre = rsv.get("reservador")
    nuevo_nombre = (nuevo_nombre or orig_nombre or "").strip() or orig_nombre
    r_orig = next((r for r in res_db if r.get("nombre") == orig_nombre), None)
    r_nuevo = next((r for r in res_db if r.get("nombre") == nuevo_nombre), None)
    if not r_orig or not r_orig.get("clave"):
        return "error", "El reservador original no tiene credenciales cargadas."
    if not r_nuevo or not r_nuevo.get("clave"):
        return "error", "El reservador nuevo no tiene credenciales cargadas."

    club_id = rsv.get("club_id", CLUB_DEFAULT)
    try:
        api = OnDepor(r_orig["usuario"], r_orig["clave"], club_id)
        api.login()
        api_nuevo = None
        if nuevo_nombre != orig_nombre:
            api_nuevo = OnDepor(r_nuevo["usuario"], r_nuevo["clave"], club_id)
            api_nuevo.login()
        res = api.cambiar_jugadores(rsv["fecha"], club_id, rsv["hora_desde"], rsv["hora_hasta"],
                                    nuevos, viejos, single_double=rsv.get("single_double", 2),
                                    telephone=r_orig.get("telefono", ""),
                                    api_nuevo=api_nuevo, telephone_nuevo=r_nuevo.get("telefono", ""))
    except Exception as e:
        return "error", str(e)[:200]

    st = res.get("status")
    msg = str(res.get("mensaje", res.get("errors", "")))
    if st == "success":
        # re-leer bajo el lock y actualizar SOLO esta reserva (el cambio pasó por red, lo
        # leído antes puede estar viejo -> no escribir una lista vieja entera)
        with rb._io_lock:
            lista2 = cargar("reservas.json", "reservas")
            for x in lista2:
                if x.get("id") == rid:
                    x["jugadores"] = nuevos
                    x["reservador"] = nuevo_nombre
                    break
            guardar("reservas.json", "reservas", lista2)
    elif st == "perdida":
        set_estado(rid, "fallada", msg)      # se perdió la cancha
    return st, msg


def _validar_cambio_form(form, arg_retorno):
    """Validaciones comunes de los forms de cambio (inmediato y programado).
    Devuelve (rsv, nuevos, nuevo_nombre, None) o (None, None, None, redirect)."""
    rid = form.get(arg_retorno + "_id", "").strip()
    nuevos = [int(x) for x in form.getlist("jug")]
    rsv = next((x for x in cargar("reservas.json", "reservas") if x.get("id") == rid), None)
    if not rsv:
        flash("No encontré esa reserva.")
        return None, None, None, redirect(url_for("reservas"))
    if get_estado(rid) != "reservada":
        flash("Solo se pueden cambiar jugadores de una reserva ya confirmada.")
        return None, None, None, redirect(url_for("reservas"))
    if len(nuevos) < 3:
        flash("Elegí 3 jugadores acompañantes.")
        return None, None, None, redirect(url_for("reservas") + f"?{arg_retorno}={rid}")
    orig_nombre = rsv.get("reservador")
    nuevo_nombre = (form.get("reservador", "") or orig_nombre).strip() or orig_nombre
    # el reservador nuevo (jugador 1) no puede estar también entre los acompañantes
    jug_db = cargar("db_jugadores.json", "jugadores")
    res_db = cargar("db_reservadores.json", "reservadores")
    r_nuevo = next((r for r in res_db if r.get("nombre") == nuevo_nombre), None)
    if not r_nuevo or not r_nuevo.get("clave"):
        flash("El reservador nuevo no tiene credenciales cargadas.")
        return None, None, None, redirect(url_for("reservas"))
    pid_nuevo = r_nuevo.get("player_id") or rb.resolver_player_id(nuevo_nombre, jug_db, res_db)
    if pid_nuevo and pid_nuevo in nuevos:
        flash("El reservador nuevo no puede estar también en la lista de acompañantes.")
        return None, None, None, redirect(url_for("reservas") + f"?{arg_retorno}={rid}")
    return rsv, nuevos, nuevo_nombre, None


@app.route("/reservas/cambiar-jugadores", methods=["POST"])
@permiso_requerido("cambiar_ondepor")
def reserva_cambiar_jugadores():
    rsv, nuevos, nuevo_nombre, err = _validar_cambio_form(request.form, "cambiar")
    if err:
        return err
    st, msg = _hacer_cambio_jugadores(rsv, nuevos, nuevo_nombre)
    if st == "success":
        flash(msg)
    elif st == "revertida":
        flash(msg)                           # siguen los originales; no se tocó reservas.json
    elif st == "perdida":
        flash(msg)
    elif st == "error":
        flash("Error al cambiar: " + msg)
    else:
        flash("No se cambió: " + msg)
    return redirect(url_for("reservas"))


@app.route("/reservas/programar-cambio", methods=["POST"])
@permiso_requerido("cambiar_ondepor")
def reserva_programar_cambio():
    rsv, nuevos, nuevo_nombre, err = _validar_cambio_form(request.form, "programar")
    if err:
        return err
    rid = rsv["id"]
    try:
        turno = datetime.datetime.fromisoformat(f"{rsv['fecha']}T{rsv['hora_desde']}:00")
        ej_epoch = _epoch_ar(turno) - CAMBIO_PROG_ANTES_SEG
        hora_ej = (turno - datetime.timedelta(seconds=CAMBIO_PROG_ANTES_SEG)).strftime("%H:%M")
    except (ValueError, TypeError):
        flash("La reserva tiene fecha u hora inválida.")
        return redirect(url_for("reservas"))
    if hora_real() >= ej_epoch:
        flash("Faltan menos de 10 minutos para el turno: usá 'Cambiar jugadores' directo.")
        return redirect(url_for("reservas") + "?cambiar=" + rid)
    with rb._io_lock:
        lista = cargar("reservas.json", "reservas")
        for x in lista:
            if x.get("id") == rid:
                x["cambio_prog"] = {"reservador": nuevo_nombre, "jugadores": nuevos,
                                    "estado": "pendiente", "detalle": "", "hora_ej": hora_ej,
                                    "creado_por": session.get("usuario", "")}
                break
        guardar("reservas.json", "reservas", lista)
    flash(f"Cambio de nombres programado: se ejecuta solo a las {hora_ej} (10 min antes del turno).")
    return redirect(url_for("reservas"))


@app.route("/reservas/cambio-prog-borrar/<rid>", methods=["POST"])
@permiso_requerido("cambiar_ondepor")
def cambio_prog_borrar(rid):
    with rb._io_lock:
        lista = cargar("reservas.json", "reservas")
        obj = next((x for x in lista if x.get("id") == rid), None)
        if obj and obj.pop("cambio_prog", None) is not None:
            guardar("reservas.json", "reservas", lista)
            flash("Cambio programado anulado: quedan los nombres actuales.")
        else:
            flash("Esa reserva no tenía un cambio programado.")
    return redirect(url_for("reservas"))


def _cancelar_en_ondepor(obj):
    """Cancela en OnDepor el turno que corresponde a esta reserva del bot.
    Loguea con la cuenta que la reservó, busca el turno por fecha+hora y lo cancela."""
    res = next((r for r in cargar("db_reservadores.json", "reservadores")
                if r.get("nombre") == obj.get("reservador")), None)
    if not res or not res.get("clave"):
        return "No pude cancelar en OnDepor: falta la clave del reservador."
    try:
        api = OnDepor(res["usuario"], res["clave"], obj.get("club_id", CLUB_DEFAULT))
        api.login()
        turnos = api.mis_reservas(30)
        club = str(obj.get("club_id", CLUB_DEFAULT))
        match = next((t for t in turnos
                      if str(t.get("fecha")) == str(obj.get("fecha"))
                      and str(t.get("club_id")) == club), None)
        if not match:
            return "En OnDepor no encontré ese turno (¿ya no estaba, o lo tomó otra cuenta?)."
        resp = api.cancelar_reserva(match["id"])
        if resp.get("status") == "success":
            return "✅ Turno cancelado también en OnDepor."
        return "No se pudo cancelar en OnDepor: " + json.dumps(resp.get("errors", resp), ensure_ascii=False)[:150]
    except Exception as e:
        return "Error al cancelar en OnDepor: " + str(e)[:150]


@app.route("/reservas/vigilar/<rid>", methods=["POST"])
@login_requerido
def reserva_vigilar(rid):
    """Prende/apaga la caza de cancelaciones para una reserva que no entró."""
    obj = next((x for x in cargar("reservas.json", "reservas") if x.get("id") == rid), None)
    if obj is None:
        return redirect(url_for("reservas"))
    creador = obj.get("creado_por", "")
    if not puede("todas_reservas") and creador != session.get("usuario"):
        flash("Solo podés vigilar las reservas que creaste vos.")
        return redirect(url_for("reservas"))
    nuevo = not obj.get("vigilar")
    _marcar_en_reserva(rid, "vigilar", nuevo)
    if nuevo:
        set_estado(rid, "programada")     # vuelve a la lista de pendientes
        _ultima_vigilancia.pop(rid, None)
        flash("👀 Vigilando: si alguien cancela, te tomo la cancha y te aviso.")
    else:
        flash("Dejé de vigilar esa reserva.")
    return redirect(url_for("reservas"))


@app.route("/reservas/borrar/<rid>", methods=["POST"])
@login_requerido
def reserva_borrar(rid):
    obj = next((x for x in cargar("reservas.json", "reservas") if x.get("id") == rid), None)
    if obj is None:
        return redirect(url_for("reservas"))
    creador = obj.get("creado_por", "")
    if not puede("todas_reservas") and creador != session.get("usuario"):
        flash("Solo podés borrar las reservas que creaste vos.")
        return redirect(url_for("reservas"))
    # cancelación en OnDepor: es una llamada de RED (lenta) -> FUERA del lock de archivos
    if request.form.get("cancelar_ml") == "1" and get_estado(rid) == "reservada":
        flash(_cancelar_en_ondepor(obj))
    # borrado atómico: re-leer bajo el lock justo antes de escribir (evita lost-update)
    with rb._io_lock:
        lista = [x for x in cargar("reservas.json", "reservas") if x.get("id") != rid]
        guardar("reservas.json", "reservas", lista)
    with _estados_lock:
        _estados.pop(rid, None)
    _guardar_estados()
    try:                                  # limpiar el log huérfano de esa reserva
        os.remove(os.path.join(AQUI, "logs", f"{rid}.log"))
    except OSError:
        pass
    flash("Quitada de la lista del bot.")
    return redirect(url_for("reservas"))


# ----------------------------------------------------------------------
#  JUGADORES
# ----------------------------------------------------------------------
@app.route("/jugadores")
@login_requerido
def jugadores():
    guardados = cargar("db_jugadores.json", "jugadores")
    es_admin = puede("gestionar_bases")          # borrar (destructivo)
    puede_sumar = puede("agregar_jugadores")     # buscar y agregar (inofensivo)
    filas = ""
    for j in sorted(guardados, key=lambda x: str(x.get("nombre", "")).lower()):
        borrar = ('<form method="post" action="' + url_for('jugador_borrar', pid=j['player_id'])
                  + '" onsubmit="return confirm(\'¿Quitar este jugador de la base?\')">'
                  + f'<button class="danger btn-ico" aria-label="Borrar jugador" title="Borrar jugador">{icon("trash")}</button></form>') if es_admin else ''
        filas += (f'<li><div class="sp">{esc(j["nombre"])} <small class="hint">· id {esc(j["player_id"])}</small></div>'
                  f'<div class="acc">{borrar}</div></li>')
    if not filas:
        filas = (f'<li class="vacio">{icon("users")}<div>Todavía no guardaste jugadores.<br>'
                 f'Buscalos arriba por apellido y agregalos con un toque.</div></li>')
    buscador = f"""
    <div class="card">
      <h2>{icon('search')} Buscar y agregar jugador</h2>
      <p><small class="hint">Se busca en OnDepor por apellido. Al agregarlo queda disponible
         para las reservas y para la encuesta.</small></p>
      <div class="row">
        <div><input id="q" placeholder="Apellido…" onkeydown="if(event.key=='Enter')buscar()"></div>
        <div style="flex:0"><button onclick="buscar()">{icon('search')} Buscar</button></div>
      </div>
      <div id="estado" style="margin-top:8px"><small class="hint"></small></div>
      <ul class="lista compacta" id="result"></ul>
    </div>"""

    cuerpo = (buscador if puede_sumar else "") + f"""
    <div class="card">
      <h2>{icon('users')} Jugadores guardados <small class="hint" style="font-weight:400">({len(guardados)})</small></h2>
      <ul class="lista compacta">{filas}</ul>
    </div>
    <script>
    async function buscar(){{
      const q=document.getElementById('q').value.trim(); if(!q) return;
      document.getElementById('estado').innerHTML='<small class="hint">Buscando...</small>';
      document.getElementById('result').innerHTML='';
      try{{
        const r=await fetch('{url_for('api_buscar')}?q='+encodeURIComponent(q));
        const d=await r.json();
        if(d.error){{document.getElementById('estado').innerHTML='<small class="hint">'+d.error+'</small>';return;}}
        document.getElementById('estado').innerHTML='<small class="hint">'+d.jugadores.length+' resultado(s)</small>';
        const ul=document.getElementById('result');
        d.jugadores.forEach(j=>{{
          const li=document.createElement('li');
          li.innerHTML='<div class="sp">'+j.nombre+' <small class="hint">(id '+j.id+')</small></div>';
          const b=document.createElement('button'); b.textContent='Agregar';
          b.onclick=()=>agregar(j.id,j.nombre,b);
          li.appendChild(b); ul.appendChild(li);
        }});
      }}catch(e){{document.getElementById('estado').innerHTML='<small class="hint">Error: '+e+'</small>';}}
    }}
    async function agregar(id,nombre,btn){{
      btn.disabled=true; btn.textContent='Agregando…';
      const r=await fetch('{url_for('jugador_agregar')}',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{id:id,nombre:nombre}})}});
      if(r.ok){{btn.textContent='Agregado'; setTimeout(()=>location.reload(),500);}}
      else{{btn.textContent='Error'; btn.disabled=false;}}
    }}
    </script>"""
    return pagina("Jugadores", "jugadores", cuerpo)


@app.route("/recurrentes")
@login_requerido
def recurrentes():
    """Reservas RECURRENTES: se cargan solas cada semana (ej: todos los martes 22 hs)."""
    recs = rb.leer_json("recurrentes.json", {"recurrentes": []}).get("recurrentes", [])
    reservadores = cargar("db_reservadores.json", "reservadores")
    jugadores = cargar("db_jugadores.json", "jugadores")
    yo, es_admin = session.get("usuario", ""), puede("todas_reservas")
    nom_por_id = {str(x.get("player_id")): x.get("nombre") for x in jugadores}

    filas = ""
    for rec in sorted(recs, key=lambda r: (int(r.get("dia_semana", 0)), str(r.get("hora")))):
        mio = es_admin or rec.get("creado_por") == yo
        jugs = ", ".join(str(esc(nom_por_id.get(str(p), str(p)))) for p in rec.get("jugadores", []))
        sede = "Noche" if int(rec.get("club_id", 1102)) == 504 else "Día"
        activa = rec.get("activa", True)
        chip = (f'<span class="pill reservada">{icon("check")} Activa</span>' if activa
                else f'<span class="pill vencida">Pausada</span>')
        acciones = ""
        if mio:
            acciones = (
                f'<form method="post" action="{url_for("recurrente_pausar", rid=rec["id"])}" style="display:inline">'
                f'<button class="sec">{"Pausar" if activa else "Reactivar"}</button></form>'
                f'<form method="post" action="{url_for("recurrente_borrar", rid=rec["id"])}" style="display:inline"'
                f' onsubmit="return confirm(\'¿Borrar esta reserva recurrente? (las ya cargadas no se tocan)\')">'
                f'<button class="danger btn-ico" aria-label="Borrar" title="Borrar">{icon("trash")}</button></form>')
        filas += f"""<li class="reserva"><div class="sp">
          <div class="titulo"><span>{icon('repeat')} Todos los {DIAS_SEMANA[int(rec.get('dia_semana',0))].lower()} · {esc(rec.get('hora'))}</span>{chip}</div>
          <div class="meta"><span>{icon('sun' if sede=='Día' else 'moon')} {sede}</span>
            <span>{icon('pin')} {desc_cancha(rec.get('cancha',''))}</span>
            <span>{icon('users')} {esc(rec.get('reservador'))}{(', ' + jugs) if jugs else ''}</span></div>
          <small class="hint">se carga sola {RECURRENTE_ANTICIPO_DIAS} días antes · creada por {esc(rec.get('creado_por'))}</small>
          <div class="acciones">{acciones}</div>
        </div></li>"""
    if not filas:
        filas = (f'<li class="vacio">{icon("repeat")}<div>No tenés reservas recurrentes.<br>'
                 f'Cargá una y el bot la programa solo, todas las semanas.</div></li>')

    opts_res = ('<option value="" selected disabled>Seleccioná el reservador…</option>'
                + "".join(f"<option>{esc(r['nombre'])}</option>"
                          for r in sorted(reservadores, key=lambda r: str(r.get("nombre", "")).lower())))
    opts_dia = "".join(f'<option value="{i}">{d}</option>' for i, d in enumerate(DIAS_SEMANA))
    sede_cfg = SEDES["dia"]
    opts_hora = "".join(f"<option>{h}</option>" for h in sede_cfg["horas"])
    opts_cancha = ("<optgroup label='Cancha específica'>"
                   + "".join(f"<option value='{v}'>{lbl}</option>" for v, lbl in sede_cfg["canchas"])
                   + "</optgroup><optgroup label='Por zona'>"
                   + "".join(f"<option value='{zv}'>{zl}</option>" for zv, zl in sede_cfg["zonas"])
                   + "</optgroup><option value=''>Cualquiera libre</option>")
    checks = "".join(
        f"<label class='jugitem' data-n='{esc(str(j['nombre']).lower())}' "
        f"style='display:flex;gap:8px;align-items:center'>"
        f"<input type='checkbox' name='jug' value='{esc(j['player_id'])}' onchange='alElegirRec(event)'> {esc(j['nombre'])}</label>"
        for j in sorted(jugadores, key=lambda j: str(j.get("nombre", "")).lower()))

    radios = "".join(
        "<label style='display:inline-flex;align-items:center;gap:6px;margin-right:18px'>"
        f"<input type='radio' name='sede' value='{k}'{' checked' if k == 'dia' else ''} "
        f"onchange='cambiarSedeRec()'> {v['label']}</label>" for k, v in SEDES.items())

    cuerpo = f"""
    <div class="card">
      <h2>{icon('repeat')} Nueva reserva recurrente</h2>
      <p><small class="hint">Se programa sola todas las semanas, {RECURRENTE_ANTICIPO_DIAS} días antes del turno.
        Después se dispara como cualquier otra a la apertura.</small></p>
      <form method="post" action="{url_for('recurrente_guardar')}">
        <label>Turno</label>
        <div style="margin:2px 0 10px">{radios}</div>
        <div class="row">
          <div><label>Día de la semana</label><select name="dia_semana">{opts_dia}</select></div>
          <div><label>Hora</label><select name="hora">{opts_hora}</select></div>
        </div>
        <div class="row">
          <div><label>Reservador (jugador 1)</label><select name="reservador" required>{opts_res}</select></div>
          <div><label>Cancha</label><select name="cancha">{opts_cancha}</select></div>
        </div>
        <label>Jugadores acompañantes (elegí 3)</label>
        <input id="jugfiltro" placeholder="🔍 buscar jugador…" oninput="filtrarRec()" style="margin:4px 0 8px">
        <div id="juglista" style="display:flex;flex-direction:column;gap:6px;max-height:240px;overflow:auto;border:1px solid var(--borde);border-radius:10px;padding:8px">{checks}</div>
        <div style="height:14px"></div>
        <button>{icon('save')} Guardar reserva recurrente</button>
      </form>
    </div>
    <div class="card">
      <h2>{icon('list')} Reservas recurrentes</h2>
      <ul class="lista">{filas}</ul>
    </div>
    <script>
    var SEDES_REC = {json.dumps({k: {"horas": v["horas"], "canchas": v["canchas"], "zonas": v["zonas"]} for k, v in SEDES.items()})};
    function filtrarRec(){{
      var q=(document.getElementById('jugfiltro').value||'').toLowerCase();
      document.querySelectorAll('#juglista .jugitem').forEach(function(el){{
        el.style.display = el.getAttribute('data-n').indexOf(q)>-1 ? 'flex':'none';
      }});
    }}
    function alElegirRec(e){{
      // al tildar un jugador se limpia la búsqueda para elegir el siguiente
      if(!e.target.checked) return;
      var f=document.getElementById('jugfiltro');
      if(f && f.value){{ f.value=''; filtrarRec(); f.focus(); }}
    }}
    function cambiarSedeRec(){{
      var s = SEDES_REC[document.querySelector('input[name=sede]:checked').value];
      document.querySelector('select[name=hora]').innerHTML = s.horas.map(h=>'<option>'+h+'</option>').join('');
      var o = "<optgroup label='Cancha especifica'>" + s.canchas.map(c=>"<option value='"+c[0]+"'>"+c[1]+"</option>").join('') + "</optgroup>";
      o += "<optgroup label='Por zona'>" + s.zonas.map(z=>"<option value='"+z[0]+"'>"+z[1]+"</option>").join('') + "</optgroup>";
      o += "<option value=''>Cualquiera libre</option>";
      document.querySelector('select[name=cancha]').innerHTML = o;
    }}
    </script>"""
    return pagina("Recurrentes", "recurrentes", cuerpo)


@app.route("/recurrentes/guardar", methods=["POST"])
@login_requerido
def recurrente_guardar():
    f = request.form
    sede = f.get("sede", "dia")
    cfg = SEDES.get(sede, SEDES["dia"])
    hora = f.get("hora", "").strip()
    jugs = [int(x) for x in f.getlist("jug") if str(x).strip().isdigit()]
    jugs = list(dict.fromkeys(jugs))
    if hora not in cfg["horas"]:
        flash("Esa hora no corresponde al turno elegido.")
        return redirect(url_for("recurrentes"))
    if not f.get("reservador", "").strip():
        flash("Elegí el reservador.")
        return redirect(url_for("recurrentes"))
    if len(jugs) != 3:
        flash("Elegí exactamente 3 jugadores acompañantes.")
        return redirect(url_for("recurrentes"))
    try:
        dia = int(f.get("dia_semana", "0"))
        assert 0 <= dia <= 6
    except (ValueError, AssertionError):
        flash("Día inválido.")
        return redirect(url_for("recurrentes"))
    with rb._io_lock:
        data = rb.leer_json("recurrentes.json", {"recurrentes": []})
        lista = data.get("recurrentes", [])
        lista.append({
            "id": f"rec-{secrets.token_hex(4)}", "dia_semana": dia, "hora": hora,
            "club_id": cfg["club_id"], "cancha": f.get("cancha", "").strip(),
            "reservador": f.get("reservador", "").strip(), "jugadores": jugs,
            "creado_por": session.get("usuario", ""), "activa": True,
        })
        data["recurrentes"] = lista
        rb.escribir_json("recurrentes.json", data)
    flash(f"🔁 Lista: todos los {DIAS_SEMANA[dia].lower()} a las {hora} se carga sola.")
    return redirect(url_for("recurrentes"))


def _mi_recurrente(rid):
    """Devuelve (lista, indice) si el usuario puede tocar esa recurrente."""
    lista = rb.leer_json("recurrentes.json", {"recurrentes": []}).get("recurrentes", [])
    i = next((k for k, r in enumerate(lista) if r.get("id") == rid), None)
    if i is None:
        return None, None
    if not puede("todas_reservas") and lista[i].get("creado_por") != session.get("usuario"):
        return None, None
    return lista, i


@app.route("/recurrentes/pausar/<rid>", methods=["POST"])
@login_requerido
def recurrente_pausar(rid):
    with rb._io_lock:
        lista, i = _mi_recurrente(rid)
        if lista is None:
            flash("No podés tocar esa reserva recurrente.")
            return redirect(url_for("recurrentes"))
        lista[i]["activa"] = not lista[i].get("activa", True)
        rb.escribir_json("recurrentes.json", {"recurrentes": lista})
        estado = "reactivada" if lista[i]["activa"] else "pausada"
    flash(f"Reserva recurrente {estado}.")
    return redirect(url_for("recurrentes"))


@app.route("/recurrentes/borrar/<rid>", methods=["POST"])
@login_requerido
def recurrente_borrar(rid):
    with rb._io_lock:
        lista, i = _mi_recurrente(rid)
        if lista is None:
            flash("No podés borrar esa reserva recurrente.")
            return redirect(url_for("recurrentes"))
        lista.pop(i)
        rb.escribir_json("recurrentes.json", {"recurrentes": lista})
    flash("Reserva recurrente borrada (las ya cargadas siguen igual).")
    return redirect(url_for("recurrentes"))


@app.route("/encuesta")
@login_requerido
def encuesta_panel():
    """Panel de la encuesta semanal: abrirla, ver quién votó y cerrarla."""
    e = enc.cargar_encuesta()
    abierta = enc.esta_abierta(e)

    if not e:
        cuerpo = f"""
        <div class="card">
          <h2>{icon('list')} Encuesta semanal</h2>
          <p><small class="hint">Se manda un link al grupo de WhatsApp para que cada uno diga
            qué días y horarios puede jugar, y si presta su carnet. No necesitan instalar nada
            ni entrar acá.</small></p>
          <a class="btn" href="{url_for('encuesta_nueva')}">{icon('plus')} Abrir la encuesta de esta semana</a>
        </div>"""
        return pagina("Encuesta", "encuesta", cuerpo)

    r = enc.resumen_votos(e)
    link = url_encuesta(e["token"])

    # --- quién votó y quién falta ---
    def chips_voto(v):
        voto = v.get("voto") or {}
        partes = []
        for d in e.get("dias", enc.DIAS_POR_DEFECTO):
            info = (voto.get("dias") or {}).get(str(d))
            dia3 = enc.DIAS[d][:3]
            if not info:
                continue
            if info["modo"] == enc.MODO_JUEGO:
                horas = " ".join(str(int(h[:2])) for h in info.get("horas", []))
                partes.append(f'<span class="pill reservada">{dia3}: {horas}</span>')
            elif info["modo"] == enc.MODO_PRESTA:
                partes.append(f'<span class="pill programada">{dia3}: presta</span>')
            else:
                partes.append(f'<span class="pill vencida">{dia3}: no</span>')
        return " ".join(partes) or '<span class="pill vencida">sin respuesta</span>'

    filas_ok = "".join(
        f'<li class="reserva"><div class="sp"><div class="titulo"><span>{esc(v["nombre"])}</span></div>'
        f'<div class="meta">{chips_voto(v)}</div></div></li>' for v in r["votaron"])
    filas_falta = "".join(
        f'<li><div class="sp">{esc(v["nombre"])}</div></li>' for v in r["faltan"])

    # --- por día: disponibilidad + carnets ---
    tablas = ""
    for d, info in r["por_dia"].items():
        filas = ""
        for h, gente in info["por_hora"].items():
            n = len(gente)
            canchas = enc.canchas_para(n)
            tope = min(canchas, info["canchas_posibles"])   # limitado por los carnets
            aviso = (f' <small class="hint">(los carnets alcanzan para {tope})</small>'
                     if tope < canchas else "")
            filas += (f'<tr><td class="que">{int(h[:2])} h</td><td><b>{n}</b></td>'
                      f'<td><span class="pill {"reservada" if tope else "vencida"}">'
                      f'{tope} cancha(s)</span>{aviso}</td>'
                      f'<td class="que"><small class="hint">{esc(", ".join(gente))}</small></td></tr>')
        if not filas:
            filas = '<tr><td class="que" colspan="4"><small class="hint">Nadie se anotó todavía.</small></td></tr>'
        tablas += f"""<div style="margin-top:16px">
          <b>{enc.DIAS[d]}</b>
          <div style="margin:6px 0 8px"><small class="hint">
            Carnets disponibles: <b>{len(info['bolsa'])}</b>
            ({len(info['juegan'])} juegan · {len(info['prestan'])} prestan · {len(info['extra'])} de afuera)
            → alcanzan para <b>{info['canchas_posibles']}</b> cancha(s)</small></div>
          <div style="overflow-x:auto"><table class="permisos" style="min-width:340px">
          <thead><tr><th>Hora</th><th>Anotados</th><th>Da para</th><th>Quiénes</th></tr></thead>
          <tbody>{filas}</tbody></table></div></div>"""

    estado_chip = ('<span class="pill reservada">Abierta</span>' if abierta
                   else '<span class="pill vencida">Cerrada</span>')
    n_votos = len(r["votaron"])
    conf_borrar = ("¿Borrar la encuesta"
                   + (f" y sus {n_votos} voto(s)" if n_votos else "")
                   + "? El link deja de funcionar y no se puede deshacer.")
    acciones = ""
    if abierta:
        acciones = (f'<form method="post" action="{url_for("encuesta_cerrar")}" style="display:inline"'
                    f' onsubmit="return confirm(\'¿Cerrar la encuesta? Nadie va a poder votar más.\')">'
                    f'<button class="sec">Cerrar ahora</button></form>')
    acciones += (f'<a class="btn sec" href="{url_for("encuesta_participantes")}">'
                 f'{icon("users")} Participantes</a>'
                 f'<a class="btn sec" href="{url_for("encuesta_nueva")}">Nueva encuesta</a>'
                 f'<form method="post" action="{url_for("encuesta_borrar")}" style="display:inline"'
                 f' onsubmit="return confirm(\'{conf_borrar}\')">'
                 f'<button class="danger">{icon("trash")} Borrar encuesta</button></form>')

    cuerpo = f"""
    <div class="card">
      <h2>{icon('list')} {esc(e.get('titulo'))} {estado_chip}</h2>
      <p><small class="hint">Pegá este link en el grupo de WhatsApp. Cierra el {esc(e.get('cierre_txt'))}.</small></p>
      <div class="row" style="align-items:center;gap:8px">
        <div><input id="link" value="{link}" readonly onclick="this.select()"></div>
        <div style="flex:0"><button type="button" onclick="copiar(this)">Copiar</button></div>
      </div>
      <div style="margin-top:10px">
        <a class="btn dark" target="_blank" rel="noopener"
           href="https://wa.me/?text={_url_wa(e, link)}">Compartir por WhatsApp</a>
        <a class="btn sec" target="_blank" rel="noopener" href="{link}">Ver la encuesta</a>
      </div>
      <div style="height:12px"></div>
      {acciones}
    </div>

    <div class="card">
      <h2>{icon('users')} Votaron {len(r['votaron'])} de {r['total']}
        <small class="hint" style="font-weight:400">· <a href="{url_for('encuesta_participantes')}">cambiar quiénes participan</a></small></h2>
      <ul class="lista">{filas_ok or '<li class="vacio">Todavía no votó nadie.</li>'}</ul>
      {f'<h2 style="margin-top:16px">{icon("clock")} Faltan {len(r["faltan"])}</h2><ul class="lista compacta">{filas_falta}</ul>' if r['faltan'] else ''}
    </div>

    <div class="card">
      <h2>{icon('chart')} Disponibilidad por día</h2>
      <p><small class="hint">4 a 7 jugadores = 1 cancha · 8 a 11 = 2 · 12 o más = 3.
        Y siempre limitado por los carnets que haya ese día (4 por cancha).</small></p>
      <div><a class="btn sec" href="{url_for('encuesta_carnets')}">{icon('plus')} Sumar carnets de afuera</a></div>
      {tablas}
    </div>

    <div class="card">
      <h2>{icon('check')} Armar el cronograma</h2>
      <p><small class="hint">Reparte los carnets entre las canchas y deja listos los cambios de
         nombres. Te muestra la propuesta antes de programar nada.</small></p>
      <a class="btn" href="{url_for('encuesta_cronograma')}">Ver la propuesta</a>
    </div>
    <script>
    function copiar(b){{
      var i=document.getElementById('link'); i.select(); i.setSelectionRange(0,99999);
      navigator.clipboard.writeText(i.value).then(function(){{
        var t=b.textContent; b.textContent='¡Copiado!';
        setTimeout(function(){{b.textContent=t;}},1500);
      }});
    }}
    </script>"""
    return pagina("Encuesta", "encuesta", cuerpo)


def url_encuesta(token):
    """Link público de la encuesta. Usa su dominio propio si está configurado, para que
    lo que se pega en WhatsApp no delate la app de reservas."""
    if DOMINIO_ENCUESTA:
        return f"https://{DOMINIO_ENCUESTA}/e/{token}"
    return request.url_root.rstrip("/") + f"/e/{token}"


def _url_wa(e, link):
    from urllib.parse import quote
    return quote(f"🎾 ¿Jugás este finde? Decime qué horarios podés (y si prestás el carnet). "
                 f"Se cierra el {e.get('cierre_txt','')}: {link}")


def _pantalla_participantes(titulo, acción, preseleccion, texto_boton, aviso="",
                            excluir=None, ayuda=None):
    """Checkboxes con la gente de las bases. Sirve para elegir participantes de la
    encuesta y también los carnets extra (ahí se excluye a los que ya participan)."""
    fuera = {str(x) for x in (excluir or [])}
    todos = [j for j in enc.todos_los_jugadores() if j["id"] not in fuera]
    sel = {str(x) for x in (preseleccion or [])}
    items = "".join(
        f"<label class='jugitem' data-n='{esc(j['nombre'].lower())}' "
        f"style='display:flex;gap:9px;align-items:center'>"
        f"<input type='checkbox' name='p' value='{esc(j['id'])}'"
        f"{' checked' if j['id'] in sel else ''}> {esc(j['nombre'])}</label>"
        for j in todos)
    return f"""
    <div class="card">
      <h2>{icon('users')} {titulo}</h2>
      <p><small class="hint">{ayuda or "Solo los que marques van a poder votar. Los demás, aunque tengan el link, no aparecen en la lista."}</small></p>
      {aviso}
      <form method="post" action="{acción}">
        <div class="row" style="align-items:center;gap:8px;margin-bottom:8px">
          <div><input id="filtro" placeholder="🔍 buscar…" oninput="filtrar()"></div>
          <div style="flex:0"><button type="button" class="sec" onclick="marcar(true)">Todos</button></div>
          <div style="flex:0"><button type="button" class="sec" onclick="marcar(false)">Ninguno</button></div>
        </div>
        <div id="lista" style="display:flex;flex-direction:column;gap:7px;max-height:340px;
             overflow:auto;border:1px solid var(--borde);border-radius:10px;padding:10px">{items}</div>
        <div style="margin-top:8px"><small class="hint"><b id="cuenta">0</b> elegidos de {len(todos)}</small></div>
        <div style="height:14px"></div>
        <button>{icon('check')} {texto_boton}</button>
        <a class="btn sec" href="{url_for('encuesta_panel')}">Cancelar</a>
      </form>
    </div>
    <script>
    function filtrar(){{
      var q=(document.getElementById('filtro').value||'').toLowerCase();
      document.querySelectorAll('#lista .jugitem').forEach(function(el){{
        el.style.display = el.getAttribute('data-n').indexOf(q)>-1 ? 'flex':'none';
      }});
    }}
    function marcar(v){{
      document.querySelectorAll('#lista input:not([style*="none"])').forEach(function(c){{
        if(c.closest('.jugitem').style.display!=='none') c.checked=v;
      }});
      contar();
    }}
    function contar(){{
      document.getElementById('cuenta').textContent =
        document.querySelectorAll('#lista input:checked').length;
    }}
    document.addEventListener('change', function(e){{ if(e.target.name==='p') contar(); }});
    document.addEventListener('DOMContentLoaded', contar);
    </script>"""


@app.route("/encuesta/nueva")
@permiso_requerido("encuesta")
def encuesta_nueva():
    """Elegir a quiénes se les manda la encuesta (arranca con los de la anterior)."""
    anterior = enc.cargar_encuesta().get("participantes")
    if not anterior:                       # la primera vez: todos preseleccionados
        anterior = [j["id"] for j in enc.todos_los_jugadores()]
    aviso = ('<div class="nota info">Vienen marcados los mismos de la encuesta anterior.</div>'
             if enc.cargar_encuesta().get("participantes") else "")
    cuerpo = _pantalla_participantes("¿A quiénes les mandás la encuesta?",
                                     url_for("encuesta_abrir"), anterior,
                                     "Abrir la encuesta", aviso)
    return pagina("Encuesta", "encuesta", cuerpo)


@app.route("/encuesta/participantes", methods=["GET", "POST"])
@permiso_requerido("encuesta")
def encuesta_participantes():
    """Agregar o sacar gente de una encuesta que ya está abierta."""
    e = enc.cargar_encuesta()
    if not e:
        return redirect(url_for("encuesta_panel"))
    if request.method == "GET":
        actuales = e.get("participantes") or [j["id"] for j in enc.todos_los_jugadores()]
        cuerpo = _pantalla_participantes("Participantes de la encuesta",
                                         url_for("encuesta_participantes"), actuales,
                                         "Guardar cambios")
        return pagina("Encuesta", "encuesta", cuerpo)
    elegidos = request.form.getlist("p")
    if not elegidos:
        flash("Elegí al menos una persona.")
        return redirect(url_for("encuesta_participantes"))
    e["participantes"] = elegidos
    enc.guardar_encuesta(e)
    flash(f"Listo: {len(elegidos)} participantes.")
    return redirect(url_for("encuesta_panel"))


@app.route("/encuesta/carnets", methods=["GET", "POST"])
@permiso_requerido("encuesta")
def encuesta_carnets():
    """Carnets prestados por gente que NO está en la encuesta (de afuera del grupo).
    Se suman a la bolsa con la que se arman las reservas."""
    e = enc.cargar_encuesta()
    if not e:
        return redirect(url_for("encuesta_panel"))
    if request.method == "GET":
        cuerpo = _pantalla_participantes(
            "Carnets prestados de afuera",
            url_for("encuesta_carnets"),
            e.get("carnets_extra") or [],
            "Guardar carnets",
            excluir=e.get("participantes") or [],
            ayuda=("Gente que no está en la encuesta pero que igual nos presta el carnet. "
                   "Se suman a la bolsa para reservar (no se les pide que voten)."))
        return pagina("Encuesta", "encuesta", cuerpo)
    e["carnets_extra"] = request.form.getlist("p")
    enc.guardar_encuesta(e)
    flash(f"{len(e['carnets_extra'])} carnet(s) prestados de afuera.")
    return redirect(url_for("encuesta_panel"))


@app.route("/encuesta/abrir", methods=["POST"])
@permiso_requerido("encuesta")
def encuesta_abrir():
    elegidos = request.form.getlist("p")
    if not elegidos:
        flash("Elegí al menos una persona para mandarle la encuesta.")
        return redirect(url_for("encuesta_nueva"))
    hoy = hoy_ar()
    # cierra el jueves a la noche (23:59); si hoy ya es viernes o después, el jueves que viene
    jueves = enc._proximo(3, hoy)
    cierre = datetime.datetime.combine(jueves, datetime.time(23, 59))
    sab, dom = enc._proximo(5, hoy), enc._proximo(6, hoy)
    nueva = {
        "id": f"enc-{hoy.isoformat()}-{secrets.token_hex(2)}",
        "token": secrets.token_urlsafe(9),
        "titulo": f"Finde del {sab.strftime('%d/%m')} y {dom.strftime('%d/%m')}",
        "dias": enc.DIAS_POR_DEFECTO,
        "horas": enc.HORAS_POR_DEFECTO,   # solo los horarios en que juegan
        "fechas": {"5": sab.isoformat(), "6": dom.isoformat()},
        "estado": "abierta",
        "cierra_epoch": _epoch_ar(cierre),
        "cierre_txt": f"jueves {jueves.strftime('%d/%m')} a la noche",
        "creada": ahora_ar().strftime("%Y-%m-%d %H:%M"),
        "creada_por": session.get("usuario", ""),
        "participantes": elegidos,          # solo esta gente puede votar
    }
    enc.guardar_encuesta(nueva)
    flash("Encuesta abierta. Copiá el link y pegalo en el grupo.")
    return redirect(url_for("encuesta_panel"))


@app.route("/encuesta/cerrar", methods=["POST"])
@permiso_requerido("encuesta")
def encuesta_cerrar():
    e = enc.cargar_encuesta()
    if e:
        e["estado"] = "cerrada"
        e["cerrada"] = ahora_ar().strftime("%Y-%m-%d %H:%M")
        enc.guardar_encuesta(e)
        flash("Encuesta cerrada.")
    return redirect(url_for("encuesta_panel"))


@app.route("/encuesta/borrar", methods=["POST"])
@permiso_requerido("encuesta")
def encuesta_borrar():
    """Borra la encuesta y sus votos. El link deja de funcionar al instante."""
    e = enc.cargar_encuesta()
    if not e:
        return redirect(url_for("encuesta_panel"))
    with rb._io_lock:
        votos = rb.leer_json("votos.json", {"votos": {}})
        cuantos = len(votos.get("votos", {}).pop(e.get("id", ""), {}))
        rb.escribir_json("votos.json", votos)
        rb.escribir_json("encuesta_config.json", {})
    flash(f"Encuesta borrada (se perdieron {cuantos} voto(s)). El link ya no funciona.")
    return redirect(url_for("encuesta_panel"))


# ======================================================================
#  ARMADOR DE CRONOGRAMA: de los votos al flujo de reservas del finde
#  La lógica vive en encuesta.armar_cronograma() (modelo explicado en
#  ARMADOR_CRONOGRAMA.md): todas las reservas del sábado se hacen el
#  viernes y las del domingo el sábado; los huecos se pisan con carnets
#  prestados y los cambios corren apenas se liberan los que entran.
# ======================================================================
def _plural(n, singular, plural):
    return f"{n} {singular if n == 1 else plural}"


def _apertura_pasada(rsv):
    ap = (datetime.datetime.fromisoformat(f"{rsv['fecha']}T{rsv['hora']}:00")
          - datetime.timedelta(minutes=24 * 60 - 1))
    return ap <= ahora_ar()


def _tarjeta_reserva(rsv):
    nombres = {x["id"]: x["nombre"] for x in rsv["juegan"] + rsv["titulares"]}
    tit = "".join(
        f'<span class="pill {"programada" if x["pisa"] else "reservada"}">'
        f'{esc(x["nombre"])}{" (pisa)" if x["pisa"] else ""}</span> '
        for x in rsv["titulares"])
    lineas = ""
    c = rsv.get("cambio")
    if c:
        entran = ", ".join(esc(nombres.get(p, p)) for p in c["entran"])
        lineas += (f'<div style="margin-top:6px"><small class="hint">↻ Cambio el '
                   f'<b>{esc(c["hora_txt"])}</b>: entran {entran} · la retoma '
                   f'<b>{esc(c["reservador"])}</b></small></div>')
    lineas += (f'<div><small class="hint">Juegan: '
               f'{esc(", ".join(x["nombre"] for x in rsv["juegan"]))}'
               + (f' · rotan: {esc(", ".join(x["nombre"] for x in rsv["rotan"]))}'
                  if rsv["rotan"] else "") + '</small></div>')
    for a in rsv["avisos"]:
        lineas += f'<div class="nota warn">{icon("alert")} {esc(a)}</div>'
    vencida = (' <span class="pill vencida">apertura pasada</span>'
               if _apertura_pasada(rsv) else "")
    return f"""<div class="reserva" style="margin-bottom:8px">
      <div class="titulo"><span>{int(rsv['hora'][:2])} h · Cancha {rsv['cancha_num']}</span>
        <span class="pill">{esc(rsv['reservador']['nombre'])} reserva</span>{vencida}</div>
      <div class="meta">{tit}</div>{lineas}</div>"""


@app.route("/encuesta/cronograma")
@permiso_requerido("encuesta")
def encuesta_cronograma():
    e = enc.cargar_encuesta()
    if not e:
        flash("Primero abrí una encuesta.")
        return redirect(url_for("encuesta_panel"))
    plan = enc.armar_cronograma(e)

    bloques, orden = {}, []
    for rsv in plan["reservas"]:
        b = rsv["bloque"]
        if b not in bloques:
            bloques[b] = []
            orden.append(b)
        bloques[b].append(rsv)
    cuerpo_bloques = "".join(
        f'<div class="card"><h2>{esc(b)} · {_plural(len(bloques[b]), "cancha", "canchas")}</h2>'
        f'<p><small class="hint">Se reserva a la apertura ({esc(bloques[b][0]["apertura_txt"])}).'
        f'</small></p>{"".join(_tarjeta_reserva(r) for r in bloques[b])}</div>'
        for b in orden) or ('<div class="card"><p><small class="hint">Con los votos '
                            'cargados no sale ninguna cancha.</small></p></div>')

    extras = ""
    if plan["sin_vincular"]:
        extras += (f'<div class="nota warn">{icon("alert")} Sin vincular con su carnet: '
                   f'<b>{esc(", ".join(plan["sin_vincular"]))}</b> — en '
                   f'<a href="{url_for("jugadores")}">Jugadores</a> tienen que figurar '
                   f'con el mismo nombre que en Reservadores.</div>')
    for a in plan["avisos"]:
        extras += f'<div class="nota warn">{icon("alert")} {esc(a)}</div>'
    if plan["sobran"]:
        extras += (f'<div class="nota info">{icon("check")} Quedan sin usar '
                   f'{_plural(len(plan["sobran"]), "carnet", "carnets")}: '
                   f'{esc(", ".join(plan["sobran"]))}. Si hace falta, dan para ampliar '
                   f'alguna mañana.</div>')

    roto = any("VERIFICACIÓN" in a for r in plan["reservas"] for a in r["avisos"])
    n = len([r for r in plan["reservas"] if not _apertura_pasada(r)])
    cambios = len([r for r in plan["reservas"]
                   if r.get("cambio") and not _apertura_pasada(r)])
    if roto:
        boton = (f'<div class="nota err">{icon("alert")} La verificación encontró un '
                 f'error en el plan: no se puede programar así.</div>')
    elif not n:
        boton = '<p><small class="hint">No hay ninguna cancha para programar.</small></p>'
    else:
        txt = _plural(n, "reserva", "reservas") + (
            f" y {_plural(cambios, 'cambio de carnets', 'cambios de carnets')}"
            if cambios else "")
        boton = f"""
      <form method="post" action="{url_for('encuesta_cronograma_programar')}"
            onsubmit="return confirm('¿Programar {txt}? Cada reserva se dispara sola a su apertura.')">
        <button>{icon('check')} Programar {txt}</button>
      </form>"""

    cuerpo = f"""
    <div class="card">
      <h2>{icon('list')} Cronograma del finde</h2>
      <p><small class="hint">Armado con los votos: las canchas se pisan con los carnets
         prestados y los cambios corren solos apenas se libera el carnet de cada uno.
         Nada se programa hasta que toques el botón de abajo.</small></p>
      {extras}
    </div>
    {cuerpo_bloques}
    <div class="card">
      {boton}
      <a class="btn sec" href="{url_for('encuesta_panel')}">Volver a la encuesta</a>
    </div>"""
    return pagina("Cronograma", "encuesta", cuerpo)


@app.route("/encuesta/cronograma/programar", methods=["POST"])
@permiso_requerido("encuesta")
def encuesta_cronograma_programar():
    """Convierte el plan en reservas programadas de verdad, cada una con su cambio de
    carnets. Se rearma acá mismo para no confiar en lo que viaje por el form."""
    from werkzeug.datastructures import MultiDict
    e = enc.cargar_encuesta()
    if not e:
        return redirect(url_for("encuesta_panel"))
    plan = enc.armar_cronograma(e)
    if any("VERIFICACIÓN" in a for r in plan["reservas"] for a in r["avisos"]):
        flash("La verificación encontró un error en el plan: no se programó nada.")
        return redirect(url_for("encuesta_cronograma"))

    creadas, cambios, tarde, errores = 0, 0, 0, []
    for rsv in plan["reservas"]:
        if _apertura_pasada(rsv):
            tarde += 1
            continue
        f = MultiDict([("sede", SEDE_POR_CLUB.get(rsv["club_id"], "dia")),
                       ("fecha", rsv["fecha"]), ("hora", rsv["hora"]),
                       ("reservador", rsv["reservador"]["nombre"]), ("cancha", "")]
                      + [("jug", x["id"]) for x in rsv["titulares"]])
        try:
            rid = _guardar_reserva_desde_form(f)
        except Exception as ex:
            errores.append(f"{rsv['bloque']} {rsv['hora']}: {ex}")
            continue
        creadas += 1
        if rsv.get("cambio"):
            _colgar_cambio_del_cronograma(rid, rsv["cambio"])
            cambios += 1

    if creadas:
        flash(f"Listo: {creadas} reserva(s) programada(s)"
              + (f" con {cambios} cambio(s) de carnets." if cambios else "."))
    if tarde:
        flash(f"{tarde} reserva(s) quedaron afuera: su apertura ya pasó.")
    if errores:
        flash("No pude programar: " + " · ".join(errores[:3]))
    if not creadas and not errores and not tarde:
        flash("No había nada para programar.")
    return redirect(url_for("reservas"))


def _colgar_cambio_del_cronograma(rid, c):
    """Deja el cambio de carnets colgado de la reserva, con su hora propia (ej_epoch):
    corre apenas se liberan los carnets de los que entran, no 10 min antes del turno.
    El ejecutor solo lo corre si para entonces la reserva está confirmada."""
    with rb._io_lock:
        lista = cargar("reservas.json", "reservas")
        for x in lista:
            if x.get("id") == rid:
                x["cambio_prog"] = {
                    "reservador": c["reservador"],
                    "jugadores": [int(p) for p in c["jugadores"]],
                    "estado": "pendiente", "detalle": "",
                    "hora_ej": c["hora_txt"], "ej_epoch": c["epoch"],
                    "creado_por": session.get("usuario", ""),
                }
                break
        guardar("reservas.json", "reservas", lista)


@app.route("/historial")
@login_requerido
def historial():
    """Qué pasó con las reservas ya terminadas: cuántas entraron, cuáles no, y en qué cancha."""
    regs = rb.leer_json("historial.json", {"historial": []}).get("historial", [])
    regs = sorted(regs, key=lambda r: (str(r.get("fecha")), str(r.get("hora"))), reverse=True)
    es_admin = puede("todas_reservas")
    yo = session.get("usuario", "")
    if not es_admin:                     # cada uno ve lo suyo; el admin ve todo
        # comparación tolerante (mayúsculas/espacios): el nombre para mostrar puede variar
        regs = [r for r in regs if rb._norm(r.get("creado_por")) == rb._norm(yo)]

    total = len(regs)
    ok = sum(1 for r in regs if r.get("estado") == "reservada")
    pct = round(ok * 100 / total) if total else 0
    # ¿cuántas veces entró en la cancha que se pidió?
    con_pedida = [r for r in regs if r.get("estado") == "reservada" and str(r.get("cancha_pedida", "")).isdigit()]
    justas = sum(1 for r in con_pedida
                 if str(r.get("cancha_real", "")).split("-")[0].lstrip("0") == str(r.get("cancha_pedida")))
    pct_justas = round(justas * 100 / len(con_pedida)) if con_pedida else 0

    tarjetas = f"""
    <div class="row" style="gap:10px">
      <div class="stat"><b>{total}</b><small>reservas</small></div>
      <div class="stat ok"><b>{ok}</b><small>entraron ({pct}%)</small></div>
      <div class="stat err"><b>{total - ok}</b><small>no entraron</small></div>
      <div class="stat"><b>{pct_justas}%</b><small>en la cancha pedida</small></div>
    </div>"""

    filas = ""
    for r in regs[:100]:
        entro = r.get("estado") == "reservada"
        chip = (f'<span class="pill reservada">{icon("check")} Entró</span>' if entro
                else f'<span class="pill fallada">{icon("x")} No entró</span>')
        real = str(r.get("cancha_real", ""))
        real_num = real.split("-")[0].lstrip("0") if "-" in real else real
        ped = str(r.get("cancha_pedida", ""))
        cancha = desc_cancha(real_num) if real_num else (desc_cancha(ped) if ped else "—")
        aviso = ""
        if entro and ped.isdigit() and real_num and real_num != ped:
            aviso = f' <span class="nota warn">{icon("alert")} pedías la {esc(ped)}</span>'
        sede = "Noche" if r.get("club_id") == 504 else "Día"
        jugs = ", ".join(str(esc(x)) for x in (r.get("jugadores") or []))
        filas += f"""<li class="reserva"><div class="sp">
          <div class="titulo"><span>{fmt_fecha(r.get('fecha'))} · {esc(r.get('hora'))}</span>{chip}</div>
          <div class="meta"><span>{icon('sun' if sede=='Día' else 'moon')} {sede}</span>
            <span>{icon('pin')} {cancha}{aviso}</span>
            <span>{icon('users')} {esc(r.get('reservador'))}{(', ' + jugs) if jugs else ''}</span></div>
          {f'<small class="hint">{esc(r.get("detalle"))}</small>' if r.get('detalle') else ''}
        </div></li>"""
    if not filas:
        filas = (f'<li class="vacio">{icon("inbox")}<div>Todavía no hay historial.<br>'
                 f'Se va llenando solo a medida que el bot dispara reservas.</div></li>')

    cuerpo = f"""
    <div class="card">
      <h2>{icon('chart')} Resumen{'' if es_admin else ' (tus reservas)'}</h2>
      {tarjetas}
    </div>
    <div class="card">
      <h2>{icon('list')} Últimas reservas</h2>
      <ul class="lista">{filas}</ul>
    </div>"""
    return pagina("Historial", "historial", cuerpo)


@app.route("/api/buscar")
@permiso_requerido("agregar_jugadores")
def api_buscar():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"jugadores": []})
    if not rate_ok("ondepor:" + (request.remote_addr or "?"), maximo=20, ventana_seg=60):
        return jsonify({"error": "Demasiadas búsquedas seguidas; esperá unos segundos."})
    res = reservador_con_clave()
    if not res:
        return jsonify({"error": "Cargá un reservador con clave primero."})
    try:
        api = OnDepor(res["usuario"], res["clave"], CLUB_DEFAULT)
        api.login()
        return jsonify({"jugadores": api.buscar_jugadores_lista(q)})
    except Exception as e:
        return jsonify({"error": str(e)[:200]})


@app.route("/jugadores/agregar", methods=["POST"])
@permiso_requerido("agregar_jugadores")
def jugador_agregar():
    data = request.get_json(force=True, silent=True) or {}
    pid, nombre = data.get("id"), limpiar_nombre(data.get("nombre"))
    if not pid or not str(pid).strip().isdigit():
        return ("id inválido", 400)
    pid = int(pid)
    if not nombre:
        return ("falta nombre", 400)
    with rb._io_lock:   # AJAX: clics rápidos pueden pisarse -> leer-modificar-escribir atómico
        lista = cargar("db_jugadores.json", "jugadores")
        if not any(str(j.get("player_id")) == str(pid) for j in lista):
            lista.append({"nombre": nombre, "player_id": pid})
            guardar("db_jugadores.json", "jugadores", lista,
                    ayuda="Jugadores (compañeros) con su player_id de OnDepor.")
    return ("ok", 200)


@app.route("/jugadores/borrar/<pid>", methods=["POST"])
@permiso_requerido("gestionar_bases")
def jugador_borrar(pid):
    with rb._io_lock:
        lista = [j for j in cargar("db_jugadores.json", "jugadores")
                 if str(j.get("player_id")) != str(pid)]
        guardar("db_jugadores.json", "jugadores", lista,
                ayuda="Jugadores (compañeros) con su player_id de OnDepor.")
    flash("Jugador quitado.")
    return redirect(url_for("jugadores"))


# ----------------------------------------------------------------------
#  RESERVADORES
# ----------------------------------------------------------------------
@app.route("/reservadores")
@login_requerido
def reservadores():
    lista = cargar("db_reservadores.json", "reservadores")
    es_admin = puede("gestionar_bases")
    edit_idx = request.args.get("editar")
    ed = None
    if edit_idx is not None and edit_idx.isdigit() and int(edit_idx) < len(lista):
        ed = lista[int(edit_idx)]

    filas = ""
    # orden alfabético solo para MOSTRAR; 'i' sigue siendo el índice real en el archivo
    # (editar/borrar/probar login referencian por índice)
    for i, r in sorted(enumerate(lista), key=lambda t: str(t[1].get("nombre", "")).lower()):
        sin_clave = str(r.get("clave", "")).startswith("PONER") or not r.get("clave")
        chip = ('<span class="pill vencida">sin clave</span>' if sin_clave
                else f'<span class="pill reservada">{icon("check")} ok</span>')
        borrar = ('<form method="post" action="' + url_for("reservador_borrar", idx=i)
                  + '" onsubmit="return confirm(\'¿Borrar este reservador?\')">'
                  + f'<button class="danger btn-ico" aria-label="Borrar reservador" title="Borrar reservador">{icon("trash")}</button></form>') if es_admin else ""
        editar = ((f'<a class="btn sec btn-ico" aria-label="Editar" title="Editar" href="'
                  + url_for("reservadores") + '?editar=' + str(i) + f'">{icon("edit")}</a>')
                  if es_admin else "")
        probar = (f'<button class="sec" onclick="probar({i},this)">Probar</button>') if es_admin else ""
        filas += (f'<li><div class="sp">{esc(r.get("nombre"))} {chip}<br><small class="hint">{esc(r.get("usuario"))}</small></div>'
                  f'<div class="acc">{editar}{probar}{borrar}</div></li>')
    if not filas:
        filas = (f'<li class="vacio">{icon("user")}<div>Todavía no cargaste reservadores.<br>'
                 f'El reservador es el que se loguea en OnDepor y toma el turno.</div></li>')

    tituloR = f"Editar reservador: {esc(ed.get('nombre'))}" if ed else "Agregar reservador (el que reserva = jugador 1)"
    v_nombre = esc(ed.get("nombre", "")) if ed else ""
    v_tel = esc(ed.get("telefono", "")) if ed else ""
    v_usuario = esc(ed.get("usuario", "")) if ed else ""
    v_orig = esc(ed.get("usuario", "")) if ed else ""
    ph_clave = "(dejá vacío para no cambiarla)" if ed else "contraseña de OnDepor"
    cancelarR = ('<a class="btn sec" style="text-decoration:none" href="' + url_for("reservadores") + '">Cancelar</a>') if ed else ""

    form_alta = f"""
    <div class="card">
      <h2>{icon('edit') if ed else icon('plus')} {tituloR}</h2>
      <form method="post" action="{url_for('reservador_guardar')}">
        <input type="hidden" name="orig" value="{v_orig}">
        <div class="row">
          <div><label>Nombre</label><input name="nombre" value="{v_nombre}"></div>
          <div><label>Teléfono (opcional)</label><input name="telefono" value="{v_tel}"></div>
        </div>
        <label>Usuario / email de OnDepor</label><input name="usuario" value="{v_usuario}" autocapitalize="off">
        <label>Contraseña de OnDepor</label><input name="clave" type="password" placeholder="{ph_clave}">
        <div style="height:14px"></div>
        <button>{icon('save')} {'Guardar cambios' if ed else 'Guardar'}</button> {cancelarR}
      </form>
    </div>"""

    cuerpo = (form_alta if es_admin else "") + f"""
    <div class="card">
      <h2>{icon('user')} Reservadores guardados <small class="hint" style="font-weight:400">({len(lista)})</small></h2>
      <ul class="lista compacta">{filas}</ul>
      <div id="res_estado"></div>
    </div>
    <script>
    async function probar(i,btn){{
      btn.disabled=true; const t=btn.textContent; btn.textContent='Probando…';
      const r=await fetch('{url_for('api_probar_login')}?i='+i);
      const d=await r.json();
      document.getElementById('res_estado').innerHTML='<div class="msg">'+d.msg+'</div>';
      btn.textContent=t; btn.disabled=false;
    }}
    </script>"""
    return pagina("Reservadores", "reservadores", cuerpo)


@app.route("/reservadores/guardar", methods=["POST"])
@permiso_requerido("gestionar_bases")
def reservador_guardar():
    f = request.form
    nombre = limpiar_nombre(f.get("nombre", ""))
    usuario = limpiar_nombre(f.get("usuario", ""), maxlen=120)
    clave = f.get("clave", "").strip()
    telefono = limpiar_nombre(f.get("telefono", ""), maxlen=30)
    orig = f.get("orig", "").strip().lower()
    if not (nombre and usuario):
        flash("Nombre y usuario son obligatorios.")
        return redirect(url_for("reservadores"))
    with rb._io_lock:
        lista = cargar("db_reservadores.json", "reservadores")
        idx = None
        for i, r in enumerate(lista):
            uu = str(r.get("usuario", "")).strip().lower()
            if (orig and uu == orig) or (not orig and uu == usuario.lower()):
                idx = i
                break
        nuevo = {"nombre": nombre, "usuario": usuario, "telefono": telefono}
        if idx is not None:
            nuevo["clave"] = clave if clave else lista[idx].get("clave", "")  # vacío = conservar
            nuevo["player_id"] = lista[idx].get("player_id")
            lista[idx] = nuevo
        else:
            if not clave:
                flash("Para un reservador nuevo la contraseña de OnDepor es obligatoria.")
                return redirect(url_for("reservadores"))
            nuevo["clave"] = clave
            nuevo["player_id"] = None
            lista.append(nuevo)
        # el armador del cronograma necesita el carnet del reservador; si el nombre
        # coincide sin ambigüedad con uno de Jugadores, se guarda solo
        if not nuevo.get("player_id"):
            nuevo["player_id"] = enc.vincular_carnet(nombre)
        guardar("db_reservadores.json", "reservadores", lista,
                ayuda="Reservadores: los que reservan (usuario + clave = jugador 1).")
    flash(f"Guardado: {nombre}")
    return redirect(url_for("reservadores"))


@app.route("/reservadores/borrar/<int:idx>", methods=["POST"])
@permiso_requerido("gestionar_bases")
def reservador_borrar(idx):
    with rb._io_lock:
        lista = cargar("db_reservadores.json", "reservadores")
        if 0 <= idx < len(lista):
            lista.pop(idx)
            guardar("db_reservadores.json", "reservadores", lista,
                    ayuda="Reservadores: los que reservan (usuario + clave = jugador 1).")
    return redirect(url_for("reservadores"))


@app.route("/api/probar_login")
@permiso_requerido("gestionar_bases")
def api_probar_login():
    if not rate_ok("ondepor:" + (request.remote_addr or "?"), maximo=20, ventana_seg=60):
        return jsonify({"msg": "Demasiadas pruebas seguidas; esperá unos segundos."})
    lista = cargar("db_reservadores.json", "reservadores")
    try:
        i = int(request.args.get("i", "-1"))
    except ValueError:
        i = -1
    if not (0 <= i < len(lista)):
        return jsonify({"msg": "Reservador inválido."})
    r = lista[i]
    try:
        OnDepor(r["usuario"], r["clave"], CLUB_DEFAULT).login()
        return jsonify({"msg": f"Login OK para {r['nombre']}."})
    except Exception as e:
        return jsonify({"msg": f"Falló: {str(e)[:200]}"})


# ----------------------------------------------------------------------
cargar_roles()   # permisos por rol (roles.json o los de fábrica)

if __name__ == "__main__":
    _cargar_estados()
    threading.Thread(target=_manager, daemon=True).start()   # reloj interno (1 solo hilo)
    # bind por defecto a localhost: Caddy llega por 127.0.0.1 y el puerto NO queda
    # expuesto al mundo aunque el firewall se reabra (M4). Override con PANEL_HOST.
    host = os.environ.get("PANEL_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 8080))
    try:
        from waitress import serve   # servidor WSGI de producción (un solo proceso)
        print(f"Panel (waitress) en http://{host}:{port}", flush=True)
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        print(f"waitress no instalado; uso el server de desarrollo de Flask en http://{host}:{port}", flush=True)
        app.run(host=host, port=port, threaded=True)
