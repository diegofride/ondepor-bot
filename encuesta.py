# -*- coding: utf-8 -*-
"""
==========================================================================
  encuesta.py  -  Encuesta semanal de disponibilidad (Blueprint de Flask)
==========================================================================
Los jueves se abre una encuesta y se pasa el link por WhatsApp al grupo.
Cada uno entra, dice qué días/horarios puede jugar y si presta su carnet.

DECISIONES DE DISEÑO (pedidas por Diego):
  - SIN login y SIN app: es un link suelto que no muestra ni menciona el
    panel de reservas. Nada acá enlaza a la app.
  - La web NO puede leer el número de teléfono de quien entra (no existe
    forma de hacerlo). Por eso la identidad se resuelve así: la primera
    vez elegís tu nombre de la lista y queda guardado en una cookie de ese
    celular; las siguientes veces entra reconocido.
  - Jugar y prestar el carnet son cosas SEPARADAS: hay gente que presta el
    carnet sin venir, y gente que juega pero no lo presta.

Archivos:
  encuesta_config.json  -> la encuesta abierta (o la última cerrada)
  votos.json            -> {encuesta_id: {player_id: voto}}
==========================================================================
"""
import json
import secrets
import datetime

from flask import (Blueprint, request, render_template_string, jsonify,
                   redirect, url_for, session, flash, make_response)
from markupsafe import escape

import reservar_bot as rb
from ondepor_core import hoy_ar, ahora_ar

bp = Blueprint("encuesta", __name__)

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
DIAS_POR_DEFECTO = [5, 6]                       # sábado y domingo
HORAS_TODAS = [f"{h:02d}:00" for h in range(8, 24)]   # 8 a 23 (día + noche)
COOKIE_JUGADOR = "jug"
COOKIE_DIAS = 180


# ----------------------------------------------------------------------
#  Datos
# ----------------------------------------------------------------------
def cargar_encuesta():
    return rb.leer_json("encuesta_config.json", {}) or {}


def guardar_encuesta(e):
    with rb._io_lock:
        rb.escribir_json("encuesta_config.json", e)


def cargar_votos():
    return rb.leer_json("votos.json", {"votos": {}}).get("votos", {})


def votos_de(enc_id):
    return cargar_votos().get(enc_id, {})


def guardar_voto(enc_id, pid, voto):
    with rb._io_lock:
        data = rb.leer_json("votos.json", {"votos": {}})
        data.setdefault("votos", {}).setdefault(enc_id, {})[str(pid)] = voto
        rb.escribir_json("votos.json", data)


def todos_los_jugadores():
    """Toda la gente de las bases (jugadores + reservadores), ordenada alfabéticamente.
    Es el universo del que se ELIGEN los participantes de cada encuesta."""
    jug = rb._cargar("db_jugadores.json", "jugadores")
    res = rb._cargar("db_reservadores.json", "reservadores")
    por_id = {}
    for j in jug:
        if j.get("player_id"):
            por_id[str(j["player_id"])] = {"id": str(j["player_id"]), "nombre": j.get("nombre", "")}
    for r in res:                                # un reservador puede no estar en db_jugadores
        if r.get("player_id"):
            por_id.setdefault(str(r["player_id"]),
                              {"id": str(r["player_id"]), "nombre": r.get("nombre", "")})
    return sorted(por_id.values(), key=lambda x: x["nombre"].lower())


def _jugadores(e=None):
    """Los que participan de ESTA encuesta (los que Diego eligió al abrirla).
    Si la encuesta no tiene lista (formato viejo), participan todos."""
    e = cargar_encuesta() if e is None else e
    todos = todos_los_jugadores()
    elegidos = (e or {}).get("participantes")
    if not elegidos:
        return todos
    permitidos = {str(x) for x in elegidos}
    return [j for j in todos if j["id"] in permitidos]


def esta_abierta(e):
    if not e or e.get("estado") != "abierta":
        return False
    cierre = e.get("cierra_epoch")
    return not cierre or ahora_ar().timestamp() + 3 * 3600 < cierre + 1


# ----------------------------------------------------------------------
#  Página pública de la encuesta (sin login, sin rastro de la app)
# ----------------------------------------------------------------------
PAGINA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>¿Jugás este finde?</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
  background:#0e2038;color:#1c2330;font-size:16px;line-height:1.45;
  background-image:radial-gradient(900px 400px at 80% -10%,rgba(18,183,106,.18),transparent 60%)}
