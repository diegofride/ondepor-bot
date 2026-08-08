# -*- coding: utf-8 -*-
"""
==========================================================================
  ondepor_core.py  -  MOTOR de reservas OnDepor (login + reserva exacta)
==========================================================================
Reproduce EXACTAMENTE el proceso de reserva del navegador (confirmado con
capturas reales), sin verificar disponibilidad:

  1. LOGIN:            POST /app/api/login   (usuario + clave, form url-encoded)
                       -> luego se lee 'svid' (site_version_id) y el token CSRF
                          del HTML de /user/_favorites.
  2. ABRIR FORMULARIO: POST /player/reservation/my-reservation-new
                       -> devuelve 'jid' (id de reserva que genera el servidor).
  3. CANCHAS:          POST /player/reservation/courtsavailable
                       -> lista de canchas {key: court_id, value: nombre}.
  4. CONFIRMAR:        POST /player/reservation/add-reservation-new
                       -> con svid, jid, court_id y los player_id[] de los otros
                          jugadores (el que loguea es el jugador 1).

CLAVE: TODOS los POST de /player llevan 'svid' y el token CSRF; sin 'svid'
OnDepor redirige a la home (era el bug que devolvia 302).

id del turno = "time-HH:MM-club-<club_id>-<epoch>"  donde <epoch> es la
medianoche (hora Argentina) de la fecha del turno.

Requisitos:  pip install requests ntplib
==========================================================================
"""
import re
import json
import time
import datetime
import threading
import concurrent.futures

import requests
try:
    import ntplib
except ImportError:
    ntplib = None

BASE = "https://ondepor.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SVID_FALLBACK = "2.1.1"   # version del sitio; se lee del HTML, esto es por si falla
PAUSA_PASO = 0.35         # respiro entre pasos del flujo de reserva (evita el "refresca tu pantalla")
PAUSA_RAPIDA = 0.05       # pausa mínima para el disparo a la apertura (jugadores ya pre-registrados)

# -- Tick de la apertura: cuánto insistir con la cancha PEDIDA --------------
# 07/08/2026: se pidieron las canchas 9 y 10 y las dos volvieron "ocupada" DESPUÉS de
# ~1,3 s — lo mismo que tarda una reserva que ENTRA (la 11 entró en 1,284 s), y 5 veces
# más que un "no" de verdad (0,20-0,40 s medidos). Eso es una carrera perdida por
# milésimas, no una cancha tomada. Se tiraba UN solo tiro y no se volvía a intentar
# nunca más: ahora se insiste.
TICK_RONDAS = 3           # tiros a la cancha pedida (el 1ro solo, los demás con las otras)
TICK_ESPERA = 0.15        # respiro entre rondas
TICK_LIMITE_SEG = 6.0     # tope total del tick; pasado eso actúa el flujo normal completo


# ----------------------------------------------------------------------
#  Reloj real (NTP) y calculo de la hora de apertura
# ----------------------------------------------------------------------
# El offset contra NTP se calcula UNA vez y se cachea; hora_real() usa el
# reloj local + ese offset (sin pegarle a la red en cada llamada). Así el
# spin-loop del disparo tiene precisión de reloj local, no jitter de red.
_ntp_offset = 0.0
_ntp_ts = 0.0            # cuándo se calculó el offset (monotonic)
_ntp_lock = threading.Lock()
NTP_REFRESCO_SEG = 300   # revalidar el offset como mucho cada 5 min


def sincronizar_reloj(ntp_servidor="pool.ntp.org", forzar=False):
    """Consulta NTP y cachea el offset. Devuelve el offset (0.0 si no se pudo)."""
    global _ntp_offset, _ntp_ts
    with _ntp_lock:
        if not forzar and _ntp_ts and (time.monotonic() - _ntp_ts) < NTP_REFRESCO_SEG:
            return _ntp_offset
        if ntplib:
            try:
                r = ntplib.NTPClient().request(ntp_servidor, version=3, timeout=3)
                _ntp_offset = r.offset
                _ntp_ts = time.monotonic()
            except Exception as e:
                print(f"[reloj] no pude sincronizar NTP ({e}); uso reloj local", flush=True)
        return _ntp_offset


def hora_real(ntp_servidor="pool.ntp.org"):
    """Timestamp UNIX corregido con el offset NTP cacheado (sin request en cada llamada)."""
    if _ntp_ts and (time.monotonic() - _ntp_ts) < NTP_REFRESCO_SEG:
        return time.time() + _ntp_offset
    return time.time() + sincronizar_reloj(ntp_servidor)


