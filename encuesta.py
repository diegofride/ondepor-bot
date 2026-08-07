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
import itertools
import datetime
import unicodedata

from flask import (Blueprint, request, render_template_string, jsonify,
                   redirect, url_for, session, flash, make_response)
from markupsafe import escape

import reservar_bot as rb
from ondepor_core import hoy_ar, ahora_ar, _epoch_ar

bp = Blueprint("encuesta", __name__)

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
DIAS_POR_DEFECTO = [5, 6]                       # sábado y domingo
# Solo los horarios en los que realmente juegan: pedir 16 franjas hacía que la
# encuesta se sintiera pesada y la gente no la contestara.
HORAS_POR_DEFECTO = ["09:00", "10:00", "11:00", "15:00", "16:00", "17:00"]
HORAS_TODAS = [f"{h:02d}:00" for h in range(8, 24)]   # el universo, por si se agrega alguna

# Cada día se responde con UNA sola decisión (jugar y prestar el carnet no son dos
# preguntas: si jugás, tu carnet se usa igual; prestarlo solo se decide si NO venís).
MODO_JUEGO = "juego"
MODO_PRESTA = "presta"      # no viene, pero deja el carnet
MODO_NO = "no"              # no cuenta para nada ese día

LEYENDA_CARNET = ("Prestar el carnet no te compromete a nada. Se usa solo para pisar la "
                  "cancha y se cambia por quien juega realmente antes del turno. No te "
                  "genera sanciones ni te impide reservar después.")
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
.dia-card h2{margin-bottom:10px;font-size:1.15rem}
.op{display:flex;gap:11px;align-items:flex-start;padding:13px;border:1.5px solid #d7dde6;
  border-radius:12px;margin-bottom:9px;cursor:pointer;transition:.12s}
