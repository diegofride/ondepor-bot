# -*- coding: utf-8 -*-
"""
==========================================================================
  reservar_bot.py  -  ORQUESTADOR de reservas OnDepor
==========================================================================
Lee tres archivos:
    db_jugadores.json     -> jugadores (nombre + player_id)
    db_reservadores.json  -> los que reservan (usuario + clave = jugador 1)
    reservas.json         -> reservas precargadas (que turno, quien, con quienes)

Cada reserva se dispara SOLA a la hora exacta de apertura (turno - 24h + 1 min),
y si hay varias corren EN PARALELO.

COMANDOS (abrir una terminal en esta carpeta):
    python reservar_bot.py listar
        Muestra las reservas cargadas y a que hora se dispararia cada una.

    python reservar_bot.py buscar-jugador "apellido"
        Busca en OnDepor y muestra nombre + player_id (solo muestra).

    python reservar_bot.py agregar-jugador "apellido"
        Busca, te deja elegir cual(es) y los GUARDA solo en db_jugadores.json.
        (usa el primer reservador de la base para loguearse)

    python reservar_bot.py probar <id | all>
        Ensayo: loguea y ubica el turno, pero NO reserva.

    python reservar_bot.py ya <id | all>
        Reserva YA MISMO (si el turno ya esta abierto). Varias -> en paralelo.

    python reservar_bot.py correr <id | all>
        Espera la hora de apertura y reserva al segundo. Varias -> en paralelo.

NOTA: la app corre online (webapp.py) con su propio reloj interno; este CLI es
para pruebas/uso manual. La programación 24/7 la maneja el servidor, no Windows.

Requisitos:  pip install requests ntplib
==========================================================================
"""
import os
import sys
import json
import time
import threading
import datetime

try:  # que la consola de Windows no rompa con acentos/simbolos
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from ondepor_core import (OnDepor, hora_real, calcular_apertura, sincronizar_reloj,
                          hoy_ar, ahora_ar, PAUSA_PASO, PAUSA_RAPIDA)

AQUI = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(AQUI, "logs")


# ----------------------------------------------------------------------
#  Carga/guardado de las bases (con lock + escritura atómica)
# ----------------------------------------------------------------------
_io_lock = threading.RLock()   # serializa todo el acceso a los JSON dentro del proceso


def leer_json(nombre_archivo, default=None):
    """Lee un JSON completo (dict). Devuelve 'default' si no existe/está roto."""
    ruta = os.path.join(AQUI, nombre_archivo)
    with _io_lock:
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {} if default is None else default


def escribir_json(nombre_archivo, data):
    """Escritura ATÓMICA (tmp + os.replace) bajo lock: nunca deja el archivo a medias."""
    ruta = os.path.join(AQUI, nombre_archivo)
    tmp = ruta + ".tmp"
    with _io_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ruta)   # rename atómico a nivel de sistema operativo


def _cargar(nombre_archivo, clave_lista):
    return leer_json(nombre_archivo, {}).get(clave_lista, [])


def cargar_todo():
    jugadores = _cargar("db_jugadores.json", "jugadores")
    reservadores = _cargar("db_reservadores.json", "reservadores")
    reservas = _cargar("reservas.json", "reservas")
    return jugadores, reservadores, reservas


def _norm(s):
    return str(s).strip().lower()


def resolver_player_id(ref, jugadores, reservadores):
    """Convierte un nombre (o numero) a player_id, buscando en ambas bases."""
    if isinstance(ref, int):
        return ref
    if isinstance(ref, str) and ref.strip().isdigit():
        return int(ref.strip())
    objetivo = _norm(ref)
    for j in jugadores:
        if _norm(j.get("nombre")) == objetivo:
            return j.get("player_id")
    for r in reservadores:
        if _norm(r.get("nombre")) == objetivo:
            return r.get("player_id")
    return None