def ahora_ar(tz_offset=-3):
    """Fecha y hora ACTUAL en Argentina (naive). El server corre en UTC: usar
    datetime.now() directo da el día equivocado entre las 21 y las 24 hs ARG."""
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=tz_offset)).replace(tzinfo=None)


def hoy_ar(tz_offset=-3):
    """Fecha de HOY en Argentina (no la del server, que puede estar en UTC)."""
    return ahora_ar(tz_offset).date()


def _epoch_ar(dt, tz_offset=-3):
    """datetime local (Argentina) -> epoch UNIX."""
    return (dt - datetime.timedelta(hours=tz_offset)).replace(
        tzinfo=datetime.timezone.utc).timestamp()


def midnight_epoch(fecha, tz_offset=-3):
    """Epoch UNIX de las 00:00 (hora Argentina) de 'fecha' (YYYY-MM-DD)."""
    d = datetime.date.fromisoformat(fecha)
    return int(_epoch_ar(datetime.datetime(d.year, d.month, d.day), tz_offset))


def _ids_jugadores(jugadores):
    """Normaliza la lista de jugadores a player_ids. Acepta [{id, nombre}] o [player_id]."""
    return [(j.get("id") if isinstance(j, dict) else j) for j in jugadores]


def _texto_ondepor(info):
    """Saca el mensaje legible de una respuesta de OnDepor. Viene en 'message' con
    <br> y a veces solo dentro de 'error', que es el HTML del popup entero."""
    for campo in ("message", "error"):
        crudo = re.sub(r"<[^>]+>", " ", str((info or {}).get(campo, "") or ""))
        crudo = re.sub(r"\s+", " ", crudo).strip()
        if crudo:
            return crudo
    return ""


def _es_ocupada(r):
    """¿El rechazo es "esa cancha no está libre"? ÚNICA definición del criterio (antes
    estaba duplicada en reservar() y en confirmar_objetivo()). Los demás errores —cupo,
    jugador repetido, "refresca tu pantalla"— NO son de cancha: cortan el tick para que
    actúe el flujo normal completo, porque insistir con otra cancha no los arregla."""
    e = json.dumps(r.get("errors", r), ensure_ascii=False).lower()
    return ("no disponible" in e) or ("ocupada" in e) or ("no est" in e and "disponible" in e)


def calcular_apertura(fecha, hora_desde, horas_antes=24, minutos_extra=1, tz_offset=-3):
    """Hora en que se habilita el turno. Ej: 15/07 16:00 -> 14/07 16:01."""
    turno = datetime.datetime.fromisoformat(f"{fecha}T{hora_desde}:00")
    dt = turno - datetime.timedelta(hours=horas_antes) + datetime.timedelta(minutes=minutos_extra)
    return dt, _epoch_ar(dt, tz_offset)