.op input{margin-top:2px;width:20px;height:20px;accent-color:#12b76a;flex:0 0 auto}
.op b{display:block;font-size:.98rem}
.op small{color:#6b7480;font-size:.82rem;display:block;margin-top:3px;line-height:1.35}
.op.sel{border-color:#12b76a;background:#f0fdf6}
.horarios{display:none;padding:2px 4px 12px 40px}
.franja{margin-bottom:10px}
.fr-tit{display:block;font-size:.8rem;color:#6b7480;margin-bottom:5px;font-weight:600}
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

    # ---- una sola decisión por día ----
    def bloque(d):
        v = (voto.get("dias") or {}).get(str(d), {})
        modo = v.get("modo", "")
        horas_sel = v.get("horas", [])
        manana = [h for h in e.get("horas", HORAS_POR_DEFECTO) if int(h[:2]) < 13]
        tarde = [h for h in e.get("horas", HORAS_POR_DEFECTO) if int(h[:2]) >= 13]

        def chips(lista, titulo):
            if not lista:
                return ""
            cs = "".join(
                f'<label class="chip"><input type="checkbox" name="h{d}" value="{h}"'
                f'{" checked" if h in horas_sel else ""}><span>{int(h[:2])}</span></label>'
                for h in lista)
            return f'<div class="franja"><span class="fr-tit">{titulo}</span><div class="chips">{cs}</div></div>'

        return f"""
        <div class="card dia-card">
          <h2>{DIAS[d]}</h2>
          <label class="op"><input type="radio" name="m{d}" value="{MODO_JUEGO}"
             {"checked" if modo == MODO_JUEGO else ""} onchange="pintar({d})">
            <span><b>Juego</b></span></label>
          <div class="horarios" id="hs{d}">
            <p class="sub" style="margin:2px 0 8px">¿A qué hora podés? Marcá todas las que te sirvan.</p>
            {chips(manana, "Mañana")}{chips(tarde, "Tarde")}
          </div>
          <label class="op"><input type="radio" name="m{d}" value="{MODO_PRESTA}"
             {"checked" if modo == MODO_PRESTA else ""} onchange="pintar({d})">
            <span><b>No juego, pero pueden usar mi carnet</b>
            <small>{LEYENDA_CARNET}</small></span></label>
          <label class="op"><input type="radio" name="m{d}" value="{MODO_NO}"
             {"checked" if modo == MODO_NO else ""} onchange="pintar({d})">
            <span><b>Este día no cuenten conmigo</b></span></label>
        </div>"""

    dias_html = "".join(bloque(d) for d in e.get("dias", DIAS_POR_DEFECTO))
    # backstop del server: si el POST vino incompleto, votar() redirige acá con ?falta=1
    aviso_falta = ""
    if request.args.get("falta"):
        aviso_falta = ('<div class="falta" style="display:block">Tu respuesta no se guardó: '
                       'hay que contestar TODOS los días (aunque sea "no cuenten conmigo").</div>')
    dias_js = json.dumps([[d, DIAS[d]] for d in e.get("dias", DIAS_POR_DEFECTO)],
                         ensure_ascii=False)
    cuerpo = f"""
    <style>.falta{{display:none;background:#fde8e8;border:1px solid #f5b5b5;color:#b42318;
      border-radius:10px;padding:10px 12px;font-size:.9rem;margin-bottom:10px}}</style>
    <div class="hola"><span>Hola, <b>{escape(yo["nombre"])}</b>
      {"· ya votaste, podés cambiarlo" if voto else ""}</span>
      <a href="{url_for("encuesta.no_soy", token=token)}">no soy yo</a></div>
    {aviso_falta}
    <form method="post" action="{url_for("encuesta.votar", token=token)}"
          onsubmit="return validar()">
      {dias_html}
      <div id="avisoFalta" class="falta"></div>
      <button>{"Actualizar mi respuesta" if voto else "Listo, enviar"}</button>
    </form>
    <script>
    function pintar(d){{
      var r = document.querySelector('input[name=m'+d+']:checked');
      var hs = document.getElementById('hs'+d);
      var juega = r && r.value === '{MODO_JUEGO}';
      hs.style.display = juega ? 'block' : 'none';
      if(!juega) hs.querySelectorAll('input').forEach(function(x){{ x.checked = false; }});
      document.querySelectorAll('.op').forEach(function(l){{
        l.classList.toggle('sel', !!l.querySelector('input:checked'));
      }});
    }}
    function validar(){{
      var faltan = [];
      {dias_js}.forEach(function(par){{
        var d = par[0], nom = par[1];
        var r = document.querySelector('input[name=m'+d+']:checked');
        if(!r) faltan.push(nom);
        else if(r.value === '{MODO_JUEGO}'
                && !document.querySelector('input[name=h'+d+']:checked'))
          faltan.push(nom + ' (marca al menos un horario)');
      }});
      var av = document.getElementById('avisoFalta');
      if(faltan.length){{
        av.textContent = 'Para enviar hay que contestar todos los dias. Falta: '
                         + faltan.join(' y ') + '.';
        av.style.display = 'block';
        av.scrollIntoView({{behavior:'smooth', block:'center'}});
        return false;
      }}
      return true;
    }}
    document.addEventListener('DOMContentLoaded', function(){{
      {" ".join("pintar(%d);" % d for d in e.get("dias", DIAS_POR_DEFECTO))}
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

    dias = {}
    incompleto = False
    for d in e.get("dias", DIAS_POR_DEFECTO):
        modo = request.form.get(f"m{d}", "")
        if modo not in (MODO_JUEGO, MODO_PRESTA, MODO_NO):
            incompleto = True                          # no contestó ese día
            continue
        entrada = {"modo": modo}
        if modo == MODO_JUEGO:
            horas = [h for h in request.form.getlist(f"h{d}")
                     if h in e.get("horas", HORAS_POR_DEFECTO)]
            if not horas:                              # dijo "juego" pero no marcó hora
                incompleto = True
                continue
            entrada["horas"] = sorted(horas)
        dias[str(d)] = entrada
    if incompleto:
        # la respuesta vale solo COMPLETA (pedido de Diego): hay que contestar TODOS
        # los días, aunque sea "no cuenten conmigo". El JS ya lo frena en el celular;
        # esto es el backstop del server y no guarda nada.
        return redirect(url_for("encuesta.ver", token=token, falta=1))
    guardar_voto(e["id"], pid, {
        "nombre": yo["nombre"],
        "dias": dias,
        "cuando": ahora_ar().strftime("%Y-%m-%d %H:%M"),
    })

    partes = []
    for d in e.get("dias", DIAS_POR_DEFECTO):
        v = dias.get(str(d))
        if not v:
            continue
        if v["modo"] == MODO_JUEGO:
            txt = "jugás a las " + ", ".join(f"{int(h[:2])}" for h in v["horas"]) + " h"
        elif v["modo"] == MODO_PRESTA:
            txt = "no jugás, prestás el carnet"
        else:
            txt = "no contás"
        partes.append(f"<div><b>{DIAS[int(d)]}</b>: {txt}</div>")
    resumen = "".join(partes) or "<div>No contestaste ningún día.</div>"

    cuerpo = f"""<div class="card ok">
      <div class="tic">✅</div>
      <h2>¡Listo, {escape(yo["nombre"].split()[0])}!</h2>
      <p class="sub">Anotado. Podés volver a entrar y cambiarlo cuando quieras.</p>
      <div style="text-align:left;background:#f6f8fb;border-radius:10px;padding:12px;font-size:.93rem">
        {resumen}
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
    """Qué contestó cada uno y con qué carnets se cuenta CADA DÍA.
    La bolsa es por día porque el carnet se responde por día: alguien puede prestarlo
    el sábado y no el domingo."""
    jugadores = _jugadores(e)
    votos = votos_de(e.get("id", ""))
    votaron, faltan = [], []
    for j in jugadores:
        v = votos.get(j["id"])
        (votaron if v else faltan).append({**j, "voto": v})

    nombres = {j["id"]: j["nombre"] for j in todos_los_jugadores()}
    extra = {str(p): nombres.get(str(p), str(p)) for p in (e.get("carnets_extra") or [])}

    # carnets que NO se pueden usar aunque estén en la bolsa (pedido de Diego: alguien
    # que ya tiene su propia reserva por fuera del cronograma, o que directamente no lo
    # presta). Se sacan de todas las listas para que el armador ni los mire.
    bloq = {str(x) for x in (e.get("carnets_bloqueados") or [])}

    por_dia = {}
    for d in e.get("dias", DIAS_POR_DEFECTO):
        juegan, prestan, por_hora = {}, {}, {}
        for pid, v in votos.items():
            info = (v.get("dias") or {}).get(str(d))
            if not info:
                continue
            if info["modo"] == MODO_JUEGO:
                juegan[pid] = v["nombre"]
                for h in info.get("horas", []):
                    por_hora.setdefault(h, []).append(pid)
            elif info["modo"] == MODO_PRESTA:
                prestan[pid] = v["nombre"]
        for p in bloq:                                  # fuera de juego para el armador
            juegan.pop(p, None)
            prestan.pop(p, None)
        extra_d = {p: n for p, n in extra.items() if p not in bloq}
        bolsa = {**juegan, **prestan, **extra_d}        # los extra sirven todos los días
        ids = {h: sorted((p for p in g if p not in bloq),
                         key=lambda p: juegan.get(p, "").lower())
               for h, g in sorted(por_hora.items())}
        ids = {h: g for h, g in ids.items() if g}
        por_dia[d] = {
            "por_hora_ids": ids,
            "por_hora": {h: [juegan[p] for p in g] for h, g in ids.items()},
            "juegan": juegan, "prestan": prestan, "extra": extra_d, "bolsa": bolsa,
            "canchas_posibles": len(bolsa) // 4,
        }
    return {"votaron": votaron, "faltan": faltan, "total": len(jugadores),
            "por_dia": por_dia, "extra": extra}


def canchas_para(n):
    """Regla del grupo: 4-7 -> 1 cancha, 8-11 -> 2, 12+ -> 3 (tope 3)."""
    if n < 4:
        return 0
    return min(3, n // 4)


# ======================================================================
#  ARMADOR DE CRONOGRAMA: de los votos a las reservas
#  Las reglas están explicadas en ARMADOR_CRONOGRAMA.md. El corazón es que
#  un carnet no puede estar en dos reservas activas del mismo club a la vez,
#  y que se libera cuando pasa el turno O cuando el cambio de nombres lo
#  saca (10 min antes). Por eso los turnos se resuelven en orden cronológico
#  arrastrando en qué ventanas queda tomado cada carnet.
# ======================================================================
DUR_TURNO_MIN = 60          # los turnos son de 1 hora
CAMBIO_ANTES_MIN = 10       # el cambio de nombres se ejecuta 10 min antes
APERTURA_ANTES_MIN = 24 * 60 - 1    # el turno se habilita 24 h antes + 1 min
POR_CANCHA = 4
CLUB_DIA, CLUB_NOCHE = 1102, 504


def _dt(fecha, hora):
    return datetime.datetime.fromisoformat(f"{fecha}T{hora}:00")


def club_de(hora):
    """De 8 a 17 es la sede de día; de 18 en adelante, la de noche (son clubes distintos
    en OnDepor, y el límite de una reserva por persona es POR CLUB)."""
    return CLUB_DIA if int(str(hora)[:2]) < 18 else CLUB_NOCHE


def _norm_nombre(s):
    """Nombre comparable: sin acentos, en minúscula y con los espacios normalizados."""
    s = unicodedata.normalize("NFKD", str(s or "").strip().lower())
    return " ".join(s.encode("ascii", "ignore").decode().split())


def vincular_carnet(nombre, jugadores=None):
    """Busca el player_id que le corresponde a ese nombre en db_jugadores.

    Aguanta acentos ("Dario" por "Darío") y apodos ("Rafa" por "Rafael"), pero solo
    devuelve algo cuando NO hay ninguna duda: con dos candidatos con el mismo apellido
    ("Jony Enser" con un Jonathan y un Jacobo) devuelve None, porque meter el carnet
    equivocado en una reserva es peor que avisar.
    """
    partes = _norm_nombre(nombre).split()
    if not partes:
        return None
    exacto, por_apellido = None, []
    for j in (rb._cargar("db_jugadores.json", "jugadores") if jugadores is None else jugadores):
        if not j.get("player_id"):
            continue
        otras = _norm_nombre(j.get("nombre")).split()
        if not otras:
            continue
        if otras == partes:
            exacto = j["player_id"]
        elif len(partes) > 1 and len(otras) > 1 and otras[-1] == partes[-1] \
                and (otras[0].startswith(partes[0]) or partes[0].startswith(otras[0])):
            por_apellido.append(j["player_id"])
    if exacto:
        return exacto
    return por_apellido[0] if len(por_apellido) == 1 else None


def reservadores_con_carnet():
    """{player_id: nombre} de los que pueden disparar una reserva, y los que no se
    pudieron vincular.

    OJO: en `db_reservadores.json` el `player_id` es OPCIONAL y casi ninguno lo tiene
    (el resto de la app trabaja por nombre). Acá hace falta sí o sí, porque el carnet
    del reservador entra en la reserva y hay que saber cuál es.
    """
    jugadores = rb._cargar("db_jugadores.json", "jugadores")
    listos, sin_vincular = {}, []
    for r in rb._cargar("db_reservadores.json", "reservadores"):
        if not r.get("usuario"):
            continue                                   # sin cuenta no puede disparar
        pid = r.get("player_id") or vincular_carnet(r.get("nombre"), jugadores)
        if pid:
            listos[str(pid)] = r.get("nombre", "")
        else:
            sin_vincular.append(r.get("nombre", ""))
    return listos, sin_vincular


class _Agenda:
    """Ventanas en las que cada carnet está tomado, por club.

    ⚠ Se pregunta por un RANGO, no por un instante: un carnet puede estar libre en el
    momento de reservar y aun así pisarse con otra reserva más adelante. Siempre se
    mira todo el rato que va a quedar tomado.
    """

    def __init__(self):
        self.tomado = {}

    def libre(self, club, pid, desde, hasta):
        """¿El carnet está libre TODO el rango [desde, hasta)?"""
        return all(hasta <= a or desde >= b for a, b in self.tomado.get((club, pid), ()))

    def ocupar(self, club, pid, desde, hasta):
        self.tomado.setdefault((club, pid), []).append((desde, hasta))

    def libre_desde(self, club, pid, turno_dt, apertura):
        """Momento en que el carnet queda libre para ENTRAR a un turno: el fin de la
        última ventana que se cruce con [apertura, turno). Si esa ventana termina
        DESPUÉS del turno, el que llama lo detecta porque el resultado >= turno.
        Sin nada en el medio, está libre desde la apertura."""
        fines = [b for a, b in self.tomado.get((club, pid), ())
                 if a < turno_dt and b > apertura]
        return max(fines) if fines else apertura


ES_MANANA = lambda h: int(str(h)[:2]) < 13          # noqa: E731
MARGEN_CAMBIO_SEG = 300      # el cambio corre 5 min después de que se libera el último
# ⚠ El cambio NUNCA va el día anterior: cancelar y volver a tomar la cancha cinco
# minutos después de ganarla en la apertura es devolverla al ruedo en el peor momento.
# Va el día del turno, y no de madrugada.
HORA_MIN_CAMBIO = 8
MAX_ROTAN = 2                # regla de Darío: rotando afuera a lo sumo 2 por turno


def reservas_existentes():
    """Las reservas que YA están cargadas en el sistema, con las ventanas en que dejan
    tomado cada carnet.

    ⚠ SIN esto el armador planifica como si el finde estuviera vacío y propone canchas
    pisando carnets que ya están comprometidos (lo cazó Diego: "Furmanski no tiene libre
    el carnet hasta las 17 del sábado" — tenía una reserva cargada a las 16).
    Devuelve [(club, player_id, desde, hasta)].
    """
    estados = rb.leer_json("estados.json", {}) or {}
    jugadores = rb._cargar("db_jugadores.json", "jugadores")
    tramos = []
    for r in rb._cargar("reservas.json", "reservas"):
        est = (estados.get(r.get("id")) or {}).get("estado", "programada")
        if est in ("fallada", "vencida"):
            continue                       # esa cancha no entró: no toma ningún carnet
        try:
            turno = _dt(r["fecha"], r["hora_desde"])
        except (KeyError, ValueError, TypeError):
            continue
        club = r.get("club_id") or club_de(r["hora_desde"])
        ap = turno - datetime.timedelta(minutes=APERTURA_ANTES_MIN)
        fin = turno + datetime.timedelta(minutes=DUR_TURNO_MIN)
        res_pid = vincular_carnet(r.get("reservador"), jugadores)
        ini = {str(p) for p in r.get("jugadores", [])} | ({str(res_pid)} if res_pid else set())

        cp = r.get("cambio_prog") or {}
        cambio = None
        if cp.get("estado") in ("pendiente", "en_curso") and cp.get("ej_epoch"):
            cambio = ap + datetime.timedelta(seconds=cp["ej_epoch"] - _epoch_ar(ap))
            if not (ap < cambio < fin):
                cambio = None
        if cambio:
            fin_pid = vincular_carnet(cp.get("reservador"), jugadores)
            fin_l = {str(p) for p in cp.get("jugadores", [])} | ({str(fin_pid)} if fin_pid else set())
        else:
            fin_l = ini

        for p in ini | fin_l:
            if cambio is None or (p in ini and p in fin_l):
                tramos.append((club, p, ap, fin))
            elif p in ini:
                tramos.append((club, p, ap, cambio))
            else:
                tramos.append((club, p, cambio, fin))
    return tramos


def armar_cronograma(e, fijas=None, desde_cero=False):
    """Arma el flujo de reservas del finde entero (modelo de Darío/Diego, ver
    ARMADOR_CRONOGRAMA.md). Reparte los CARNETS; jugar y tener el carnet en la reserva
    son cosas distintas (el que votó una hora la juega aunque su carnet esté en otra
    reserva: rota).

    'desde_cero' ignora las reservas que ya están cargadas (por defecto se respetan:
    sus carnets están comprometidos). 'fijas' = {(día, "HH:MM"): cuántas canchas} para MANDAR a mano cuántas canchas va
    cada horario (pedido de Diego: "quiero 2 a las 15 y 2 a las 16"). Lo que no venga
    fijado lo decide el optimizador, que elige el plan con más canchas, después el que
    hace jugar a más gente distinta, y recién ahí desempata por la prioridad
    (sáb tarde → dom mañana → sáb mañana → dom tarde).
    """
    r = resumen_votos(e)
    reservadores, sin_vincular = reservadores_con_carnet()
    a = _Armador(e, r, reservadores, fijas, desde_cero)
    plan = a.mejor_plan()
    verificar_plan(plan["reservas"], r, e)
    plan.update({"resumen": r, "sin_vincular": sin_vincular,
                 "fijas": a.fijas, "posibles": a.posibles()})
    return plan


class _Armador:
    def __init__(self, e, r, cuentas, fijas=None, desde_cero=False):
        self.e, self.r, self.cuentas = e, r, cuentas
        self.fijas = {(int(d), str(h)): int(c) for (d, h), c in (fijas or {}).items()}
        # 'desde_cero' = armar el finde ignorando las reservas ya cargadas (para
        # comparar contra un cronograma hecho a mano, o para rehacerlo de nuevo)
        self.ya_tomados = [] if desde_cero else reservas_existentes()
        self.dias = list(e.get("dias", DIAS_POR_DEFECTO))
        self.fechas = e.get("fechas", {})
        # por día: [(hora, votantes, canchas_max)] en orden cronológico
        self.turnos = {}
        for d in self.dias:
            info = r["por_dia"].get(d, {})
            self.turnos[d] = [(h, list(pids), canchas_para(len(pids)))
                              for h, pids in sorted(info.get("por_hora_ids", {}).items())]
        # cuántas horas vota cada uno por día (para elegir a quién poner en la reserva:
        # mejor gastar el carnet del que vota UNA sola hora)
        self.n_horas = {}
        for d in self.dias:
            for h, pids, _ in self.turnos[d]:
                for p in pids:
                    self.n_horas[(d, p)] = self.n_horas.get((d, p), 0) + 1

    def _nombre(self, d, pid):
        for dd in self.dias:
            n = self.r["por_dia"].get(dd, {}).get("bolsa", {}).get(pid)
            if n:
                return n
        return self.cuentas.get(pid, str(pid))

    def _pool_prestados(self, d):
        """Los carnets que están SOLO para pisar: los que prestan en la encuesta y los
        de afuera. Estos son los que hay que gastar primero (regla de Diego)."""
        info = self.r["por_dia"].get(d, {})
        prestan = list(info.get("prestan", {}))
        return prestan + [p for p in info.get("extra", {}) if p not in prestan]

    def _pool_sueltos(self, d):
        """Último recurso: el carnet de alguien que juega pero que en ese rango no está
        en ninguna reserva. Se usa solo si los prestados no alcanzan."""
        fuera = set(self._pool_prestados(d))
        return [p for p in self.r["por_dia"].get(d, {}).get("juegan", {}) if p not in fuera]

    def _pool_pisadores(self, d):
        return self._pool_prestados(d) + self._pool_sueltos(d)

    # ------------------------------------------------------------------
    #  Búsqueda: enumera cuántas canchas darle a cada hora del primer día
    #  (ahí está el conflicto grande: la tarde del sábado contra el domingo)
    #  y el segundo día se llena greedy. Gana el plan con más canchas.
    # ------------------------------------------------------------------
    def posibles(self):
        """Cuántas canchas admite cada horario por cantidad de gente (el tope de la
        regla 4-7/8-11/12+). Lo usa la pantalla para ofrecer el selector."""
        return {(d, h): cmax for d in self.dias for h, _, cmax in self.turnos[d]}

    def mejor_plan(self):
        d1 = self.dias[0]
        bolsa1 = len(self.r["por_dia"].get(d1, {}).get("bolsa", {}))
        # los horarios FIJADOS a mano no se enumeran: van como los pidió Diego
        opciones = [([self.fijas[(d1, h)]] if (d1, h) in self.fijas else range(cmax + 1))
                    for h, _, cmax in self.turnos[d1]]
        mejor = None
        for combo in itertools.product(*opciones):
            # el filtro por presupuesto es solo un atajo: si hay horarios FIJADOS a mano
            # no se aplica, porque ahí lo que corresponde es intentar y degradar avisando
            if not self.fijas and sum(combo) * POR_CANCHA > bolsa1:
                continue
            cfg = {h: c for (h, _, _), c in zip(self.turnos[d1], combo)}
            plan = self._simular(cfg)
            if plan and (mejor is None or plan["score"] > mejor["score"]):
                mejor = plan
        if mejor is None:
            mejor = self._simular({h: 0 for h, _, _ in self.turnos[d1]}) or {
                "reservas": [], "avisos": [], "score": (), "armadas": {}, "usados": set()}
        mejor["sugerencias"] = self._sugerencias(mejor)
        todos = {}
        for d in self.dias:
            todos.update(self.r["por_dia"].get(d, {}).get("bolsa", {}))
        mejor["sobran"] = sorted(n for p, n in todos.items() if p not in mejor["usados"])
        mejor.pop("score", None)
        mejor.pop("usados", None)
        return mejor

    def _simular(self, cfg_d1):
        ag = _Agenda()
        # arranca con los carnets que YA están comprometidos por las reservas cargadas
        for club, p, a, b in self.ya_tomados:
            ag.ocupar(club, p, a, b)
        st = {"agenda": ag, "reservas": [], "avisos": [], "usados": set(),
              "armadas": {}}
        d1 = self.dias[0]
        for h, pids, cmax in self.turnos[d1]:
            c = cfg_d1.get(h, 0)
            st["armadas"][(d1, h)] = 0
            if not c:
                continue
            if (d1, h) not in self.fijas:
                if not self._armar_turno(st, d1, h, c):
                    return None                   # esta configuración no cierra
                st["armadas"][(d1, h)] = c
                continue
            # horario FIJADO a mano: si no entran las que pediste, se baja de a una y
            # se avisa — mejor eso que devolver un plan vacío sin explicar por qué
            for cc in range(c, 0, -1):
                foto = self._foto(st)
                if self._armar_turno(st, d1, h, cc):
                    st["armadas"][(d1, h)] = cc
                    if cc < c:
                        st["avisos"].append(
                            f"{DIAS[d1]} {int(h[:2])} h: pediste {c} canchas y solo "
                            f"{'entra 1' if cc == 1 else 'entran ' + str(cc)} "
                            f"(los demás tienen el carnet tomado a esa hora).")
                    break
                self._volver(st, foto)
            else:
                st["avisos"].append(
                    f"{DIAS[d1]} {int(h[:2])} h: pediste {c} cancha(s) y no entra "
                    f"ninguna — a esa hora no quedan carnets libres.")
        for d in self.dias[1:]:
            for h, pids, cmax in self.turnos[d]:
                st["armadas"][(d, h)] = 0
                # si está fijado a mano se intenta ESE número y nada más
                intentos = ([self.fijas[(d, h)]] if (d, h) in self.fijas
                            else range(cmax, 0, -1))
                for c in intentos:
                    if not c:
                        break
                    foto = self._foto(st)
                    if self._armar_turno(st, d, h, c):
                        st["armadas"][(d, h)] = c
                        break
                    self._volver(st, foto)
                    if (d, h) in self.fijas:     # solo avisa lo que pediste vos
                        st["avisos"].append(
                            f"{DIAS[d]} {int(h[:2])} h: pediste {c} cancha(s) y no salen "
                            f"(no alcanzan los carnets libres a esa hora).")
                        break
        st["score"] = self._puntaje(st)
        return st

    @staticmethod
    def _foto(st):
        return ({k: list(v) for k, v in st["agenda"].tomado.items()},
                len(st["reservas"]), len(st["avisos"]), set(st["usados"]))

    @staticmethod
    def _volver(st, foto):
        st["agenda"].tomado = foto[0]
        del st["reservas"][foto[1]:]
        del st["avisos"][foto[2]:]
        st["usados"] = foto[3]

    def _puntaje(self, st):
        """Qué plan es mejor, en orden:
          1. más canchas;
          2. más GENTE DISTINTA que llega a jugar (dos planes con las mismas canchas
             no son iguales: el que deja a alguien afuera del finde entero es peor);
          3. más lugares de cancha jugados (el que juega dos veces suma);
          4. la prioridad de Diego: sáb tarde → dom mañana → sáb mañana → dom tarde.
        """
        a = st["armadas"]
        d1 = self.dias[0]
        d2 = self.dias[1] if len(self.dias) > 1 else None
        def suma(d, fr):
            if d is None:
                return 0
            return sum(c for (dd, h), c in a.items()
                       if dd == d and ES_MANANA(h) == (fr == "m"))
        gente, lugares = set(), 0
        for rsv in st["reservas"]:
            ids = {x["id"] for x in rsv["juegan"]}
            gente |= ids
            lugares += len(ids)
        # ⚠ REPARTIR entre horarios, no amontonar: 3 canchas todas a las 16 hacen que
        # cada uno juegue UNA vez, mientras que 15 + 17 encadenan (el turno de las 15
        # libera los carnets a las 16:00 y a las 16:10 esa gente entra a las 17). Sin
        # esto el optimizador metía todo en el horario más votado y dejaba las otras
        # horas vacías, que es justo lo que no quieren.
        horarios = sum(1 for c in a.values() if c)
        # la tarde del sábado manda (es el día de más gente): entre dos planes con la
        # misma cantidad de canchas, gana el que le da más al sábado a la tarde
        return (sum(a.values()), suma(d1, "t"), horarios, len(gente), lugares,
                suma(d2, "m"), suma(d1, "m"), suma(d2, "t"))

    # ------------------------------------------------------------------
    def _armar_turno(self, st, d, hora, canchas, con_avisos=True):
        fecha = self.fechas.get(str(d))
        if not fecha:
            return False
        _, votantes, _ = next(t for t in self.turnos[d] if t[0] == hora)
        # a quién ponerle el carnet en la reserva: primero los que lo tienen libre todo
        # el rango, y entre esos los que votan UNA sola hora (su carnet no le sirve a
        # ningún otro turno); el resto juega igual, rotando
        club = club_de(hora)
        turno = _dt(fecha, hora)
        apertura = turno - datetime.timedelta(minutes=APERTURA_ANTES_MIN)
        fin = turno + datetime.timedelta(minutes=DUR_TURNO_MIN)
        # ⚠ NO alcanza con separar "libre / no libre": entre los que tienen el carnet
        # tomado hay que mirar CUÁNDO se libera. El que juega a las 10 lo devuelve a las
        # 11:00 y llega de sobra al turno de las 16 con un cambio; el que juega a las 15
        # lo devuelve a las 16:00 en punto y NO llega. Tratarlos igual hacía perder
        # canchas enteras (lo cazó Diego: "una a las 10, una a las 15 y 2 a las 16 te
        # entran perfecto" — y entraban).
        margen = datetime.timedelta(seconds=MARGEN_CAMBIO_SEG)

        def libera(p):
            return st["agenda"].libre_desde(club, p, turno, apertura)

        posibles = [p for p in votantes
                    if st["agenda"].libre(club, p, apertura, fin) or libera(p) + margen < turno]
        orden = sorted(posibles, key=lambda p: (
            libera(p),                              # el que antes tiene el carnet libre
            self.n_horas.get((d, p), 1),            # menos horas votadas primero
            self._nombre(d, p).lower()))
        nombrados = orden[:canchas * POR_CANCHA]
        if len(nombrados) < canchas * POR_CANCHA:
            return False                            # no hay gente para tantas canchas
        # cada cancha necesita a alguien con cuenta de OnDepor ENTRE LOS QUE JUEGAN, o la
        # tiene que disparar un carnet prestado que después ocupa un lugar (cancha para 3).
        # Si el corte no dejó suficientes, se cambian los últimos por los que sí tienen.
        con = [p for p in nombrados if p in self.cuentas]
        if len(con) < canchas:
            afuera = [p for p in orden[canchas * POR_CANCHA:] if p in self.cuentas]
            sin = [p for p in reversed(nombrados) if p not in self.cuentas]
            while len(con) < canchas and afuera and sin:
                entra, sale = afuera.pop(0), sin.pop(0)
                nombrados[nombrados.index(sale)] = entra
                con.append(entra)
        rotan = [p for p in votantes if p not in nombrados]

        # repartir en canchas cuidando que cada una tenga al menos una cuenta
        grupos = [[] for _ in range(canchas)]
        con_cta = [p for p in nombrados if p in self.cuentas]
        sin_cta = [p for p in nombrados if p not in self.cuentas]
        for i, p in enumerate(con_cta):
            grupos[i % canchas].append(p)
        for p in sin_cta:
            grupos[min(range(canchas), key=lambda k: len(grupos[k]))].append(p)
        if any(len(g) != POR_CANCHA for g in grupos):
            # rebalancear los que sobraron de la vuelta de cuentas
            todos = [p for g in grupos for p in g]
            grupos = [todos[k::canchas] for k in range(canchas)]

        hechas = []
        for i, grupo in enumerate(grupos, 1):
            rsv = self._armar_reserva(st, d, fecha, hora, i, grupo,
                                      rotan if i == 1 else [])
            if rsv is None:
                return False
            hechas.append(rsv)
        st["reservas"] += hechas
        if len(rotan) > MAX_ROTAN and con_avisos:
            st["avisos"].append(
                f"{DIAS[d]} {int(hora[:2])} h: rotan {len(rotan)} y el máximo es "
                f"{MAX_ROTAN} — mirá las sugerencias para acomodarlo.")
        return True

    def _armar_reserva(self, st, d, fecha, hora, num, juegan, rotan):
        agenda = st["agenda"]
        club = club_de(hora)
        turno = _dt(fecha, hora)
        apertura = turno - datetime.timedelta(minutes=APERTURA_ANTES_MIN)
        fin = turno + datetime.timedelta(minutes=DUR_TURNO_MIN)
        avisos = []
        etiqueta = f"{DIAS[d]} {int(hora[:2])} h · cancha {num}"
        nom = lambda p: self._nombre(d, p)      # noqa: E731

        # ⚠ REGLA DE DIEGO: la cancha se PISA CON LOS CARNETS PRESTADOS. El carnet de
        # alguien que juega se toca solo cuando ya no queda prestado libre. Si se gastan
        # al revés, los carnets de los jugadores quedan tomados desde el viernes y no
        # sirven para los otros turnos; los prestados, en cambio, se liberan con el
        # cambio y vuelven a la bolsa.
        forzados = [p for p in juegan if not agenda.libre(club, p, apertura, fin)]

        # el cambio corre cuando se libera el ÚLTIMO de los que tienen el carnet tomado,
        # pero NUNCA antes de la mañana del día del turno (ver HORA_MIN_CAMBIO)
        piso = min(turno.replace(hour=HORA_MIN_CAMBIO, minute=0),
                   turno - datetime.timedelta(minutes=30))
        if forzados:
            # APENAS SE PUEDA: en cuanto se libera el último que entra. Para una cancha
            # del domingo eso suele caer el sábado a la tarde, y está bien: libera el
            # carnet del que la pisó (pedido de Diego).
            libres_en = [agenda.libre_desde(club, p, turno, apertura) for p in forzados]
            cambio_dt = max(libres_en) + datetime.timedelta(seconds=MARGEN_CAMBIO_SEG)
            if cambio_dt >= turno:
                return None                    # los que entran no se liberan a tiempo
        else:
            # ya están todos libres: no hay apuro, y hacerlo apenas se reserva sería
            # cancelar y volver a tomar la cancha recién ganada. Va el día del turno.
            cambio_dt = piso

        prestados = [p for p in self._pool_prestados(d)
                     if p not in juegan and agenda.libre(club, p, apertura, cambio_dt)]
        if len(prestados) < len(forzados):
            # a los que tienen el carnet tomado hay que pisarlos sí o sí; si no hay
            # prestados, se completa con el carnet suelto de algún otro jugador
            sueltos = [p for p in self._pool_sueltos(d)
                       if p not in juegan and agenda.libre(club, p, apertura, cambio_dt)]
            prestados += sueltos[:len(forzados) - len(prestados)]
            if len(prestados) < len(forzados):
                return None                    # no hay con qué pisar
        # ⚠ NO pisar de más: gastar un prestado en alguien que YA tiene el carnet libre
        # quema capacidad al pedo (ese prestado queda tomado desde el día anterior y no
        # sirve para otra cancha). Los prestados se guardan para los turnos donde los
        # jugadores tienen el carnet tomado, que es lo que permite sacar MÁS canchas:
        # medido con los votos reales, pisar de más baja de 6 canchas a 5.
        pisados = list(forzados)
        directos = [p for p in juegan if p not in pisados]
        # ⚠ los carnets prestados CON cuenta de OnDepor son escasos y son los únicos que
        # pueden disparar una cancha donde ninguno de los que juegan tiene cuenta. Si esta
        # cancha ya tiene cuenta entre los suyos, se pisa con prestados SIN cuenta y se
        # los guarda para las otras (si no, la primera cancha se los come y la siguiente
        # se queda sin con qué dispararse).
        hay_cuenta = any(p in self.cuentas for p in directos)
        prestados = sorted(prestados, key=lambda p: (p in self.cuentas) != (not hay_cuenta))
        pisadores = prestados[:len(pisados)]
        if not pisados:
            cambio_dt = None

        res_prestado = None      # se llena solo si la dispara un carnet prestado
        res_ini = next((p for p in directos if p in self.cuentas), None) \
            or next((p for p in pisadores if p in self.cuentas), None)
        if not res_ini:
            # nadie de los que juegan tiene cuenta de OnDepor. Como en el cronograma que
            # armó Diego a mano, la dispara un carnet PRESTADO que sí la tenga; como el
            # reservador no se puede sacar con el cambio, ocupa un lugar de la cancha y
            # sale el jugador que más horarios votó (es el que tiene más chances de
            # entrar en otro turno).
            res_ini = next((p for p in self._pool_pisadores(d)
                            if p in self.cuentas and p not in juegan
                            and agenda.libre(club, p, apertura, fin)), None)
            if res_ini is None:
                return None                    # no hay ninguna cuenta libre a esa hora
            res_prestado = res_ini
            fuera = max(juegan, key=lambda p: (self.n_horas.get((d, p), 1), nom(p).lower()))
            juegan = [p for p in juegan if p != fuera]
            rotan = list(rotan) + [fuera]
            directos = [p for p in directos if p != fuera]
            pisados = [p for p in pisados if p != fuera]
            if pisadores:                      # sobra un pisador: ya no hace falta
                pisadores = pisadores[:len(pisados)]
            avisos.append(f"{etiqueta}: ninguno de los que juegan tiene cuenta de OnDepor, "
                          f"así que la dispara {self.cuentas.get(res_ini, nom(res_ini))} "
                          f"con un carnet prestado y ocupa un lugar (queda para 3).")

        res_fin = res_ini if res_ini in juegan else \
            next((p for p in juegan if p in self.cuentas), None)
        cambio = None
        if pisados:
            if res_fin is None:
                # decisión de Diego: sin votante con cuenta el cambio queda a mano
                avisos.append(f"{etiqueta}: ninguno de los que juegan tiene cuenta de "
                              f"OnDepor: el cambio de carnets queda para hacer A MANO "
                              f"(entran {', '.join(self._nombre(d, p) for p in pisados)}).")
                cambio_dt = None
            else:
                cambio = {
                    "epoch": _epoch_ar(cambio_dt),
                    "hora_txt": (DIAS[cambio_dt.weekday()][:3].lower() + " "
                                 + cambio_dt.strftime("%H:%M")),
                    "reservador": self.cuentas.get(res_fin, self._nombre(d, res_fin)),
                    "jugadores": [p for p in juegan if p != res_fin],
                    "entran": pisados,
                }

        hasta_pisador = cambio_dt or fin
        for p in directos:
            agenda.ocupar(club, p, apertura, fin)
        for p in pisadores:
            agenda.ocupar(club, p, apertura, hasta_pisador)
        if cambio_dt:
            for p in pisados:
                agenda.ocupar(club, p, cambio_dt, fin)
        st["usados"] |= set(directos) | set(pisadores) | (set(pisados) if cambio_dt else set())
        if res_prestado:
            # el reservador prestado no está en directos ni en pisadores: si no se anota
            # acá, su carnet queda "libre" y termina disparando dos canchas a la vez
            agenda.ocupar(club, res_prestado, apertura, fin)
            st["usados"].add(res_prestado)

        titulares = [p for p in directos + pisadores if p != res_ini]
        return {
            "fecha": fecha, "hora": hora, "club_id": club, "dia": d,
            "franja": "m" if ES_MANANA(hora) else "t",
            "cancha_num": num,
            "bloque": f"{DIAS[d]} · {'mañana' if ES_MANANA(hora) else 'tarde'}",
            "apertura_txt": apertura.strftime("%d/%m %H:%M"),
            "reservador": {"id": res_ini, "nombre": self.cuentas.get(res_ini, nom(res_ini))},
            "titulares": [{"id": p, "nombre": nom(p), "pisa": p in pisadores}
                          for p in titulares][:POR_CANCHA - 1],
            "juegan": [{"id": p, "nombre": nom(p)} for p in juegan],
            "rotan": [{"id": p, "nombre": nom(p)} for p in rotan],
            "cambio": cambio,
            "avisos": avisos,
        }

    # ------------------------------------------------------------------
    def _sugerencias(self, plan):
        """Para Darío: dónde un cambio chico de horarios daría más canchas u horas.
        Se fija en horas con poca gente, en horas que quedaron sin canchas por falta
        de carnets, y en turnos donde rotan más de 2."""
        sug = []
        armadas = plan.get("armadas", {})
        for d in self.dias:
            for h, pids, cmax in self.turnos[d]:
                v, c = len(pids), armadas.get((d, h), 0)
                vecinos = [p for hh, pp, _ in self.turnos[d] if hh != h
                           and ES_MANANA(hh) == ES_MANANA(h) for p in pp if p not in pids]
                nombres = sorted({self._nombre(d, p) for p in vecinos})[:5]
                if 0 < v < POR_CANCHA:
                    sug.append(f"{DIAS[d]} {int(h[:2])} h: hay {v} y hacen falta 4 para "
                               f"abrir una cancha. Podrían sumarse: "
                               f"{', '.join(nombres) or 'nadie de horas vecinas'}.")
                elif c and v - c * POR_CANCHA == 3:
                    sug.append(f"{DIAS[d]} {int(h[:2])} h: rotan 3 (máximo 2). Con UNO "
                               f"más se abre otra cancha"
                               + (f" (candidatos: {', '.join(nombres)})" if nombres else "")
                               + "; si no, conviene que uno cambie de horario.")
                elif cmax and c < cmax:
                    # OJO: no siempre es por falta de carnets. Si ese día se armaron
                    # canchas en OTRO horario, fue una elección del armador y hay que
                    # decirlo así (antes mentía y mandaba a buscar carnets al pedo).
                    otras = sum(v for (dd, hh), v in armadas.items()
                                if dd == d and hh != h and v)
                    if not c and otras:
                        sug.append(f"{DIAS[d]} {int(h[:2])} h: se anotaron {v} (darían "
                                   f"{cmax} cancha(s)) pero las canchas de ese día se "
                                   f"armaron en otros horarios. Si las querés acá, "
                                   f"fijalo arriba.")
                    else:
                        sug.append(f"{DIAS[d]} {int(h[:2])} h: se anotaron {v} (darían "
                                   f"{cmax} cancha(s)) y salieron {c}: a esa hora no "
                                   f"quedaban carnets libres para más.")
        return sug


def verificar_plan(reservas, resumen, e):
    """Barrido final sobre el plan armado: si algo viola las dos reglas duras, lo
    marca. No debería saltar nunca — está para que un bug no pase inadvertido.
      1. un carnet no puede estar en dos reservas que se pisen en el tiempo;
      2. cuando se juega, en la cancha están los que votaron ESE horario."""
    ocupa = {}
    for rsv in reservas:
        club = rsv["club_id"]
        turno = _dt(rsv["fecha"], rsv["hora"])
        apertura = turno - datetime.timedelta(minutes=APERTURA_ANTES_MIN)
        fin = turno + datetime.timedelta(minutes=DUR_TURNO_MIN)
        cambio = rsv.get("cambio")
        cambio_dt = None
        if cambio:
            cambio_dt = apertura + (
                datetime.timedelta(seconds=cambio["epoch"] - _epoch_ar(apertura)))
        votantes = set(resumen["por_dia"].get(rsv["dia"], {})
                       .get("por_hora_ids", {}).get(rsv["hora"], []))
        finales = {x["id"] for x in rsv["juegan"]}
        colados = [x["nombre"] for x in rsv["juegan"] if x["id"] not in votantes]
        if colados:
            rsv["avisos"].append(f"⚠ VERIFICACIÓN: {', '.join(colados)} queda(n) en la "
                                 f"cancha sin haber votado ese horario.")
        tramos = [(rsv["reservador"]["id"], apertura,
                   fin if rsv["reservador"]["id"] in finales or not cambio_dt else cambio_dt)]
        for x in rsv["titulares"]:
            if x["pisa"]:
                tramos.append((x["id"], apertura, cambio_dt or fin))
            else:
                tramos.append((x["id"], apertura, fin))
        if cambio_dt:
            for p in cambio.get("entran", []):
                tramos.append((p, cambio_dt, fin))
        for pid, desde, hasta in tramos:
            for od, oh, oq in ocupa.get((club, pid), ()):
                if desde < oh and od < hasta:
                    rsv["avisos"].append(f"⚠ VERIFICACIÓN: un carnet quedó en dos "
                                         f"reservas a la vez (también en {oq}). Es un "
                                         f"error del armador, no programar así.")
                    break
            ocupa.setdefault((club, pid), []).append(
                (desde, hasta, f"{DIAS[rsv['dia']]} {rsv['hora']}"))
    return reservas