def armar_trabajo(reserva, jugadores, reservadores):
    """Convierte una reserva del JSON en un 'trabajo' concreto y listo para disparar.
    NUNCA lanza excepción: una reserva mal formada se devuelve con 'problema' seteado
    (para no tumbar el resto de la tanda ni el reloj interno)."""
    rid = reserva.get("id", "sin-id")
    problema = None

    # reservador (jugador 1, el que loguea)
    nombre_res = reserva.get("reservador")
    res = next((r for r in reservadores if _norm(r.get("nombre")) == _norm(nombre_res)), None)
    if not res:
        problema = f"no encontré al reservador '{nombre_res}' en db_reservadores.json"
        res = {}
    elif str(res.get("clave", "")).startswith("PONER") or not res.get("clave"):
        problema = f"falta cargar la clave del reservador '{nombre_res}' en db_reservadores.json"

    # los otros jugadores -> {id, nombre}  (el nombre se usa para registrarlos con search-players)
    ids_otros, jug_full, faltan = [], [], []
    for ref in reserva.get("jugadores", []):
        pid = resolver_player_id(ref, jugadores, reservadores)
        if pid:
            nombre = next((j.get("nombre") for j in jugadores if str(j.get("player_id")) == str(pid)),
                          None) or str(ref)
            ids_otros.append(pid)
            jug_full.append({"id": pid, "nombre": nombre})
        else:
            faltan.append(ref)

    # campos obligatorios + hora de apertura: si algo falta o está mal, se marca 'problema'
    fecha = reserva.get("fecha")
    hora_desde = reserva.get("hora_desde")
    hora_hasta = reserva.get("hora_hasta", "")
    dt = epoch = None
    try:
        if not fecha or not hora_desde:
            raise ValueError("faltan fecha u hora")
        if reserva.get("apertura_iso"):
            dt = datetime.datetime.fromisoformat(reserva["apertura_iso"])
            epoch = (dt - datetime.timedelta(hours=-3)).replace(tzinfo=datetime.timezone.utc).timestamp()
        else:
            dt, epoch = calcular_apertura(
                fecha, hora_desde,
                horas_antes=reserva.get("horas_antes", 24),
                minutos_extra=reserva.get("minutos_extra", 1))
    except Exception as e:
        if not problema:   # no pisar un problema ya detectado (reservador)
            problema = f"reserva mal formada ({e}); revisá reservas.json"
        # placeholders para que el resto del código no explote al leer estos campos
        dt = dt or datetime.datetime(2000, 1, 1)
        epoch = epoch if epoch is not None else 0.0

    return {
        "id": rid,
        "club_id": reserva.get("club_id", 1102),
        "cancha": reserva.get("cancha", ""),
        "fecha": fecha or "",
        "hora_desde": hora_desde or "",
        "hora_hasta": hora_hasta,
        "single_double": reserva.get("single_double", 2),
        "usuario": res.get("usuario"),
        "clave": res.get("clave"),
        "telefono": res.get("telefono", ""),
        "reservador": res.get("nombre", nombre_res),
        "problema": problema,
        "player_ids": ids_otros,
        "jugadores_full": jug_full,
        "faltan_jugadores": faltan,
        "reintentos": reserva.get("reintentos", 40),
        "espera": reserva.get("espera_entre_reintentos_seg", 0.25),
        "apertura_dt": dt,
        "apertura_epoch": epoch,
    }


def buscar_trabajos(cual, datos=None):
    # 'datos' = (jugadores, reservadores, reservas) ya cargados, para no releer los JSON
    jug, res, reservas = datos if datos else cargar_todo()
    if cual and cual != "all":
        reservas = [r for r in reservas if _norm(r.get("id")) == _norm(cual)]
        if not reservas:
            raise RuntimeError(f"No encontre la reserva con id '{cual}' en reservas.json")
    return [armar_trabajo(r, jug, res) for r in reservas]