# ----------------------------------------------------------------------
#  Cliente OnDepor (una instancia = una cuenta de reservador)
# ----------------------------------------------------------------------
class OnDepor:
    def __init__(self, usuario, clave, club_id, tz_offset=-3):
        self.usuario = usuario
        self.clave = clave
        self.club_id = club_id
        self.tz = tz_offset
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "*/*"})
        self.csrf = None
        self.svid = None
        self._ultimo_form = {}   # última respuesta de my-reservation-new (para saber por qué no hay jid)

    # -- LOGIN + lectura de tokens (svid + csrf) --
    def login(self):
        payload = {
            "LoginForm[email]": self.usuario,
            "LoginForm[password]": self.clave,
            "LoginForm[rememberMe]": "1",
        }
        data, ultima = None, "sin respuesta del servidor"
        for host in (BASE, "https://www.ondepor.com"):
            try:
                r = self.s.post(host + "/app/api/login", data=payload,
                                headers={"Content-Type": "application/x-www-form-urlencoded",
                                         "Origin": host, "Referer": host + "/app/login",
                                         "X-Requested-With": "XMLHttpRequest"}, timeout=20)
            except Exception as e:
                ultima = f"no pude conectar a {host} ({e})"
                continue
            try:
                d = r.json()
            except Exception:
                d = {}
            if d.get("status") == "success" or d.get("location"):
                data = d
                break
            if d.get("status") == "failure":
                ultima = json.dumps(d.get("errors", d.get("message", d)), ensure_ascii=False)
        if not data:
            raise RuntimeError("Login fallido: " + str(ultima))

        # asentar sesion y leer tokens
        self.s.get(BASE + "/player", timeout=20)
        self._leer_tokens()
        if not self.csrf:
            raise RuntimeError("Login OK pero no pude leer el token de la pagina (revisá conexión).")
        return True

    def _leer_tokens(self):
        html = self.s.get(BASE + "/user/_favorites", timeout=20).text
        m = re.search(r'csrf-token"\s*content="([^"]+)"', html)
        self.csrf = m.group(1) if m else self.csrf
        sv = re.search(r'id="site_version_id"\s+value="([^"]*)"', html) or \
             re.search(r'value="([^"]*)"\s+id="site_version_id"', html)
        self.svid = (sv.group(1) if sv and sv.group(1) else None) or self.svid or SVID_FALLBACK
        return self.csrf

    def _hdr(self):
        return {"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": self.csrf or "",
                "Referer": BASE + "/user/_favorites", "Origin": BASE,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "*/*"}

    def _q(self, v):
        return requests.utils.quote(str(v))

    # -- PASO 2: abrir el formulario del turno -> jid --
    def abrir_formulario(self, fecha, hora_desde):
        slot_id = f"time-{hora_desde}-club-{self.club_id}-{midnight_epoch(fecha, self.tz)}"
        body = (f"id={self._q(slot_id)}&date_from={fecha}&&svid={self._q(self.svid)}"
                f"&_csrf-frontend-ondepor={self._q(self.csrf)}")
        r = self.s.post(BASE + "/player/reservation/my-reservation-new",
                        data=body, headers=self._hdr(), timeout=20, allow_redirects=False)
        try:
            data = r.json()
        except Exception:
            data = {}
        self._ultimo_form = data   # guardo la respuesta cruda para que reservar() sepa si el turno "no abrió"
        return (data.get("model") or {}).get("jid")

    # -- PASO 3: canchas disponibles para ese turno -> [{court_id, nombre}] --
    def canchas_disponibles(self, fecha, hora_desde, hora_hasta, jid, single_double=2):
        data = {"club_id": self.club_id, "date": fecha, "time_from": hora_desde,
                "time_to": hora_hasta, "single_double": single_double, "jid": jid,
                "svid": self.svid, "_csrf-frontend-ondepor": self.csrf}
        r = self.s.post(BASE + "/player/reservation/courtsavailable",
                        data=data, headers=self._hdr(), timeout=20, allow_redirects=False)
        try:
            j = r.json()
        except Exception:
            return []
        if j.get("status") != "success":
            return []
        out = []
        for op in j.get("options", []) or []:
            if op.get("key") is not None:
                out.append({"court_id": op["key"], "nombre": str(op.get("value", "")).strip()})
        return out

    @staticmethod
    def _cancha_coincide(c, preferida):
        """¿Esta cancha es la que se pidió? Acepta número ('5'), nombre completo
        ('05-KINERET'), zona ('KINERET') o court_id. ÚNICA definición del criterio:
        la usan elegir_cancha, ordenar_canchas y la pre-carga (antes estaba triplicado).

        ⚠ Si se pide un NÚMERO se compara SOLO por número. El "p in nom" de antes hacía
        que pedir la 1 matcheara con "10-CHACRA" y "11-CHACRA" (bug real del 07/08: se
        pidió la cancha 1 y reservó la 10, y encima la pre-carga creía que la 1 estaba
        en la lista y no la agregaba)."""
        p = str(preferida or "").strip().lower()
        if p == "":
            return True
        nom = str(c.get("nombre", "")).lower()
        if p == str(c.get("court_id")):
            return True
        if p.isdigit():
            return nom.split("-")[0].lstrip("0") == p.lstrip("0")
        return p == nom or p in nom or nom.startswith(p + "-")

    @staticmethod
    def elegir_cancha(canchas, preferida=""):
        """Elige la cancha: si 'preferida' está vacía -> la primera; si no, la que
        coincida por número o por nombre (ej '5', 'KINERET', '05-KINERET')."""
        if not canchas:
            return None
        return OnDepor.ordenar_canchas(canchas, preferida)[0]

    @staticmethod
    def ordenar_canchas(canchas, preferida=""):
        """Lista de canchas para PROBAR EN ORDEN: primero la(s) preferida(s) (o las de
        la zona), después todas las demás disponibles. Así, si la elegida está ocupada,
        se salta a la siguiente hasta agotarlas."""
        if not canchas:
            return []
        if str(preferida or "").strip() == "":
            return list(canchas)   # "cualquiera": en el orden que las devuelve OnDepor
        pref = [c for c in canchas if OnDepor._cancha_coincide(c, preferida)]
        pref_ids = {c["court_id"] for c in pref}
        resto = [c for c in canchas if c["court_id"] not in pref_ids]
        return pref + resto

    def _clonar_sesion(self):
        """Copia de la sesión (mismas cookies + headers) para pedidos en paralelo
        sin que compartan estado y se pisen entre hilos. update() copia cada cookie
        conservando sus flags de dominio (recrearlas a mano rompe la sesión)."""
        ns = requests.Session()
        ns.headers.update(self.s.headers)
        ns.cookies.update(self.s.cookies)
        return ns

    # -- PASO 4: CONFIRMAR la reserva --
    def confirmar(self, fecha, hora_desde, hora_hasta, court_id, jid, player_ids,
                  single_double=2, telephone="", sess=None):
        s = sess or self.s
        campos = {
            "ReservationForm[single_double]": single_double,
            "ReservationForm[time_from]": hora_desde,
            "ReservationForm[time_to]": hora_hasta,
            "ReservationForm[court_id]": court_id,
            "ReservationForm[telephone]": telephone,
            "players": "",
            "ReservationForm[remember]": 0,
            "ReservationForm[terms_and_cond]": 1,
            "ReservationForm[club_id]": self.club_id,
            "ReservationForm[date]": fecha,
            "ReservationForm[jid]": jid,
            "invitations": 0,
            "svid": self.svid,
            "_csrf-frontend-ondepor": self.csrf,
        }
        pares = [(k, str(v)) for k, v in campos.items()]
        for pid in player_ids:
            pares.append(("player_id[]", str(pid)))
        r = s.post(BASE + "/player/reservation/add-reservation-new",
                   data=pares, headers=self._hdr(), timeout=20, allow_redirects=False)
        try:
            return r.json()
        except Exception:
            return {"status": "error_http", "code": r.status_code, "text": r.text[:300]}

    def preparar_jugadores(self, jugadores):
        """Registra (search-players) a los jugadores en la sesión. OnDepor exige haberlos
        'buscado' o rechaza el cupo con 'refresca tu pantalla'. NO necesita el turno abierto
        ni el jid → conviene hacerlo ANTES de la apertura, para que al abrir el turno solo
        queden los pasos imprescindibles (abrir formulario → canchas → confirmar)."""
        for j in jugadores:
            if isinstance(j, dict):
                pid, nombre = j.get("id"), str(j.get("nombre") or j.get("id"))
            else:
                pid, nombre = j, str(j)
            consulta = nombre.split()[-1] if nombre and not nombre.isdigit() else str(pid)
            try:
                self.buscar_jugadores_lista(consulta)
            except Exception:
                pass

    # -- Todo junto: abrir form -> canchas -> registrar jugadores -> confirmar --
    def reservar(self, fecha, hora_desde, hora_hasta, jugadores,
                 cancha_preferida="", single_double=2, telephone="",
                 precalentado=False, pausa=None):
        """jugadores: lista de {"id":.., "nombre":..} o de player_id sueltos.
        IMPORTANTE: hay que 'buscar' cada jugador (search-players) antes de confirmar,
        si no OnDepor rechaza el cupo completo con 'refresca tu pantalla'.
        precalentado=True: los jugadores YA se registraron antes (preparar_jugadores) →
          se saltea ese paso y su pausa (camino crítico mínimo para la apertura).
        pausa: respiro entre pasos; None = PAUSA_PASO (seguro, para el modo inmediato)."""
        p = PAUSA_PASO if pausa is None else pausa
        jid = self.abrir_formulario(fecha, hora_desde)
        if not jid:
            info = getattr(self, "_ultimo_form", {}) or {}
            msg = str(info.get("message", "") or info.get("error", "")).lower()
            if "anticipa" in msg or "no permite reservar" in msg:
                # el turno TODAVÍA no abrió (ej: disparamos justo antes de las :01)
                return {"status": "no_abierto", "errors": info.get("message", "el turno todavía no abrió")}
            # OnDepor SÍ dice por qué no abre el formulario ("Tenés el máximo de reservas
            # activas permitidas por el club", carnet ocupado, etc). Antes se tiraba esa
            # respuesta y se escribía un "¿svid/sesión?" fijo: el 08/08/2026 eso mandó a
            # buscar un problema de sesión cuando era un carnet ocupado. Va el texto real.
            return {"status": "failure",
                    "errors": _texto_ondepor(info) or "no pude abrir el formulario del turno (¿svid/sesión?)"}
        if p:
            time.sleep(p)   # respiro para el servidor (evita el "refresca tu pantalla")
        canchas = self.canchas_disponibles(fecha, hora_desde, hora_hasta, jid, single_double)
        if not canchas:
            return {"status": "sin_cancha", "errors": "no hay canchas disponibles todavía"}
        canchas_orden = self.ordenar_canchas(canchas, cancha_preferida)

        if not precalentado:
            # modo inmediato (no se pre-calentó): registrar los jugadores acá, en el flujo
            if p:
                time.sleep(p)
            self.preparar_jugadores(jugadores)
        if p:
            time.sleep(p)

        pids = _ids_jugadores(jugadores)
        es_unica = str(cancha_preferida or "").strip().isdigit()   # pidió UNA cancha puntual
        reg = []                          # cada intento con su tiempo y su error (para el log)

        # 1) probar la cancha preferida (o la primera del orden)
        resp = self._confirmar_reg(reg, canchas_orden[0], fecha, hora_desde, hora_hasta,
                                   jid, pids, single_double, telephone)
        resp["_canchas_ofrecidas"] = canchas
        resp["_intentos"] = reg
        if resp.get("status") == "success":
            return resp
        if not _es_ocupada(resp):
            return resp                    # error que NO es de cancha (cupo, "refresca", jugador): cortar
        resto = canchas_orden[1:]
        if not resto:
            return resp

        # 2a) cancha ÚNICA ocupada -> disparar TODAS las demás EN PARALELO (a la vez)
        if es_unica:
            r2 = self._confirmar_paralelo(resto, fecha, hora_desde, hora_hasta, jid,
                                          pids, single_double, telephone, canchas, reg)
            r2["_intentos"] = reg
            return r2

        # 2b) zona / cualquiera -> secuencial (sin riesgo de doble reserva)
        ultimo = resp
        for cancha in resto:
            r2 = self._confirmar_reg(reg, cancha, fecha, hora_desde, hora_hasta, jid,
                                     pids, single_double, telephone)
            r2["_canchas_ofrecidas"] = canchas
            r2["_intentos"] = reg
            if r2.get("status") == "success":
                return r2
            if _es_ocupada(r2):
                ultimo = r2
                continue
            return r2
        return ultimo

    def _confirmar_reg(self, registro, cancha, fecha, hora_desde, hora_hasta, jid,
                       pids, single_double, telephone, sess=None):
        """confirmar() + CRONÓMETRO. Cada intento queda anotado en 'registro' con cuánto
        tardó y qué contestó OnDepor. Sin esto no se puede distinguir un "esa cancha está
        tomada" (contesta en 0,2-0,4 s) de una carrera perdida (tarda ~1,3 s, lo mismo que
        una reserva que entra) — que fue justo lo que pasó el 07/08/2026 y hubo que
        deducirlo a mano de los tiempos del log."""
        t0 = time.perf_counter()
        try:
            r = self.confirmar(fecha, hora_desde, hora_hasta, cancha["court_id"], jid,
                               pids, single_double, telephone, sess=sess)
        except Exception as e:
            r = {"status": "error", "errors": str(e)}
        r["_cancha"] = cancha
        r["_ms"] = int((time.perf_counter() - t0) * 1000)
        if registro is not None:
            registro.append({                       # list.append es atómico: sirve desde hilos
                "cancha": str(cancha.get("nombre", "")),
                "status": str(r.get("status", "")),
                "ms": r["_ms"],
                "error": json.dumps(r.get("errors", ""), ensure_ascii=False)[:200],
            })
        return r

    def _confirmar_paralelo(self, canchas, fecha, hora_desde, hora_hasta, jid,
                            pids, single_double, telephone, canchas_ofrecidas,
                            registro=None):
        """Dispara la confirmación en TODAS las canchas al mismo tiempo (cada hilo con su
        copia de sesión). Devuelve la primera exitosa EN EL ORDEN DE ENTRADA — por eso la
        cancha pedida va primera: si entra junto con otra, la que queda es la pedida. Si
        entró más de una lo marca en '_multiple' para poder avisar (hay que cancelar las de
        sobra a mano). Si no entró ninguna devuelve el primer error que NO sea de cancha
        (cupo, jugador, "refresca"): es el informativo, y corta el tick."""
        def intentar(c):
            return self._confirmar_reg(registro, c, fecha, hora_desde, hora_hasta, jid,
                                       pids, single_double, telephone,
                                       sess=self._clonar_sesion())
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(canchas), 12)) as ex:
            resultados = list(ex.map(intentar, canchas))
        exitos = [r for r in resultados if r.get("status") == "success"]
        if exitos:
            ganador = dict(exitos[0])
            ganador["_canchas_ofrecidas"] = canchas_ofrecidas
            if len(exitos) > 1:
                ganador["_multiple"] = [str((e.get("_cancha") or {}).get("nombre", "")) for e in exitos]
            return ganador
        otros = [r for r in resultados if not _es_ocupada(r)]
        peor = (otros or resultados or
                [{"status": "sin_cancha", "errors": "ninguna cancha disponible"}])[0]
        peor["_canchas_ofrecidas"] = canchas_ofrecidas
        return peor

    # ------------------------------------------------------------------
    #  PRE-CARGA (enfoque Diego): dejar TODO listo antes de la apertura
    #  usando un turno FUENTE ya abierto, para que al abrir el objetivo
    #  solo quede el confirm (~0.5s). El court_id sale del courtsavailable
    #  del fuente (lo "activa" en el form); el jid del fuente sirve para el
    #  objetivo; los jugadores se registran contra ese jid y sobreviven.
    # ------------------------------------------------------------------
    def precargar_objetivo(self, jugadores, cancha_preferida="", fuentes=None,
                           single_double=2, pausa=None, canchas_club=None):
        """PRE-WARM: prueba turnos FUENTE (lista de (fecha, hora, fin), ya abiertos) hasta
        encontrar uno que abra; saca de ahí las canchas y registra los jugadores. Devuelve
        {jid, canchas, fuente} o None si no pudo pre-cargar.
        VERIFICADO (Diego, 25/07): el fuente NO necesita tener libre la cancha del objetivo —
        se confirmó el objetivo en una cancha que estaba OCUPADA en el fuente. La disponibilidad
        se valida contra el OBJETIVO al confirmar. Por eso sirve cualquier turno abierto, y la
        cancha pedida se agrega igual aunque el fuente no la liste ('canchas_club' = mapa
        {num: court_id} del club para poder armarla).
        NO confirma nada: el confirm va en confirmar_objetivo() al abrir el objetivo."""
        p = PAUSA_PASO if pausa is None else pausa
        for f_fecha, f_hora, f_fin in (fuentes or []):
            try:
                jid = self.abrir_formulario(f_fecha, f_hora)
            except Exception:
                continue
            if not jid:
                continue
            if p:
                time.sleep(p)
            try:
                canchas = self.canchas_disponibles(f_fecha, f_hora, f_fin, jid, single_double)
            except Exception:
                canchas = []
            if not canchas:
                continue      # fuente sin ninguna cancha: probar el siguiente
            orden = self.ordenar_canchas(canchas, cancha_preferida)
            # si pidió una cancha puntual y el fuente NO la lista (ocupada ahí), agregarla
            # igual ADELANTE: en el objetivo puede estar libre (verificado).
            pref = str(cancha_preferida or "").strip()
            if pref and not any(self._cancha_coincide(c, pref) for c in orden):
                cid = (canchas_club or {}).get(pref)
                if cid:
                    orden = [{"court_id": cid, "nombre": f"{pref} (pedida)"}] + orden
            # PLAN B COMPLETO: atrás van TODAS las demás canchas del club. El fuente solo
            # aporta las que a ÉL le sobran, y el 07/08/2026 eso dejó el tick sin salida:
            # el fuente tenía 2 libres, las 2 ocupadas en el objetivo, y el flujo lento
            # encontró 5 canchas libres 5 segundos más tarde. Que el fuente no liste una
            # cancha no impide confirmarla en el objetivo (verificado 25/07).
            ids = {str(c["court_id"]) for c in orden}
            for num, cid in sorted((canchas_club or {}).items(),
                                   key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 99):
                if str(cid) not in ids:
                    orden.append({"court_id": cid, "nombre": str(num)})
                    ids.add(str(cid))
            if p:
                time.sleep(p)
            try:
                self.preparar_jugadores(jugadores)
            except Exception:
                pass
            return {"jid": jid, "canchas": orden, "fuente": (f_fecha, f_hora)}
        return None

    def confirmar_objetivo(self, listo, fecha, hora_desde, hora_hasta, jugadores,
                           single_double=2, telephone="", cancha_preferida="",
                           rondas=TICK_RONDAS, limite_seg=TICK_LIMITE_SEG):
        """TICK: confirma el objetivo con el estado pre-cargado (jid + canchas), SIN abrir
        formulario ni courtsavailable.

        Cancha PUNTUAL (ej "10"):
          · rondas 1..N-1 -> SOLO la pedida, una y otra vez. Insistir es el punto de todo
            esto: la pedida se pierde por milésimas y al segundo tiro suele estar. Sola,
            porque así no hay manera de quedarse con dos canchas.
          · ronda N -> la pedida (primera, para que gane si entran dos) junto con TODAS
            las demás en paralelo: si la pedida se fue de verdad, que al menos salga una.
        Zona / cualquiera: secuencial en el orden dado (no se insiste: cualquier cancha
        sirve, y disparar en paralelo arriesga quedarse con dos).

        Corta al toque —sin gastar rondas— si el error NO es de cancha (cupo, jugador
        repetido, "refresca tu pantalla"): eso lo resuelve el flujo normal, no otro tiro.
        Devuelve la respuesta con '_intentos': cada tiro con su cancha, su tiempo y su error."""
        jid = listo["jid"]
        pids = _ids_jugadores(jugadores)
        canchas = list(listo.get("canchas", []))
        reg, t0 = [], time.perf_counter()

        def _fin(r):
            r["_canchas_ofrecidas"] = canchas
            r["_intentos"] = reg
            return r

        if not canchas:
            return _fin({"status": "sin_cancha", "errors": "la pre-carga no dejó ninguna cancha"})

        def _tirar(c):
            return self._confirmar_reg(reg, c, fecha, hora_desde, hora_hasta, jid, pids,
                                       single_double, telephone)

        r = _tirar(canchas[0])
        if r.get("status") == "success" or not _es_ocupada(r):
            return _fin(r)
        ultimo = r
        if len(canchas) == 1:
            return _fin(ultimo)

        if not str(cancha_preferida or "").strip().isdigit():
            for c in canchas[1:]:                      # zona / cualquiera -> de a una
                if time.perf_counter() - t0 > limite_seg:
                    break
                r = _tirar(c)
                if r.get("status") == "success" or not _es_ocupada(r):
                    return _fin(r)
                ultimo = r
            return _fin(ultimo)

        for ronda in range(2, rondas + 1):             # cancha puntual -> insistir
            if time.perf_counter() - t0 > limite_seg:
                break
            time.sleep(TICK_ESPERA)                    # que la otra punta termine de escribir
            if ronda < rondas:
                r = _tirar(canchas[0])                 # de nuevo la pedida, ella sola
            else:
                r = self._confirmar_paralelo(canchas, fecha, hora_desde, hora_hasta, jid,
                                             pids, single_double, telephone, canchas, reg)
            if r.get("status") == "success" or not _es_ocupada(r):
                return _fin(r)
            ultimo = r
        return _fin(ultimo)

    # -- Listar MIS reservas activas via /player/reservation/list (JSON confiable) --
    def mis_reservas(self, dias=30):
        """Devuelve [{id, club_id, fecha, hora_to, cancha_nombre, cancha_num, players}]
        de tus turnos reservados en los próximos 'dias'. El 'id' es el reservation_id
        que necesita cancelar_reserva()."""
        # fecha ARG (NO date.today(): el server corre en UTC y de 21 a 24 hs ARG ya está
        # en el día siguiente -> se perdían las reservas de HOY). -1 día de margen extra.
        hoy = hoy_ar(self.tz)
        params = {"date_from": (hoy - datetime.timedelta(days=1)).isoformat(),
                  "date_to": (hoy + datetime.timedelta(days=dias)).isoformat(),
                  "svid": self.svid}
        try:
            r = self.s.get(BASE + "/player/reservation/list", params=params,
                           headers=self._hdr(), timeout=20)
            data = r.json().get("data", {}).get("reservations", [])
        except Exception:
            return []
        out = []
        for rr in data or []:
            court = rr.get("court") or {}
            club = rr.get("club") or {}
            cn = str(court.get("name", "") or "")
            out.append({
                "id": rr.get("id"),
                "club_id": club.get("id") or rr.get("club_reservation_id"),
                "fecha": rr.get("date"),
                "hora_to": court.get("time_to"),
                "cancha_nombre": cn,
                "cancha_num": (cn.split("-")[0].lstrip("0") if "-" in cn else ""),
                "players": [p.get("player_id") for p in (rr.get("players") or []) if p.get("player_id")],
            })
        out.sort(key=lambda x: (str(x.get("fecha")), str(x.get("hora_to"))))
        return out

    # -- Cancelar una reserva por su id (confirmado con captura real; sin captcha real) --
    def cancelar_reserva(self, reservation_id):
        data = {
            "CancelReservationForm[id]": reservation_id,
            "CancelReservationForm[delete_reservation_all]": "false",
            "CancelReservationForm[delete_reservation_group_all]": "false",
            "from_next_matches": "0",
            "g-recaptcha-response": "undefined",   # va literal; OnDepor NO valida captcha (verificado)
            "svid": self.svid,
        }
        r = self.s.post(BASE + "/player/reservation/cancel-reservation",
                        data=data, headers=self._hdr(), timeout=20, allow_redirects=False)
        try:
            return r.json()
        except Exception:
            return {"status": "error_http", "code": r.status_code, "text": r.text[:200]}

    # -- CAMBIAR JUGADORES de una reserva confirmada (cancelar + volver a tomar) --
    def cambiar_jugadores(self, fecha, club_id, hora_desde, hora_hasta,
                          nuevos_players, viejos_players, single_double=2, telephone="",
                          api_nuevo=None, telephone_nuevo="", cancha_nueva=""):
        """Cambia los jugadores (y opcionalmente el RESERVADOR) de una reserva YA confirmada.
        'self' es el reservador ORIGINAL (dueño, el que puede cancelar). Si 'api_nuevo' se
        pasa, la reserva nueva la toma ESE reservador (login distinto); si no, la re-toma self.
        Flujo: ubica la reserva -> CANCELA (con self) -> RE-TOMA con los nuevos (con
        api_nuevo o self) en la misma cancha, o en 'cancha_nueva' si se pide mudarla. Red de seguridad: si no entra, self recupera la
        reserva ORIGINAL (reservador y jugadores viejos).
        Devuelve {status, mensaje}. status: success / revertida / perdida / no_encontrada / fallo_cancelar."""
        api_rebook = api_nuevo or self          # quién TOMA la reserva nueva
        tel_rebook = telephone_nuevo if api_nuevo else telephone

        # 1) ubicar la reserva -> reservation_id + cancha original (con la cuenta original)
        match = None
        for x in self.mis_reservas(dias=30):
            if str(x.get("fecha")) == str(fecha) and str(x.get("club_id")) == str(club_id):
                match = x
                break
        if not match:
            return {"status": "no_encontrada",
                    "mensaje": "No encontré la reserva confirmada en OnDepor (¿ya no existe?). No cancelé nada."}
        rid = match["id"]
        cancha_pref = match.get("cancha_num") or ""   # la cancha que tiene HOY
        # 'cancha_nueva' permite mudar la reserva a OTRA cancha (ej: entró en la 10 y se
        # quería la 1). La red de seguridad sigue recuperando la cancha ORIGINAL.
        cancha_rebook = str(cancha_nueva or "").strip() or cancha_pref

        # 2) precalentar: registrar los jugadores nuevos en la sesión que va a RE-TOMAR
        for pid in nuevos_players:
            try:
                api_rebook.buscar_jugadores_lista(str(pid))
            except Exception:
                pass

        # 3) CANCELAR (siempre con self = reservador original, que es el dueño)
        c = self.cancelar_reserva(rid)
        if str(c.get("status")) != "success":
            return {"status": "fallo_cancelar",
                    "mensaje": "No se pudo cancelar en OnDepor; la reserva quedó como estaba."}

        # 4) RE-TOMAR la misma cancha con el reservador/jugadores NUEVOS
        for _ in range(4):
            rb = api_rebook.reservar(fecha, hora_desde, hora_hasta, nuevos_players,
                                     cancha_preferida=cancha_rebook, single_double=single_double,
                                     telephone=tel_rebook)
            if rb.get("status") == "success":
                return {"status": "success", "cancha": (rb.get("_cancha") or {}).get("nombre", ""),
                        "mensaje": "Reserva actualizada; cancha re-tomada."}
            time.sleep(0.3)

        # 5) RED DE SEGURIDAD: recuperar la reserva ORIGINAL (self + jugadores viejos)
        for _ in range(5):
            rb2 = self.reservar(fecha, hora_desde, hora_hasta, viejos_players,
                                cancha_preferida=cancha_pref, single_double=single_double,
                                telephone=telephone)
            if rb2.get("status") == "success":
                return {"status": "revertida",
                        "mensaje": ("No pude re-tomar con los datos nuevos, pero RECUPERÉ la "
                                    "reserva original (reservador y jugadores de antes). No cambió nada.")}
            time.sleep(0.3)

        return {"status": "perdida",
                "mensaje": ("CANCELÉ la reserva pero NO pude volver a tomarla (ni con los nuevos ni "
                            "con los originales). Probablemente tomaron la cancha. Revisá OnDepor.")}

    # -- Buscar jugador por nombre -> [{id, nombre}] (para armar db_jugadores) --
    def buscar_jugadores_lista(self, q):
        params = {"club_id": self.club_id, "q": q, "svid": self.svid}
        r = self.s.get(BASE + "/player/reservation/search-players/",
                       params=params,
                       headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": self.csrf or "",
                                "Referer": BASE + "/user/_favorites", "Accept": "*/*"},
                       timeout=20)
        try:
            data = r.json()
        except Exception:
            return []
        lista = data if isinstance(data, list) else data.get("data", data.get("results", []))
        out = []
        for it in (lista or []):
            if not isinstance(it, dict):
                continue
            nombre = f"{it.get('firstname','')} {it.get('lastname','')}".strip() \
                     or str(it.get("label") or it.get("value") or "")
            if it.get("id"):
                out.append({"id": it["id"], "nombre": nombre})
        return out
