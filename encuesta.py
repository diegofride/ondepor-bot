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
from ondepor_core import hoy_ar, ahora_ar

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

    ⚠ Se pregunta por un RANGO, no por un instante. Preguntar "¿está libre cuando
    reservo?" no alcanza: un carnet que se mete en el turno de las 9 con el cambio de
    las 8:50 estaba libre el viernes a las 10:01, pero igual no se puede usar para
    pisar el turno de las 10, porque esa reserva lo tiene tomado desde el viernes y se
    pisarían entre ellas. Hay que mirar todo el rato que va a quedar tomado.
    """

    def __init__(self):
        self.tomado = {}

    def libre(self, club, pid, desde, hasta):
        """¿El carnet está libre TODO el rango [desde, hasta)?"""
        return all(hasta <= a or desde >= b for a, b in self.tomado.get((club, pid), ()))

    def ocupar(self, club, pid, desde, hasta):
        self.tomado.setdefault((club, pid), []).append((desde, hasta))


def hora_mas_votada(por_hora_ids):
    """La hora con más gente anotada; si empatan, la más temprana."""
    if not por_hora_ids:
        return None
    return sorted(por_hora_ids.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][0]


def armar_cronograma(e, elecciones=None):
    """Propone qué canchas reservar, con qué carnets y quién las dispara.

    elecciones: {"5": ["10:00"], "6": ["16:00"]}; si un día no viene, se toma su
    horario más votado. Devuelve los turnos en orden cronológico, listos para que
    el admin los revise ANTES de que se programe nada.
    """
    r = resumen_votos(e)
    fechas = e.get("fechas", {})
    reservadores, sin_vincular = reservadores_con_carnet()

    # ---- 1) qué turnos se juegan ----
    turnos = []
    for d in e.get("dias", DIAS_POR_DEFECTO):
        info = r["por_dia"].get(d, {})
        por_hora = info.get("por_hora_ids", {})
        horas = (elecciones or {}).get(str(d))
        if horas is None:
            mejor = hora_mas_votada(por_hora)
            horas = [mejor] if mejor else []
        fecha = fechas.get(str(d))
        for h in horas:
            if h and fecha and h in por_hora:
                turnos.append({"dia": d, "fecha": fecha, "hora": h})
    turnos.sort(key=lambda t: (t["fecha"], t["hora"]))

    # ---- 2) repartir carnets turno por turno, en orden ----
    agenda = _Agenda()
    salida = []
    for t in turnos:
        salida.append(_armar_turno(t, r, reservadores, agenda))
    verificar_plan(salida, r)
    return {"turnos": salida, "resumen": r, "sin_vincular": sin_vincular}


def verificar_plan(turnos, resumen):
    """Repasa el plan ya armado y marca lo que no cierra, con el motivo.

    Son las dos reglas que tienen que valer sí o sí, chequeadas sobre el resultado y no
    sobre el proceso (si el armador tuviera un error, acá se ve):
      1. un carnet no puede estar en dos reservas que se pisan en el tiempo;
      2. a la hora del turno, en la cancha tienen que estar los que pidieron ESE turno.
    """
    ocupa = {}                       # (club, pid) -> [(desde, hasta, "de qué turno")]
    for t in turnos:
        if not t["canchas"]:
            continue
        club = t["club_id"]
        turno_dt = _dt(t["fecha"], t["hora"])
        apertura = turno_dt - datetime.timedelta(minutes=APERTURA_ANTES_MIN)
        cambio = turno_dt - datetime.timedelta(minutes=CAMBIO_ANTES_MIN)
        fin = turno_dt + datetime.timedelta(minutes=DUR_TURNO_MIN)
        anotados = {p["id"] for p in t["anotados"]}
        etiqueta = f"{DIAS[t['dia']]} {t['hora']}"

        for i, c in enumerate(t["detalle"], 1):
            finales = {x["id"] for x in c["finales"]} | {c["reservador"]["id"]}
            # --- regla 2: en la cancha juegan los que pidieron el turno ---
            colados = [x["nombre"] for x in c["finales"] + [c["reservador"]]
                       if x["id"] not in anotados]
            if colados:
                t["avisos"].append(
                    f"Cancha {i}: a la hora del turno queda adentro {', '.join(colados)}, "
                    f"que no se anotó a esa hora. Faltan jugadores con el carnet libre.")
            # --- regla 1: nadie en dos reservas a la vez ---
            tramos = [(c["reservador"]["id"], apertura, fin)]
            for x in c["titulares"]:
                hasta = fin if x["id"] in finales else cambio
                tramos.append((x["id"], apertura, hasta))
            for x in c["finales"]:
                if x["id"] not in {y["id"] for y in c["titulares"]}:
                    tramos.append((x["id"], cambio, fin))
            for pid, desde, hasta in tramos:
                for od, oh, oq in ocupa.get((club, pid), ()):
                    if desde < oh and od < hasta:
                        nombre = next((x["nombre"] for x in c["titulares"] + c["finales"]
                                       + [c["reservador"]] if x["id"] == pid), pid)
                        t["avisos"].append(
                            f"Cancha {i}: el carnet de {nombre} ya está usado en "
                            f"{oq} y todavía no se liberó. NO se puede reservar así.")
                        break
                ocupa.setdefault((club, pid), []).append((desde, hasta, etiqueta))
    return turnos


def _armar_turno(t, r, reservadores, agenda):
    info = r["por_dia"][t["dia"]]
    nombres = {**info["bolsa"]}
    club = club_de(t["hora"])
    turno_dt = _dt(t["fecha"], t["hora"])
    apertura = turno_dt - datetime.timedelta(minutes=APERTURA_ANTES_MIN)
    cambio_dt = turno_dt - datetime.timedelta(minutes=CAMBIO_ANTES_MIN)
    fin_dt = turno_dt + datetime.timedelta(minutes=DUR_TURNO_MIN)

    anotados = list(info["por_hora_ids"].get(t["hora"], []))
    avisos = []

    # --- carnets libres, mirando TODO el rato que van a quedar tomados ---
    # el que PISA y después lo sacan ocupa desde que se reserva hasta el cambio;
    # el que se queda adentro (reservador o jugador) ocupa hasta que termina el turno
    def libre_pisando(p):
        return agenda.libre(club, p, apertura, cambio_dt)

    def libre_hasta_el_final(p):
        return agenda.libre(club, p, apertura, fin_dt)

    prestan_libres = [p for p in info["prestan"] if p not in anotados and libre_pisando(p)]
    extra_libres = [p for p in info["extra"]
                    if p not in anotados and p not in info["prestan"] and libre_pisando(p)]
    # un jugador que pisa NO se saca después (juega), así que necesita el rango completo
    juegan_libres = [p for p in anotados if libre_hasta_el_final(p)]
    # ORDEN QUE IMPORTA (regla de Diego): para PISAR la cancha van primero los carnets
    # PRESTADOS, y recién si no alcanzan se toca el de alguien que juega. Es al revés de
    # lo que parece cómodo: el prestado se libera con el cambio de nombres (turno −10 min)
    # y el del que juega recién cuando termina el turno, así que gastar los de los
    # jugadores deja sin carnets al turno siguiente y al día siguiente.
    prestados_libres = prestan_libres + extra_libres
    disponibles = prestados_libres + juegan_libres

    # --- cuántas canchas: la regla, los carnets y las cuentas de OnDepor ---
    tope_regla = canchas_para(len(anotados))
    tope_carnets = len(disponibles) // POR_CANCHA
    # el reservador queda sí o sí en la reserva hasta que termina el turno (el cambio no
    # lo puede sacar), así que necesita el rango completo y conviene que sea de los que juegan
    res_libres = ([p for p in anotados if p in reservadores and libre_hasta_el_final(p)]
                  + [p for p in disponibles if p in reservadores and p not in anotados
                     and libre_hasta_el_final(p)])
    tope_res = len(res_libres)
    canchas = min(tope_regla, tope_carnets, tope_res)

    def _canchas_txt(n):
        return "1 cancha" if n == 1 else f"{n} canchas"

    if canchas < tope_regla:
        if tope_carnets < tope_regla and tope_carnets <= tope_res:
            alcanzan = ("ninguna" if not tope_carnets else _canchas_txt(tope_carnets))
            avisos.append(f"Se anotaron {len(anotados)} (dan para {_canchas_txt(tope_regla)}) "
                          f"pero los carnets libres alcanzan para {alcanzan}.")
        if tope_res < tope_regla and tope_res < tope_carnets:
            avisos.append("Ninguno de los que tienen cuenta de OnDepor tiene el carnet libre a esa "
                          "hora: no hay con quién disparar la reserva."
                          if not tope_res else
                          f"Solo hay {tope_res} reservador(es) con el carnet libre: no se puede "
                          f"disparar más de {_canchas_txt(tope_res)}.")

    tarde = apertura <= ahora_ar()          # la apertura ya pasó: no se puede programar
    if tarde:
        avisos.append(f"El turno se habilitó el {apertura.strftime('%d/%m a las %H:%M')} y ya pasó: "
                      f"esta reserva no se puede programar (probá reservarla a mano).")
    base = {"dia": t["dia"], "fecha": t["fecha"], "hora": t["hora"], "club_id": club,
            "anotados": [{"id": p, "nombre": nombres.get(p, p)} for p in anotados],
            "canchas": canchas, "tope_regla": tope_regla, "tope_carnets": tope_carnets,
            "tope_reservadores": tope_res, "tarde": tarde,
            "cambio_hora": cambio_dt.strftime("%H:%M"),
            "apertura": apertura.strftime("%d/%m %H:%M"),
            "detalle": [], "rotan": [], "sin_usar": [], "avisos": avisos}
    if not canchas:
        if len(anotados) < 4:
            avisos.append(f"Solo {len(anotados)} anotado(s): no llega a una cancha.")
        return base

    # --- 3) elegir los carnets con los que se RESERVA (se pisa con los prestados) ---
    elegidos_res = res_libres[:canchas]
    resto = [p for p in disponibles if p not in elegidos_res]
    acompanan = resto[:canchas * (POR_CANCHA - 1)]

    # --- 4) quién ocupa la cancha a la hora del turno (después del cambio) ---
    # el que entra con el cambio ocupa desde el cambio hasta que termina el turno; a esa
    # altura ya se liberaron carnets de turnos anteriores, así que puede entrar gente
    # que al momento de reservar tenía el carnet tomado
    puestos = canchas * (POR_CANCHA - 1)              # el reservador ocupa el 4º lugar
    jugadores_finales = ([p for p in acompanan if p in anotados]
                         + [p for p in anotados
                            if p not in acompanan and p not in elegidos_res
                            and agenda.libre(club, p, cambio_dt, fin_dt)])
    jugadores_finales = jugadores_finales[:puestos]
    faltan = puestos - len(jugadores_finales)
    if faltan > 0:
        # no hay con quién reemplazar: el prestado se queda adentro hasta que termina el
        # turno, así que solo sirve si su carnet está libre también en ese rato
        relleno = [p for p in acompanan if p not in jugadores_finales
                   and agenda.libre(club, p, cambio_dt, fin_dt)][:faltan]
        jugadores_finales += relleno
        avisos.append(f"Quedan {len(relleno)} carnet(s) prestados dentro de la reserva: no hay "
                      f"suficientes jugadores con el carnet libre para reemplazarlos."
                      if relleno else
                      "No hay jugadores con el carnet libre para completar la cancha a la hora "
                      "del turno: revisala a mano.")

    # --- 5) repartir en canchas ---
    detalle = []
    for k in range(canchas):
        res_id = elegidos_res[k]
        titulares = acompanan[k::canchas]
        finales = jugadores_finales[k::canchas]
        juega_el_res = res_id in anotados
        if not juega_el_res:
            avisos.append(f"{nombres.get(res_id, res_id)} reserva pero no juega: ocupa un lugar "
                          f"de esa cancha (queda para 3 jugadores).")
        detalle.append({
            "reservador": {"id": res_id, "nombre": reservadores.get(res_id, nombres.get(res_id, res_id)),
                           "juega": juega_el_res},
            "titulares": [{"id": p, "nombre": nombres.get(p, p), "prestado": p not in anotados}
                          for p in titulares],
            "finales": [{"id": p, "nombre": nombres.get(p, p)} for p in finales],
            # solo hace falta programar el cambio si entra alguien distinto (el orden no importa)
            "hay_cambio": set(titulares) != set(finales),
        })

    # --- 6) anotar hasta cuándo queda tomado cada carnet ---
    for p in elegidos_res + acompanan:
        # el que se queda adentro al momento del turno lo devuelve recién al terminar
        queda = p in elegidos_res or p in jugadores_finales
        agenda.ocupar(club, p, apertura, fin_dt if queda else cambio_dt)
    for p in jugadores_finales:
        if p not in acompanan and p not in elegidos_res:
            agenda.ocupar(club, p, cambio_dt, fin_dt)   # entra con el cambio

    usados = set(elegidos_res) | set(acompanan) | set(jugadores_finales)
    base["detalle"] = detalle
    base["rotan"] = [{"id": p, "nombre": nombres.get(p, p)}
                     for p in anotados if p not in usados]
    base["sin_usar"] = [{"id": p, "nombre": nombres.get(p, p)}
                        for p in info["bolsa"] if p not in usados]
    return base