# ----------------------------------------------------------------------
#  Log (a pantalla y a archivo por reserva)
# ----------------------------------------------------------------------
def log(rid, msg):
    # hora ARGENTINA con milisegundos (el server corre en UTC; los ms sirven para ver
    # el sub-segundo del disparo, que con precisión de 1 s quedaba invisible)
    linea = ahora_ar().strftime("[%H:%M:%S.%f")[:-3] + "] " + f"[{rid}] " + str(msg)
    print(linea, flush=True)
    try:
        os.makedirs(LOGS, exist_ok=True)
        with open(os.path.join(LOGS, f"{rid}.log"), "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------
#  Disparo de UNA reserva
# ----------------------------------------------------------------------
def _login_y_tokens(job):
    api = OnDepor(job["usuario"], job["clave"], job["club_id"])
    api.login()   # login() ya lee svid + csrf
    return api


def _un_intento(api, job, precalentado=False, pausa=None):
    return api.reservar(job["fecha"], job["hora_desde"], job["hora_hasta"],
                        job["jugadores_full"], cancha_preferida=job["cancha"],
                        single_double=job["single_double"], telephone=job["telefono"],
                        precalentado=precalentado, pausa=pausa)


PRECARGA_ANTES_SEG = 40   # cuántos segundos antes de la apertura se hace la pre-carga

# court_id reales por club (capturados de OnDepor 25/07). Sirven para pedir una cancha puntual
# aunque el turno FUENTE la tenga ocupada: verificado que el objetivo igual la acepta.
CANCHAS_POR_CLUB = {
    1102: {"1": 12739, "2": 12740, "3": 12741, "4": 12742,      # QUINTA
           "5": 12596, "6": 12597, "7": 13510, "8": 13511,      # KINERET
           "9": 14425, "10": 14426, "11": 14427},               # CHACRA
    504: {"1": 9620, "2": 9621, "3": 11031, "4": 11032,         # QUINTA
          "5": 13266, "6": 13267, "7": 13508, "8": 13509},      # KINERET
}


def _candidatos_fuente(job):
    """Turnos FUENTE candidatos (mismo club, ya abiertos) para pre-cargar la cancha del
    objetivo (enfoque Diego). Prioriza: mismo día del objetivo en horas ANTERIORES (ya
    abiertas), y después HOY (que está todo abierto y suele tener canchas libres a alguna
    hora). Devuelve lista de (fecha, hora_desde, hora_hasta) en el MISMO club."""
    club = job.get("club_id", 1102)
    horas = [f"{h:02d}:00" for h in (range(8, 18) if club == 1102 else range(18, 24))]

    def fin(h):
        return f"{int(h[:2]) + 1:02d}:00"   # 23:00 -> 24:00 (no 00:00)

    try:
        f_obj = datetime.date.fromisoformat(job["fecha"])
    except Exception:
        return []
    h_obj = job.get("hora_desde", "")
    cands = []
    # 1) mismo día del objetivo, horas anteriores (de la más cercana a la más lejana)
    for h in reversed([x for x in horas if x < h_obj]):
        cands.append((job["fecha"], h, fin(h)))
    # 2) HOY, si el objetivo es a futuro (todas las horas del club ya abrieron hoy).
    #    OJO: hoy_ar(), NO date.today(): el server corre en UTC y de 21 a 24 hs ARG ya
    #    está en el día siguiente -> los candidatos salían con la fecha corrida.
    hoy = hoy_ar()
    if f_obj > hoy:
        for h in reversed(horas):
            cands.append((hoy.isoformat(), h, fin(h)))
    return cands


def disparar(job, esperar=True):
    rid = job["id"]
    if job.get("problema"):
        log(rid, f"NO puedo disparar: {job['problema']}")
        return False
    if job["faltan_jugadores"]:
        log(rid, f"OJO: no resolvi el player_id de: {job['faltan_jugadores']} (revisá los nombres en db_jugadores.json)")
    log(rid, f"Reservador (jugador 1): {job['reservador']}  |  otros jugadores: {job['player_ids']}")
    log(rid, f"Turno: {job['fecha']} {job['hora_desde']}-{job['hora_hasta']}  club={job['club_id']}  cancha={job['cancha'] or 'cualquiera'}")

    try:
        api = _login_y_tokens(job)
        log(rid, "Login OK.")
    except Exception as e:
        log(rid, f"ERROR de login: {e}")
        return False

    epoch = job["apertura_epoch"]
    listo = None   # estado pre-cargado (enfoque Diego): cancha + jugadores listos antes del tick
    if esperar:
        dt = job["apertura_dt"]
        log(rid, f"Esperando la apertura: {dt.strftime('%Y-%m-%d %H:%M:%S')} (hora Argentina)...")
        # espera gruesa hasta 40 s antes: margen amplio para PRE-CARGAR con un turno fuente
        # (probar varios fuentes cuesta ~2-4 s; el estado pre-cargado sobrevive la espera,
        # verificado 45 s) — así nunca se llega tarde a la apertura por la pre-carga.
        while hora_real() < epoch - PRECARGA_ANTES_SEG:
            faltan = epoch - hora_real()
            time.sleep(30 if faltan > 90 else 2 if faltan > 10 else 0.2)
        try:
            api._leer_tokens()  # tokens fresquitos
        except Exception as e:
            log(rid, f"aviso: no pude refrescar tokens antes de disparar ({e}); sigo con los que tengo")
        # PRE-CARGA (enfoque Diego): abrir un turno FUENTE ya abierto, sacar de ahí la cancha
        # (courtsavailable del fuente la "activa") y registrar jugadores -> al tick solo confirmar.
        try:
            fuentes = _candidatos_fuente(job)[:6]
            if fuentes:
                listo = api.precargar_objetivo(job["jugadores_full"], cancha_preferida=job["cancha"],
                                               fuentes=fuentes, single_double=job["single_double"],
                                               canchas_club=CANCHAS_POR_CLUB.get(job.get("club_id")))
            if listo:
                log(rid, f"PRE-CARGADO con turno fuente {listo['fuente']}: {len(listo['canchas'])} "
                         f"canchas activadas + jugadores registrados. Al tick solo queda confirmar.")
            else:
                log(rid, "no pude pre-cargar (sin turno fuente con la cancha); voy con el flujo normal.")
        except Exception as e:
            log(rid, f"aviso pre-carga: {e}; sigo con el flujo normal")
            listo = None
        sincronizar_reloj(forzar=True)   # offset NTP fresco UNA vez; el spin usa reloj local
        RETRASO = 1.0 / 20.0    # disparar 1/20 s DESPUÉS de la apertura: el turno ya está abierto
                                # y no se desperdicia el 1er intento en "el turno todavía no abrió"
        while hora_real() < epoch + RETRASO:
            time.sleep(0.005)
        log(rid, ">>> Disparando (1/20 s después de la apertura)...")

        # TICK RÁPIDO: si pre-cargamos, confirmar YA (solo confirm, ~0.5s). Si no entra, cae al
        # flujo normal completo de abajo (red de seguridad: nunca peor que hoy).
        if listo:
            try:
                resp = api.confirmar_objetivo(listo, job["fecha"], job["hora_desde"], job["hora_hasta"],
                                              job["jugadores_full"], single_double=job["single_double"],
                                              telephone=job["telefono"], cancha_preferida=job["cancha"])
                if resp.get("status") == "success":
                    cancha = (resp.get("_cancha") or {}).get("nombre", "")
                    log(rid, "=========================================")
                    log(rid, f"  RESERVA CONFIRMADA (OK, pre-carga)  cancha={cancha}  " + str(resp.get("result_text", "")))
                    log(rid, "=========================================")
                    return True
                log(rid, f"pre-carga no entró ({resp.get('status')}: "
                         f"{json.dumps(resp.get('errors', ''), ensure_ascii=False)[:80]}); sigo con el flujo normal...")
            except Exception as e:
                log(rid, f"confirm de pre-carga falló ({e}); sigo con el flujo normal")

    MAX_PASAJEROS = 8       # el "refresca tu pantalla" suele ser intermitente; reintentar ayuda
    pasajeros = 0
    for i in range(1, job["reintentos"] + 1):
        try:
            resp = _un_intento(api, job)   # flujo seguro: registra los jugadores DENTRO del form
        except Exception as e:
            log(rid, f"intento {i}: error {e}")
            time.sleep(job["espera"])
            continue
        estado = resp.get("status")
        if estado == "success":
            cancha = (resp.get("_cancha") or {}).get("nombre", "")
            multi = resp.get("_multiple")
            log(rid, "=========================================")
            log(rid, f"  RESERVA CONFIRMADA (OK)  cancha={cancha}  " + str(resp.get("result_text", "")))
            if multi:
                log(rid, f"  ⚠ OJO: entraron VARIAS canchas a la vez ({', '.join(multi)}). "
                         f"Quedó '{cancha}'; CANCELÁ LAS DEMÁS a mano en OnDepor.")
            log(rid, "=========================================")
            return True
        if estado in ("sin_cancha", "no_abierto"):
            motivo = "el turno todavía no abrió" if estado == "no_abierto" else "todavía no hay canchas"
            log(rid, f"intento {i}: {motivo}, reintento...")
            time.sleep(0.1)   # poll rápido para pescar la apertura apenas ocurre
            continue
        errores = json.dumps(resp.get("errors", resp), ensure_ascii=False).lower()
        # (si el turno no abrió, ya salió por 'no_abierto' arriba; acá "ocupada" = está ocupada de verdad)
        transitorio = "refresca" in errores or "intenta" in errores or "ocurrió un error" in errores or "ocurrio un error" in errores
        if transitorio and pasajeros < MAX_PASAJEROS:
            pasajeros += 1
            log(rid, f"intento {i}: error pasajero, reintento ({pasajeros}/{MAX_PASAJEROS})...")
            time.sleep(job["espera"])
            continue
        # rechazo (definitivo, o "refresca" persistente = casi seguro un rechazo real del club)
        log(rid, "=========================================")
        if transitorio:
            log(rid, "  NO SE PUDO RESERVAR tras varios intentos. Puede ser:")
            log(rid, "  (a) un error temporal de OnDepor -> probá de nuevo en un momento; o")
            log(rid, "  (b) que vos o algún jugador YA tengan un turno ese día / se llegó al")
            log(rid, "      máximo de reservas -> cambiá de fecha o de jugadores.")
        else:
            log(rid, f"  RECHAZO -> {json.dumps(resp.get('errors', resp), ensure_ascii=False)[:300]}")
        log(rid, "=========================================")
        return False
    log(rid, f"No se pudo tomar el turno tras {job['reintentos']} intentos.")
    return False


def disparar_varias(jobs, esperar=True):
    """Corre varias reservas EN PARALELO (una hebra por reserva)."""
    if len(jobs) == 1:
        return disparar(jobs[0], esperar)
    hilos = []
    for job in jobs:
        t = threading.Thread(target=disparar, args=(job, esperar), daemon=True)
        t.start()
        hilos.append(t)
    for t in hilos:
        t.join()


# ----------------------------------------------------------------------
#  Modos informativos
# ----------------------------------------------------------------------
def modo_listar():
    jobs = buscar_trabajos("all")
    if not jobs:
        print("No hay reservas cargadas en reservas.json")
        return
    print("Reservas cargadas:\n")
    for j in jobs:
        print(f"  [{j['id']}]  {j['fecha']} {j['hora_desde']}-{j['hora_hasta']}  "
              f"club={j['club_id']} cancha={j['cancha'] or 'cualquiera'}")
        print(f"       reserva: {j['reservador']}  +  jugadores {j['player_ids']}"
              + (f"  (FALTAN: {j['faltan_jugadores']})" if j['faltan_jugadores'] else ""))
        print(f"       se dispara: {j['apertura_dt'].strftime('%Y-%m-%d %H:%M:%S')} (hora Argentina)")
        if j.get("problema"):
            print(f"       (!) PENDIENTE: {j['problema']}")
        print()


def _reservador_para_login():
    """Devuelve un reservador con clave cargada, para poder loguearse y buscar."""
    _, reservadores, _ = cargar_todo()
    res = next((r for r in reservadores
                if r.get("clave") and not str(r.get("clave", "")).startswith("PONER")), None)
    if not res:
        print("Primero cargá el usuario y la clave de al menos un reservador en db_reservadores.json")
    return res


def _guardar_jugadores(lista):
    """Reescribe db_jugadores.json preservando el texto de ayuda."""
    ruta = os.path.join(AQUI, "db_jugadores.json")
    try:
        data = json.load(open(ruta, encoding="utf-8"))
    except Exception:
        data = {}
    data["jugadores"] = lista
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def modo_buscar_jugador(texto):
    """Solo muestra resultados (no guarda)."""
    res = _reservador_para_login()
    if not res:
        return
    api = OnDepor(res["usuario"], res["clave"], 1102)
    api.login()
    lista = api.buscar_jugadores_lista(texto)
    if not lista:
        print(f"Sin resultados para '{texto}'.")
        return
    print(f"Resultados para '{texto}':\n")
    for it in lista:
        print(f"  player_id {it['id']}   {it['nombre']}")


def modo_agregar_jugador(texto):
    """Busca en OnDepor, te deja elegir y guarda solo en db_jugadores.json."""
    if not texto:
        texto = input("Nombre o apellido a buscar: ").strip()
    if not texto:
        print("No escribiste nada.")
        return
    res = _reservador_para_login()
    if not res:
        return
    print("Conectando a OnDepor...")
    api = OnDepor(res["usuario"], res["clave"], 1102)
    api.login()
    encontrados = api.buscar_jugadores_lista(texto)
    if not encontrados:
        print(f"Sin resultados para '{texto}'. Probá con otra parte del nombre.")
        return

    print(f"\nEncontré {len(encontrados)} jugador(es):\n")
    for i, it in enumerate(encontrados, 1):
        print(f"  {i}) {it['nombre']}   (player_id {it['id']})")
    print()
    sel = input("¿Cuáles agrego? Números separados por coma (ej: 1,3), 'todos', o Enter para cancelar: ").strip().lower()
    if not sel:
        print("Cancelado, no agregué nada.")
        return
    if sel in ("todos", "todo", "all", "*"):
        elegidos = encontrados
    else:
        idxs = []
        for parte in sel.replace(" ", "").split(","):
            if parte.isdigit() and 1 <= int(parte) <= len(encontrados):
                idxs.append(int(parte) - 1)
        elegidos = [encontrados[i] for i in idxs]
    if not elegidos:
        print("No entendí la selección, no agregué nada.")
        return

    # cargar base actual y agregar sin duplicar (por player_id)
    actuales = _cargar("db_jugadores.json", "jugadores")
    ids_existentes = {str(j.get("player_id")) for j in actuales}
    agregados, repetidos = [], []
    for it in elegidos:
        if str(it["id"]) in ids_existentes:
            repetidos.append(it["nombre"])
        else:
            actuales.append({"nombre": it["nombre"], "player_id": it["id"]})
            ids_existentes.add(str(it["id"]))
            agregados.append(it["nombre"])
    _guardar_jugadores(actuales)

    print()
    if agregados:
        print("Agregados a db_jugadores.json: " + ", ".join(agregados))
    if repetidos:
        print("Ya estaban (no los dupliqué): " + ", ".join(repetidos))
    print(f"Total de jugadores en la base ahora: {len(actuales)}")


# ----------------------------------------------------------------------
def main():
    modo = sys.argv[1] if len(sys.argv) > 1 else "listar"
    arg = sys.argv[2] if len(sys.argv) > 2 else "all"
    try:
        if modo == "listar":
            modo_listar()
        elif modo == "buscar-jugador":
            modo_buscar_jugador(sys.argv[2] if len(sys.argv) > 2 else "")
        elif modo == "agregar-jugador":
            modo_agregar_jugador(" ".join(sys.argv[2:]) if len(sys.argv) > 2 else "")
        elif modo == "probar":
            for j in buscar_trabajos(arg):
                log(j["id"], "== ENSAYO (loguea y lista canchas, NO reserva) ==")
                if j.get("problema"):
                    log(j["id"], f"NO puedo probar: {j['problema']}")
                    continue
                try:
                    api = _login_y_tokens(j)
                    log(j["id"], f"Login OK. svid={api.svid} csrf={'OK' if api.csrf else 'NO'}")
                    jid = api.abrir_formulario(j["fecha"], j["hora_desde"])
                    log(j["id"], f"Formulario abierto, jid={jid}")
                    canchas = api.canchas_disponibles(j["fecha"], j["hora_desde"], j["hora_hasta"],
                                                      jid, j["single_double"])
                    if canchas:
                        elegida = api.elegir_cancha(canchas, j["cancha"])
                        log(j["id"], f"Canchas disponibles: {[c['nombre'] for c in canchas]}")
                        log(j["id"], f"Elegiría: {elegida['nombre']} (court_id {elegida['court_id']})")
                        log(j["id"], f"Jugadores a cargar: {j['player_ids']}")
                    else:
                        log(j["id"], "Sin canchas ahora (normal si el turno aún no abrió).")
                except Exception as e:
                    log(j["id"], f"ERROR: {e}")
        elif modo == "ya":
            disparar_varias(buscar_trabajos(arg), esperar=False)
        elif modo == "correr":
            disparar_varias(buscar_trabajos(arg), esperar=True)
        else:
            print(__doc__)
    except Exception as e:
        print("ERROR:", e)


if __name__ == "__main__":
    main()
