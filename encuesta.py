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
    cuerpo = f"""
    <div class="hola"><span>Hola, <b>{escape(yo["nombre"])}</b>
      {"· ya votaste, podés cambiarlo" if voto else ""}</span>
      <a href="{url_for("encuesta.no_soy", token=token)}">no soy yo</a></div>
    <form method="post" action="{url_for("encuesta.votar", token=token)}">
      {dias_html}
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
    for d in e.get("dias", DIAS_POR_DEFECTO):
        modo = request.form.get(f"m{d}", "")
        if modo not in (MODO_JUEGO, MODO_PRESTA, MODO_NO):
            continue                                   # no contestó ese día
        entrada = {"modo": modo}
        if modo == MODO_JUEGO:
            horas = [h for h in request.form.getlist(f"h{d}")
                     if h in e.get("horas", HORAS_POR_DEFECTO)]
            if not horas:                              # dijo "juego" pero no marcó hora
                continue
            entrada["horas"] = sorted(horas)
        dias[str(d)] = entrada
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
        bolsa = {**juegan, **prestan, **extra}          # los extra sirven todos los días
        ids = {h: sorted(g, key=lambda p: juegan.get(p, "").lower())
               for h, g in sorted(por_hora.items())}
        por_dia[d] = {
            "por_hora_ids": ids,
            "por_hora": {h: [juegan[p] for p in g] for h, g in ids.items()},
            "juegan": juegan, "prestan": prestan, "extra": extra, "bolsa": bolsa,
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


def repartir_franja(opciones):
    """Reparte a cada persona en UNA de las horas que votó dentro de la franja
    (mañana u tarde). Trata de completar canchas de a 4 y vacía las horas que
    quedan con menos de 4. Devuelve ({hora: [pids]}, afuera)."""
    cnt, eleccion = {}, {}
    # primero los que votaron una sola hora: no tienen a dónde moverse
    for pid, hs in sorted(opciones.items(), key=lambda kv: (len(kv[1]), str(kv[0]))):
        h = min(sorted(hs), key=lambda x: (cnt.get(x, 0) % 4 != 3, cnt.get(x, 0)))
        eleccion[pid] = h
        cnt[h] = cnt.get(h, 0) + 1
    # horas cortas (<4): mover su gente a otra hora que haya votado
    moviendo = True
    while moviendo:
        moviendo = False
        for h in [x for x in cnt if 0 < cnt[x] < 4]:
            for pid in [p for p, x in eleccion.items() if x == h]:
                otras = [x for x in opciones[pid] if x != h and cnt.get(x, 0) >= 4]
                if otras:
                    dest = min(otras, key=lambda x: cnt[x])
                    eleccion[pid] = dest
                    cnt[h] -= 1
                    cnt[dest] += 1
                    moviendo = True
    afuera = []
    for h in [x for x in cnt if 0 < cnt[x] < 4]:
        for pid in [p for p, x in list(eleccion.items()) if x == h]:
            afuera.append(pid)
            del eleccion[pid]
    out = {}
    for pid, h in eleccion.items():
        out.setdefault(h, []).append(pid)
    return {h: sorted(v) for h, v in sorted(out.items())}, afuera


ES_MANANA = lambda h: int(str(h)[:2]) < 13          # noqa: E731
MARGEN_CAMBIO_SEG = 300                             # el cambio corre 5 min después de liberarse


def armar_cronograma(e, elecciones=None):
    """Arma el flujo de reservas del finde entero según el modelo de Darío/Diego
    (ver ARMADOR_CRONOGRAMA.md). Devuelve las reservas en orden cronológico con sus
    cambios de carnets programados; nada se toca hasta que el admin aprueba.

    El corazón: TODAS las reservas del sábado se hacen el viernes (carnets disjuntos)
    y las del domingo el sábado. Los cambios van apenas se liberan los carnets de los
    que entran. Prioridad de carnets (elegida por Diego):
        sáb tarde → dom mañana → sáb mañana → dom tarde
    'elecciones' se acepta por compatibilidad y se ignora (el reparto es automático).
    """
    r = resumen_votos(e)
    reservadores, sin_vincular = reservadores_con_carnet()
    a = _Armador(e, r, reservadores)
    a.armar()
    verificar_plan(a.reservas, r, e)
    return {"reservas": a.reservas, "avisos": a.avisos, "sobran": a.sobran(),
            "resumen": r, "sin_vincular": sin_vincular}


class _Armador:
    def __init__(self, e, r, reservadores):
        self.e, self.r, self.cuentas = e, r, reservadores
        self.agenda = _Agenda()
        self.reservas = []
        self.avisos = []                 # avisos globales
        self.usados = set()              # pids que tocaron alguna reserva
        self.dias = list(e.get("dias", DIAS_POR_DEFECTO))
        self.fechas = e.get("fechas", {})
        # reparto de personas por (día, franja): mañana y tarde POR SEPARADO
        # (decisión de Diego: el que votó ambas puede jugar dos veces).
        # La TARDE se reparte entre sus horarios; la MAÑANA va en UN solo horario,
        # el más votado (regla de Darío: la mañana es 1 cancha salvo sobra).
        self.asig = {}
        self.hora_maniana = {}
        for d in self.dias:
            por_hora = self.r["por_dia"].get(d, {}).get("por_hora_ids", {})
            manianas = {h: pids for h, pids in por_hora.items() if ES_MANANA(h)}
            if manianas:
                mejor = sorted(manianas.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][0]
                self.hora_maniana[d] = mejor
                self.asig[(d, "m")] = {mejor: sorted(manianas[mejor])}
                if len(manianas) > 1:
                    self.avisos.append(f"{DIAS[d]} a la mañana se juega en un solo "
                                       f"horario, el más votado: {int(mejor[:2])} h.")
            votos = {}
            for h, pids in por_hora.items():
                if not ES_MANANA(h):
                    for p in pids:
                        votos.setdefault(p, []).append(h)
            self.asig[(d, "t")], afuera = repartir_franja(votos)
            for p in afuera:
                self.avisos.append(f"{self._nombre(d, p)} quedó sin lugar el "
                                   f"{DIAS[d].lower()} a la tarde: en sus horarios "
                                   f"no se juntan 4.")

    # ------------------------------------------------------------------
    def _nombre(self, d, pid):
        return self.r["por_dia"].get(d, {}).get("bolsa", {}).get(pid) \
            or self.cuentas.get(pid) or str(pid)

    def _pool_pisadores(self, d):
        """Carnets para PISAR, en orden: prestados → de afuera → carnets sueltos de
        gente que juega pero quedó rotando (su carnet no está en ninguna reserva)."""
        info = self.r["por_dia"].get(d, {})
        prestan = [p for p in info.get("prestan", {})]
        extra = [p for p in info.get("extra", {}) if p not in prestan]
        sueltos = [p for p in info.get("juegan", {})
                   if p not in prestan and p not in extra]
        return prestan + extra + sueltos

    def armar(self):
        # orden de PRIORIDAD (Diego): sáb tarde, dom mañana, sáb mañana, dom tarde
        if len(self.dias) >= 2:
            sab, dom = self.dias[0], self.dias[1]
            orden = [(sab, "t"), (dom, "m"), (sab, "m"), (dom, "t")]
        else:
            orden = [(self.dias[0], "t"), (self.dias[0], "m")]
        for d, fr in orden:
            self._armar_franja(d, fr)
        self.reservas.sort(key=lambda x: (x["fecha"], x["hora"], x["cancha_num"]))

    def sobran(self):
        """Carnets de la bolsa (todos los días) que no tocaron ninguna reserva."""
        todos = {}
        for d in self.dias:
            todos.update(self.r["por_dia"].get(d, {}).get("bolsa", {}))
        return sorted(n for p, n in todos.items() if p not in self.usados)

    # ------------------------------------------------------------------
    def _armar_franja(self, d, fr):
        fecha = self.fechas.get(str(d))
        if not fecha:
            return
        for hora, pids in self.asig.get((d, fr), {}).items():
            canchas = canchas_para(len(pids))
            if fr == "m" and canchas > 1:
                # regla de Darío: la mañana va con 1 cancha salvo que sobren carnets;
                # la prioridad siempre es la tarde
                self.avisos.append(f"{DIAS[d]} {int(hora[:2])} h: por cantidad darían "
                                   f"{canchas} canchas pero la mañana se arma con 1 "
                                   f"(la prioridad es la tarde).")
                canchas = 1
            if not canchas:
                continue
            # orden de titularidad en la tarde: primero los que NO juegan también la
            # mañana (sus carnets no le hacen falta a nadie más), y de los que sí
            # juegan la mañana, los que tienen cuenta de OnDepor van ÚLTIMOS: que
            # roten acá y su carnet quede libre para poder DISPARAR la mañana
            en_maniana = set()
            if fr == "t":
                for g in self.asig.get((d, "m"), {}).values():
                    en_maniana.update(g)
            pids = sorted(pids, key=lambda p: (
                (2 if p in self.cuentas else 1) if p in en_maniana else 0,
                p not in self.cuentas,
                self._nombre(d, p).lower()))
            grupos = [pids[k::canchas] for k in range(canchas)]
            for i, grupo in enumerate(grupos, 1):
                rsv = self._armar_reserva(d, fr, fecha, hora, i, grupo)
                if rsv:
                    self.reservas.append(rsv)

    # ------------------------------------------------------------------
    def _armar_reserva(self, d, fr, fecha, hora, num, grupo):
        club = club_de(hora)
        turno = _dt(fecha, hora)
        apertura = turno - datetime.timedelta(minutes=APERTURA_ANTES_MIN)
        fin = turno + datetime.timedelta(minutes=DUR_TURNO_MIN)
        avisos = []
        etiqueta = f"{DIAS[d]} {int(hora[:2])} h · cancha {num}"

        # --- los 4 que JUEGAN (los de la encuesta); el resto del grupo rota adentro ---
        # van primero los que tienen el carnet LIBRE todo el rango (entran directo, sin
        # cambio) y entre ellos los que tienen cuenta de OnDepor (alguien tiene que
        # disparar); el orden del grupo ya trae las preferencias de la franja
        orden_base = {p: k for k, p in enumerate(grupo)}
        grupo = sorted(grupo, key=lambda p: (not self.agenda.libre(club, p, apertura, fin),
                                             orden_base[p]))
        juegan = grupo[:POR_CANCHA]
        rotan = grupo[POR_CANCHA:]
        # la cancha necesita al menos una cuenta de OnDepor entre los que juegan (para
        # disparar o retomar la reserva): si no quedó ninguna, entra una desde los que
        # rotan en lugar del último titular
        if not any(p in self.cuentas for p in juegan):
            cand = next((p for p in rotan if p in self.cuentas), None)
            if cand:
                rotan[rotan.index(cand)] = juegan[-1]
                juegan[-1] = cand

        # --- carnet por carnet: directo si está libre hasta el final, si no se pisa ---
        directos = [p for p in juegan
                    if self.agenda.libre(club, p, apertura, fin)]
        pisados = [p for p in juegan if p not in directos]

        cambio_dt = None
        if pisados:
            # ¿los que entran llegan a liberarse ANTES del turno? Esta condición sola
            # produce la regla de Darío "la mañana del sábado no se pisa": lo que tiene
            # tomado a un jugador del sáb a la mañana es la tarde de ese mismo día, que
            # termina después → acá da "no se arma". El DOMINGO a la mañana en cambio
            # SÍ se pisa (comodines): sus jugadores se liberan el sábado a la tarde.
            libres_en = [self.agenda.libre_desde(club, p, turno, apertura) for p in pisados]
            cambio_dt = max(libres_en) + datetime.timedelta(seconds=MARGEN_CAMBIO_SEG)
            if cambio_dt >= turno:
                avisos.append(f"{etiqueta}: {', '.join(self._nombre(d, p) for p in pisados)} "
                              f"no se libera(n) antes del turno: no se arma esta cancha.")
                self.avisos += avisos
                return None

        # --- pisadores para los huecos ---
        pisadores = []
        if pisados:
            pool = [p for p in self._pool_pisadores(d)
                    if p not in juegan and p not in pisadores
                    and self.agenda.libre(club, p, apertura, cambio_dt)]
            pisadores = pool[:len(pisados)]
            if len(pisadores) < len(pisados):
                faltan = len(pisados) - len(pisadores)
                avisos.append(f"{etiqueta}: no se arma — faltan {faltan} carnet(s) "
                              f"para pisar (prestados y libres ya están todos usados).")
                self.avisos += avisos
                return None

        # --- quién dispara la reserva inicial (viernes/sábado a la apertura) ---
        res_ini = next((p for p in directos if p in self.cuentas), None) \
            or next((p for p in pisadores if p in self.cuentas), None)
        if not res_ini:
            avisos.append(f"{etiqueta}: no se arma — ni los titulares ni los que pisan "
                          f"tienen cuenta de OnDepor para disparar la reserva.")
            self.avisos += avisos
            return None

        # --- quién queda de reservador cuando se juega (el cambio re-toma la reserva) ---
        res_fin = res_ini if res_ini in juegan else \
            next((p for p in juegan if p in self.cuentas), None)
        cambio = None
        if pisados:
            if res_fin is None:
                # decisión de Diego: sin votante con cuenta NO se programa el cambio;
                # queda avisado y lo resuelven a mano
                avisos.append(f"{etiqueta}: ninguno de los que juegan tiene cuenta de "
                              f"OnDepor: el cambio de carnets queda para hacer A MANO "
                              f"(entran {', '.join(self._nombre(d, p) for p in pisados)}).")
                cambio_dt = None       # los pisadores quedan adentro hasta el final
            else:
                cambio = {
                    "epoch": _epoch_ar(cambio_dt),
                    "hora_txt": (DIAS[cambio_dt.weekday()][:3].lower() + " "
                                 + cambio_dt.strftime("%H:%M")),
                    "reservador": self.cuentas.get(res_fin, self._nombre(d, res_fin)),
                    "jugadores": [p for p in juegan if p != res_fin],
                    "entran": pisados,
                }

        # --- anotar las ventanas en la agenda ---
        hasta_pisador = cambio_dt or fin
        for p in directos:
            self.agenda.ocupar(club, p, apertura, fin)
        for p in pisadores:
            self.agenda.ocupar(club, p, apertura, hasta_pisador)
        for p in pisados:
            if cambio_dt:
                self.agenda.ocupar(club, p, cambio_dt, fin)
        self.usados |= set(directos) | set(pisadores) | (set(pisados) if cambio_dt else set())

        titulares_ini = [p for p in directos + pisadores if p != res_ini]
        nom = lambda p: self._nombre(d, p)      # noqa: E731
        return {
            "fecha": fecha, "hora": hora, "club_id": club, "dia": d, "franja": fr,
            "cancha_num": num, "bloque": f"{DIAS[d]} · {'mañana' if fr == 'm' else 'tarde'}",
            "apertura_txt": apertura.strftime("%d/%m %H:%M"),
            "reservador": {"id": res_ini, "nombre": self.cuentas.get(res_ini, nom(res_ini))},
            "titulares": [{"id": p, "nombre": nom(p), "pisa": p in pisadores}
                          for p in titulares_ini][:POR_CANCHA - 1],
            "juegan": [{"id": p, "nombre": nom(p)} for p in juegan],
            "rotan": [{"id": p, "nombre": nom(p)} for p in rotan],
            "cambio": cambio,
            "avisos": avisos,
        }


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