.wrap{max-width:520px;margin:0 auto;padding:18px 14px 40px}
.marca{color:#fff;text-align:center;font-weight:800;font-size:1.3rem;margin:8px 0 16px;letter-spacing:.2px}
.marca small{display:block;font-weight:400;font-size:.85rem;opacity:.75;margin-top:3px}
.card{background:#fff;border-radius:16px;padding:18px;margin-bottom:14px;box-shadow:0 8px 30px rgba(0,0,0,.22)}
h2{margin:0 0 4px;font-size:1.05rem;color:#0e2038}
.sub{color:#6b7480;font-size:.87rem;margin:0 0 12px}
.dia{font-weight:700;color:#0e2038;margin:14px 0 8px;display:flex;align-items:center;gap:8px}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{position:relative}
.chip input{position:absolute;opacity:0;width:100%;height:100%;margin:0;cursor:pointer}
.chip span{display:block;padding:9px 12px;border:1.5px solid #d7dde6;border-radius:999px;
  font-size:.92rem;background:#fff;transition:.12s;user-select:none;min-width:56px;text-align:center}
.chip input:checked + span{background:#12b76a;border-color:#12b76a;color:#fff;font-weight:600}
.chip input:focus-visible + span{outline:3px solid rgba(18,183,106,.4)}
.nojuego{margin-top:10px}
.nojuego label{display:flex;align-items:center;gap:9px;font-size:.93rem;color:#6b7480;cursor:pointer}
.nojuego input{width:19px;height:19px;accent-color:#98a2b3}
.carnet label{display:flex;gap:11px;align-items:flex-start;padding:13px;border:1.5px solid #d7dde6;
  border-radius:12px;margin-bottom:9px;cursor:pointer;transition:.12s}
.carnet input{margin-top:2px;width:19px;height:19px;accent-color:#12b76a;flex:0 0 auto}
.carnet label:has(input:checked){border-color:#12b76a;background:#f0fdf6}
.carnet b{display:block;font-size:.96rem}
.carnet small{color:#6b7480;font-size:.84rem}
button{width:100%;background:#12b76a;color:#fff;border:0;padding:15px;border-radius:12px;
  font-size:1.05rem;font-weight:600;cursor:pointer}
button:active{transform:scale(.99)}
button.sec{background:#fff;color:#0e2038;border:1.5px solid #d7dde6;font-size:.92rem;padding:11px}
.quien{display:flex;flex-wrap:wrap;gap:8px}
.quien a{display:block;padding:12px 14px;background:#fff;border:1.5px solid #d7dde6;border-radius:12px;
  text-decoration:none;color:#1c2330;font-size:.95rem;flex:1 1 44%}
.quien a:active{background:#f0fdf6;border-color:#12b76a}
.ok{text-align:center;padding:10px 0}
.ok .tic{font-size:2.6rem;line-height:1}
.hola{background:#f0fdf6;border:1px solid #a7e8c6;border-radius:10px;padding:9px 12px;
  font-size:.9rem;margin-bottom:14px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.hola a{color:#6b7480;font-size:.82rem}
.cerrada{text-align:center;color:#fff;padding:30px 10px}
.pie{color:rgba(255,255,255,.5);text-align:center;font-size:.78rem;margin-top:16px}
</style></head><body><div class="wrap">
<div class="marca">🎾 ¿Jugás este finde?<small>{{titulo}}</small></div>
{{cuerpo|safe}}
<div class="pie">Se cierra el {{cierre_txt}}</div>
</div></body></html>"""


def _pagina(titulo, cuerpo, cierre_txt=""):
    return render_template_string(PAGINA, titulo=titulo, cuerpo=cuerpo, cierre_txt=cierre_txt)


@bp.route("/e/<token>")
def ver(token):
    e = cargar_encuesta()
    if not e or e.get("token") != token:
        return _pagina("", '<div class="cerrada"><h2 style="color:#fff">Link no válido</h2>'
                           '<p>Pedile el link nuevo al grupo.</p></div>')
    if not esta_abierta(e):
        return _pagina(e.get("titulo", ""),
                       '<div class="cerrada"><div style="font-size:2.4rem">🔒</div>'
                       '<h2 style="color:#fff">La encuesta ya cerró</h2>'
                       '<p style="opacity:.8">Las canchas se están reservando. ¡Nos vemos!</p></div>')

    jugadores = _jugadores(e)
    pid = request.cookies.get(COOKIE_JUGADOR, "")
    yo = next((j for j in jugadores if j["id"] == pid), None)

    # ---- ¿quién sos? (solo la primera vez en este celular) ----
    if not yo:
        botones = "".join(
            f'<a href="{url_for("encuesta.soy", token=token, pid=j["id"])}">{escape(j["nombre"])}</a>'
            for j in jugadores)
        cuerpo = f"""<div class="card">
          <h2>¿Quién sos?</h2>
          <p class="sub">Tocá tu nombre. Queda guardado en este celular, no lo vas a tener que
             elegir de nuevo.</p>
          <div class="quien">{botones}</div>
        </div>"""
        return _pagina(e.get("titulo", ""), cuerpo, e.get("cierre_txt", ""))

    voto = votos_de(e["id"]).get(pid, {})
    juega = voto.get("juega", {})

    # ---- grilla de días y horarios ----
    bloques = ""
    for d in e.get("dias", DIAS_POR_DEFECTO):
        elegidas = juega.get(str(d), [])
        chips = "".join(
            f'<label class="chip"><input type="checkbox" name="h{d}" value="{h}"'
            f'{" checked" if h in elegidas else ""}><span>{h[:2]}</span></label>'
            for h in e.get("horas", HORAS_TODAS))
        sin = " checked" if (str(d) in juega and not elegidas) or (voto and str(d) not in juega) else ""
        bloques += f"""
        <div class="dia">📅 {DIAS[d]}</div>
        <div class="chips">{chips}</div>
        <div class="nojuego"><label>
          <input type="checkbox" name="no{d}"{sin} onchange="destildar({d}, this)"> Este día no juego
        </label></div>"""

    presta = voto.get("presta_carnet")
    cuerpo = f"""
    <div class="hola"><span>Hola, <b>{escape(yo["nombre"])}</b>
      {"· ya votaste, podés cambiarlo" if voto else ""}</span>
      <a href="{url_for("encuesta.no_soy", token=token)}">no soy yo</a></div>
    <form method="post" action="{url_for("encuesta.votar", token=token)}">
      <div class="card">
        <h2>¿Qué horarios podés?</h2>
        <p class="sub">Marcá todos los que te sirvan. Cuantos más marques, más fácil es
           armar las canchas.</p>
        {bloques}
      </div>
      <div class="card carnet">
        <h2>Tu carnet</h2>
        <p class="sub">Con los carnets se reservan las canchas. Prestarlo ayuda aunque no vengas.</p>
        <label><input type="radio" name="carnet" value="si"{" checked" if presta is True else ""}>
          <span><b>Pueden usar mi carnet</b>
          <small>Aunque no juegue. Así se pueden reservar más canchas.</small></span></label>
        <label><input type="radio" name="carnet" value="no"{" checked" if presta is False else ""}>
          <span><b>No usen mi carnet</b>
          <small>Solo se usa si juego.</small></span></label>
      </div>
      <button>{"Actualizar mi respuesta" if voto else "Listo, enviar"}</button>
    </form>
    <script>
    function destildar(d, cb){{
      if(!cb.checked) return;
      document.querySelectorAll('input[name=h'+d+']').forEach(function(x){{ x.checked=false; }});
    }}
    document.querySelectorAll('.chip input').forEach(function(c){{
      c.addEventListener('change', function(){{
        if(!c.checked) return;
        var d = c.name.slice(1);
        var no = document.querySelector('input[name=no'+d+']');
        if(no) no.checked = false;      // si elige un horario, ya no es "no juego"
      }});
    }});
    </script>"""
    return _pagina(e.get("titulo", ""), cuerpo, e.get("cierre_txt", ""))


@bp.route("/e/<token>/soy/<pid>")
def soy(token, pid):
    """Guarda quién es en una cookie de este celular (no hay login)."""
    if not any(j["id"] == pid for j in _jugadores()):
        return redirect(url_for("encuesta.ver", token=token))
    r = make_response(redirect(url_for("encuesta.ver", token=token)))
    r.set_cookie(COOKIE_JUGADOR, pid, max_age=COOKIE_DIAS * 86400, samesite="Lax")
    return r


@bp.route("/e/<token>/no-soy")
def no_soy(token):
    r = make_response(redirect(url_for("encuesta.ver", token=token)))
    r.delete_cookie(COOKIE_JUGADOR)
    return r


@bp.route("/e/<token>/votar", methods=["POST"])
def votar(token):
    e = cargar_encuesta()
    if not e or e.get("token") != token or not esta_abierta(e):
        return redirect(url_for("encuesta.ver", token=token))
    pid = request.cookies.get(COOKIE_JUGADOR, "")
    yo = next((j for j in _jugadores(e) if j["id"] == pid), None)
    if not yo:
        return redirect(url_for("encuesta.ver", token=token))

    juega = {}
    for d in e.get("dias", DIAS_POR_DEFECTO):
        if request.form.get(f"no{d}"):
            continue                                  # ese día no juega
        horas = [h for h in request.form.getlist(f"h{d}") if h in e.get("horas", HORAS_TODAS)]
        if horas:
            juega[str(d)] = sorted(horas)
    guardar_voto(e["id"], pid, {
        "nombre": yo["nombre"],
        "juega": juega,
        "presta_carnet": request.form.get("carnet") == "si",
        "cuando": ahora_ar().strftime("%Y-%m-%d %H:%M"),
    })

    resumen = ""
    for d, hs in sorted(juega.items()):
        resumen += f"<div><b>{DIAS[int(d)]}</b>: {', '.join(h[:2] + ' h' for h in hs)}</div>"
    if not resumen:
        resumen = "<div>Este finde no jugás.</div>"
    carnet = ("Prestás tu carnet 👍" if request.form.get("carnet") == "si"
              else "No prestás tu carnet")
    cuerpo = f"""<div class="card ok">
      <div class="tic">✅</div>
      <h2>¡Listo, {escape(yo["nombre"].split()[0])}!</h2>
      <p class="sub">Anotado. Podés volver a entrar y cambiarlo cuando quieras.</p>
      <div style="text-align:left;background:#f6f8fb;border-radius:10px;padding:12px;font-size:.93rem">
        {resumen}<div style="margin-top:6px;color:#6b7480">{carnet}</div>
      </div>
      <div style="height:12px"></div>
      <a href="{url_for('encuesta.ver', token=token)}"><button type="button" class="sec">
        Cambiar mi respuesta</button></a>
    </div>"""
    return _pagina(e.get("titulo", ""), cuerpo, e.get("cierre_txt", ""))


# ======================================================================
#  PANEL (para el admin, dentro de la app): abrir, seguir y cerrar
# ======================================================================
def _proximo(dia_semana, desde=None):
    """Próxima fecha con ese día de la semana (0=lunes), contando hoy."""
    hoy = desde or hoy_ar()
    return hoy + datetime.timedelta(days=(dia_semana - hoy.weekday()) % 7)


def resumen_votos(e):
    """Arma el resumen: quién votó, quién falta y cuántos hay por día/hora."""
    jugadores = _jugadores(e)
    votos = votos_de(e.get("id", ""))
    votaron, faltan = [], []
    for j in jugadores:
        v = votos.get(j["id"])
        (votaron if v else faltan).append({**j, "voto": v})
    # cuántos pueden en cada día/hora
    por_dia = {}
    for d in e.get("dias", DIAS_POR_DEFECTO):
        por_hora = {}
        for h in e.get("horas", HORAS_TODAS):
            gente = [v["nombre"] for pid, v in votos.items()
                     if h in (v.get("juega", {}).get(str(d)) or [])]
            if gente:
                por_hora[h] = gente
        por_dia[d] = por_hora
    carnets = [v["nombre"] for v in votos.values() if v.get("presta_carnet")]
    return {"votaron": votaron, "faltan": faltan, "por_dia": por_dia,
            "carnets": carnets, "total": len(jugadores)}


def canchas_para(n):
    """Regla del grupo: 4-7 -> 1 cancha, 8-11 -> 2, 12+ -> 3 (tope 3)."""
    if n < 4:
        return 0
    return min(3, n // 4)
