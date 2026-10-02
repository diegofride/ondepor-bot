# -*- coding: utf-8 -*-
"""
==========================================================================
  test_bot.py  -  Tests automáticos de "Reservame ésta!"
==========================================================================
Cubren lo que se rompió alguna vez (y no queremos que se rompa de nuevo):
fechas en hora Argentina, elección de cancha, reservas mal formadas,
pre-carga, permisos, validaciones del formulario y escape de HTML.

NO tocan OnDepor ni la red: todo con datos de prueba en un directorio
temporal. Se pueden correr con la app en producción sin riesgo.

    python test_bot.py            (o:  python -m unittest test_bot -v)
==========================================================================
"""
import os
import sys
import json
import shutil
import tempfile
import datetime
import unittest

os.environ["PANEL_COOKIE_INSEGURA"] = "1"     # sin HTTPS en los tests
AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)


# ----------------------------------------------------------------------
#  Datos de prueba (se escriben en un dir temporal, no tocan los reales)
# ----------------------------------------------------------------------
def _datos_base(tmp, dia_objetivo):
    def W(nombre, obj):
        with open(os.path.join(tmp, nombre), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    W("db_reservadores.json", {"reservadores": [
        {"nombre": "Diego Fride", "usuario": "d@x.com", "clave": "secreta", "player_id": 252966},
        {"nombre": "Sin Clave", "usuario": "s@x.com", "clave": "PONER_CLAVE", "player_id": 111}]})
    W("db_jugadores.json", {"jugadores": [
        {"nombre": "Pablo Becker", "player_id": 253510},
        {"nombre": "Lionel Steisel", "player_id": 130962},
        {"nombre": "Ariel Pernik", "player_id": 304064}]})
    W("usuarios.json", {"usuarios": [
        {"usuario": "diego", "clave": "clave1", "nombre": "Diego", "rol": "admin", "admin": True},
        {"usuario": "oper", "clave": "clave3", "nombre": "Oper", "rol": "operador"},
        {"usuario": "pablo", "clave": "clave2", "nombre": "Pablo", "admin": False}]})   # formato viejo
    W("recurrentes.json", {"recurrentes": []})
    W("reservas.json", {"reservas": [{
        "id": "p1", "club_id": 1102, "cancha": "3", "fecha": dia_objetivo,
        "hora_desde": "09:00", "hora_hasta": "10:00", "single_double": 2,
        "reservador": "Diego Fride", "jugadores": [253510, 130962, 304064],
        "creado_por": "Diego", "apertura_iso": None, "horas_antes": 24,
        "minutos_extra": 1, "reintentos": 40, "espera_entre_reintentos_seg": 0.3}]})
    W("estados.json", {})
    W("historial.json", {"historial": []})
    W("webconfig.json", {"password": "x"})


class BaseApp(unittest.TestCase):
    """Levanta la app apuntando a un directorio temporal con datos de prueba."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="reservame_test_")
        cls.dia = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
        _datos_base(cls.tmp, cls.dia)
        shutil.copytree(os.path.join(AQUI, "static"), os.path.join(cls.tmp, "static"),
                        dirs_exist_ok=True)
        import ondepor_core, reservar_bot, webapp          # noqa: E402
        cls.core, cls.rb, cls.web = ondepor_core, reservar_bot, webapp
        # redirigir TODA la lectura/escritura al dir temporal
        reservar_bot.AQUI = cls.tmp
        reservar_bot.LOGS = os.path.join(cls.tmp, "logs")
        webapp.AQUI = cls.tmp
        webapp.BACKUP_DIR = os.path.join(cls.tmp, "backups")
        webapp.ESTADOS_PATH = os.path.join(cls.tmp, "estados.json")
        webapp._cargar_estados()
        webapp.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def cliente(self, usuario="diego", clave="clave1"):
        c = self.web.app.test_client()
        c.post("/login", data={"usuario": usuario, "password": clave})
        return c


# ----------------------------------------------------------------------
#  1. Fechas en hora Argentina (el server corre en UTC)
# ----------------------------------------------------------------------
class TestHoraArgentina(BaseApp):
    def test_hoy_ar_es_utc_menos_3(self):
        utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        esperado = (utc - datetime.timedelta(hours=3)).date()
        self.assertEqual(self.core.hoy_ar(), esperado)

    def test_ahora_ar_tres_horas_atras(self):
        utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        dif = (utc - self.core.ahora_ar()).total_seconds()
        self.assertAlmostEqual(dif, 3 * 3600, delta=5)

    def test_mis_reservas_pide_desde_ayer_arg(self):
        """A1: si pidiera desde la fecha UTC del server, de 21 a 24 hs ARG perdería las
        reservas de HOY. Se comprueba el rango REAL que se le manda a OnDepor."""
        capturado = {}

        class SesionFalsa:
            def get(self, url, params=None, headers=None, timeout=None):
                capturado.update(params or {})
                raise RuntimeError("corto acá: solo quiero ver los params")

        api = self.core.OnDepor.__new__(self.core.OnDepor)
        api.s, api.svid, api.csrf, api.tz = SesionFalsa(), "x", "y", -3
        api.mis_reservas(30)                       # devuelve [] por la excepción, no importa
        ayer_ar = (self.core.hoy_ar() - datetime.timedelta(days=1)).isoformat()
        self.assertEqual(capturado.get("date_from"), ayer_ar,
                         "el rango debe arrancar ayer en hora ARG, no en la fecha UTC del server")

    def test_apertura_es_24h_menos_1min_antes(self):
        dt, _ = self.core.calcular_apertura("2026-07-26", "22:00")
        self.assertEqual(dt.strftime("%Y-%m-%d %H:%M"), "2026-07-25 22:01")


# ----------------------------------------------------------------------
#  2. Elección de cancha (criterio unificado)
# ----------------------------------------------------------------------
class TestReconciliacion(BaseApp):
    """Lo unico del sistema que MIRA en vez de actuar: cruza el panel contra OnDepor."""

    def _reserva(self, estado="reservada"):
        obj = {"id": "rec-1", "club_id": 1102, "cancha": "3", "fecha": "2026-08-15",
               "hora_desde": "15:00", "hora_hasta": "16:00", "single_double": 2,
               "reservador": "Diego Fride", "jugadores": [253510, 130962, 304064],
               "creado_por": "Diego"}
        with open(os.path.join(self.tmp, "reservas.json"), "w", encoding="utf-8") as f:
            json.dump({"reservas": [obj]}, f, ensure_ascii=False)
        with open(os.path.join(self.tmp, "db_reservadores.json"), "w", encoding="utf-8") as f:
            json.dump({"reservadores": [{"nombre": "Diego Fride", "usuario": "d@x.com",
                                         "clave": "secreta", "player_id": 252966}]}, f)
        self.web.set_estado("rec-1", estado)
        return [obj]

    def _api_falsa(self, turnos):
        class Falsa:
            def __init__(self, *a, **k):
                pass

            def login(self):
                pass

            def mis_reservas(self, dias=30):
                return list(turnos)
        return Falsa

    def _parche(self, nombre, valor):
        original = getattr(self.web, nombre)
        setattr(self.web, nombre, valor)
        self.addCleanup(setattr, self.web, nombre, original)

    def test_si_el_turno_ya_no_esta_lo_marca_caido_y_avisa(self):
        reservas = self._reserva()
        avisos = []
        self._parche("OnDepor", self._api_falsa([]))       # OnDepor dice: no tenes nada
        self._parche("notificar", lambda u, t, c, url="/reservas": avisos.append((u, t)))
        # una lista vacia puede ser un error de red, asi que NO alcanza para degradar
        self.assertEqual(self.web._reconciliar_con_ondepor(reservas), 0)
        self.assertEqual(self.web.get_estado("rec-1"), "reservada")
        # pero si OnDepor contesta con OTROS turnos, el nuestro se cayo de verdad
        self._parche("OnDepor", self._api_falsa(
            [{"id": 9, "fecha": "2026-08-15", "club_id": 1102, "hora_to": "21:00"}]))
        self.assertEqual(self.web._reconciliar_con_ondepor(reservas), 1)
        self.assertEqual(self.web.get_estado("rec-1"), "fallada")
        self.assertEqual([u for u, _ in avisos], ["Diego"])

    def test_si_el_turno_esta_no_toca_nada(self):
        reservas = self._reserva()
        self._parche("OnDepor", self._api_falsa(
            [{"id": 1, "fecha": "2026-08-15", "club_id": 1102, "hora_to": "16:00"}]))
        self.assertEqual(self.web._reconciliar_con_ondepor(reservas), 0)
        self.assertEqual(self.web.get_estado("rec-1"), "reservada")

    def test_si_ondepor_no_contesta_no_degrada_nada(self):
        """Sin esto, un corte de internet marcaria TODAS las canchas como caidas."""
        reservas = self._reserva()

        class Rota:
            def __init__(self, *a, **k):
                pass

            def login(self):
                raise RuntimeError("sin red")
        self._parche("OnDepor", Rota)
        self.assertEqual(self.web._reconciliar_con_ondepor(reservas), 0)
        self.assertEqual(self.web.get_estado("rec-1"), "reservada")

    def test_solo_mira_las_confirmadas(self):
        reservas = self._reserva(estado="programada")
        llamadas = []

        class Espia:
            def __init__(self, *a, **k):
                llamadas.append(1)

            def login(self):
                pass

            def mis_reservas(self, dias=30):
                return []
        self._parche("OnDepor", Espia)
        self.web._reconciliar_con_ondepor(reservas)
        self.assertEqual(llamadas, [], "le pregunto a OnDepor por una reserva sin confirmar")


class TestMotorDeAcciones(BaseApp):
    """El punto de unificar: las garantias se escriben UNA vez y valen para todas las
    acciones. Antes estaban copiadas y cuando se arreglo una no se arreglo la otra."""

    def test_las_dos_acciones_estan_registradas(self):
        self.assertEqual(set(self.web.ACCIONES_PROGRAMADAS),
                         {"cambio_prog", "cancel_prog"})

    def _reserva(self, campo, estado_reserva, ej_epoch, fecha=None):
        manana = fecha or (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        obj = {"id": "acc-1", "club_id": 1102, "cancha": "3", "fecha": manana,
               "hora_desde": "15:00", "hora_hasta": "16:00", "single_double": 2,
               "reservador": "Diego Fride", "jugadores": [253510, 130962, 304064],
               "creado_por": "Diego",
               campo: {"estado": "pendiente", "detalle": "", "hora_ej": "14:50",
                       "ej_epoch": ej_epoch, "creado_por": "Diego"}}
        with open(os.path.join(self.tmp, "reservas.json"), "w", encoding="utf-8") as f:
            json.dump({"reservas": [obj]}, f, ensure_ascii=False)
        self.web.set_estado("acc-1", estado_reserva)
        return obj

    def _estado(self, campo):
        r = self.web.cargar("reservas.json", "reservas")[0]
        return (r.get(campo) or {}).get("estado")

    def test_ninguna_accion_corre_sobre_una_reserva_sin_confirmar(self):
        """La garantia mas cara: sobre una reserva que no se disparo, actuar en OnDepor
        se lleva OTRO turno del dia. Vale para las DOS acciones."""
        for campo in ("cambio_prog", "cancel_prog"):
            corridas = []
            original = self.web._correr_accion
            self.web._correr_accion = lambda c, r: corridas.append(c)
            try:
                self._reserva(campo, "programada", self.core.hora_real() - 5)
                self.web._revisar_acciones_prog()
                self.assertEqual(corridas, [], f"{campo} corrio sin turno confirmado")
                self.assertEqual(self._estado(campo), "pendiente")
            finally:
                self.web._correr_accion = original

    def test_ninguna_accion_corre_con_el_turno_ya_empezado(self):
        ayer = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        for campo in ("cambio_prog", "cancel_prog"):
            corridas = []
            original = self.web._correr_accion
            self.web._correr_accion = lambda c, r: corridas.append(c)
            try:
                self._reserva(campo, "reservada", 0, fecha=ayer)
                self.web._revisar_acciones_prog()
                self.assertEqual(corridas, [], f"{campo} corrio con el turno empezado")
                self.assertEqual(self._estado(campo), "vencido")
            finally:
                self.web._correr_accion = original

    def test_una_accion_que_revienta_no_queda_colgada_ni_muda(self):
        """Sin esto, una excepcion dejaba la accion en 'en_curso' para siempre y sin aviso."""
        avisos = []
        original_n, original_a = self.web.notificar, dict(self.web.ACCIONES_PROGRAMADAS)
        self.web.notificar = lambda u, t, c, url="/reservas": avisos.append(t)

        def explota(rid):
            raise RuntimeError("se cayo OnDepor")
        self.web.ACCIONES_PROGRAMADAS["cancel_prog"] = ("cancelación", explota, False)
        try:
            self._reserva("cancel_prog", "reservada", 0)
            self.web._set_accion("cancel_prog", "acc-1", "en_curso")
            self.web._correr_accion("cancel_prog", "acc-1")
            self.assertEqual(self._estado("cancel_prog"), "fallado")
            self.assertEqual(len(avisos), 1, "no aviso que la accion se cayo")
        finally:
            self.web.notificar = original_n
            self.web.ACCIONES_PROGRAMADAS.clear()
            self.web.ACCIONES_PROGRAMADAS.update(original_a)


class TestUbicarTurno(BaseApp):
    """UN solo criterio para decir 'este es el turno de esta reserva'. Los dos flujos que
    lo usan CANCELAN en OnDepor, y uno corre solo desde el reloj."""
    T = [{"id": 1, "fecha": "2026-08-15", "club_id": 1102, "hora_to": "16:00"},
         {"id": 2, "fecha": "2026-08-15", "club_id": 1102, "hora_to": "18:00"},
         {"id": 3, "fecha": "2026-08-16", "club_id": 1102, "hora_to": "12:00"}]

    def test_elige_por_hora_y_no_el_mas_temprano(self):
        """El bug: mis_reservas viene ordenada por hora, así que agarraba SIEMPRE el
        primero. Cambiar los carnets del turno de las 17 cancelaba el de las 15."""
        t, motivo = self.core.OnDepor.ubicar_turno(self.T, "2026-08-15", 1102, "18:00")
        self.assertEqual(t["id"], 2)
        self.assertEqual(motivo, "")

    def test_con_varios_y_ninguno_que_coincida_no_toca_nada(self):
        t, motivo = self.core.OnDepor.ubicar_turno(self.T, "2026-08-15", 1102, "21:00")
        self.assertIsNone(t, "iba a cancelar un turno que no era")
        self.assertIn("no toco nada", motivo)

    def test_si_hay_uno_solo_ese_dia_no_hay_con_que_confundirlo(self):
        t, _ = self.core.OnDepor.ubicar_turno(self.T, "2026-08-16", 1102, "99:99")
        self.assertEqual(t["id"], 3)

    def test_estricto_no_acepta_otro_turno_del_dia(self):
        """Para RECONCILIAR la pregunta es '¿mi turno de las 15 sigue estando?': tener
        otra cancha ese día no es una respuesta."""
        solo_otro = [{"id": 9, "fecha": "2026-08-15", "club_id": 1102, "hora_to": "21:00"}]
        t, _ = self.core.ubicar_turno(solo_otro, "2026-08-15", 1102, "16:00")
        self.assertIsNotNone(t, "sin estricto, el unico del dia vale")
        t2, motivo = self.core.ubicar_turno(solo_otro, "2026-08-15", 1102, "16:00",
                                            estricto=True)
        self.assertIsNone(t2)
        self.assertIn("16:00", motivo)

    def test_otro_club_no_cuenta(self):
        t, motivo = self.core.OnDepor.ubicar_turno(self.T, "2026-08-15", 999, "16:00")
        self.assertIsNone(t)
        self.assertIn("no encontré", motivo.lower())


class TestCambiarJugadoresUbica(BaseApp):
    def _api(self, turnos, entra=False):
        api = self.core.OnDepor.__new__(self.core.OnDepor)
        api.club_id, api.svid, api.csrf = 1102, "x", "y"
        api.mis_reservas = lambda dias=30: list(turnos)
        api.buscar_jugadores_lista = lambda q: None
        api.canceladas, api.retomas = [], []
        api.cancelar_reserva = lambda rid: (api.canceladas.append(rid) or
                                            {"status": "success"})

        def reservar(f, hd, hh, jug, cancha_preferida="", single_double=2, telephone=""):
            api.retomas.append((hd, hh, cancha_preferida))
            return {"status": "success", "_cancha": {"nombre": "05"}} if entra else {"status": "no"}
        api.reservar = reservar
        return api

    def test_no_cancela_el_turno_equivocado(self):
        turnos = [{"id": 1, "fecha": "2026-08-15", "club_id": 1102, "hora_to": "16:00",
                   "cancha_num": "3"},
                  {"id": 2, "fecha": "2026-08-15", "club_id": 1102, "hora_to": "18:00",
                   "cancha_num": "9"}]
        api = self._api(turnos, entra=True)
        r = api.cambiar_jugadores("2026-08-15", 1102, "17:00", "18:00", [1, 2, 3], [4, 5, 6])
        self.assertEqual(r["status"], "success")
        self.assertEqual(api.canceladas, [2], "canceló el turno de otra hora")

    def test_la_red_de_seguridad_recupera_la_hora_QUE_SE_CANCELO(self):
        """Antes usaba la hora que le pasaron por parámetro y nunca la del turno que
        acababa de cancelar: si el match era otro, prometía recuperar y no podía."""
        turnos = [{"id": 7, "fecha": "2026-08-15", "club_id": 1102, "hora_to": "16:00",
                   "cancha_num": "3"}]
        api = self._api(turnos, entra=False)
        r = api.cambiar_jugadores("2026-08-15", 1102, "17:00", "18:00", [1, 2, 3], [4, 5, 6])
        self.assertEqual(r["status"], "perdida")
        # las ultimas 5 retomas son la red de seguridad: tienen que ir a 15:00-16:00
        self.assertEqual(api.retomas[-1][:2], ("15:00", "16:00"))

    def test_con_varios_turnos_dudosos_no_cancela_nada(self):
        turnos = [{"id": 1, "fecha": "2026-08-15", "club_id": 1102, "hora_to": "16:00"},
                  {"id": 2, "fecha": "2026-08-15", "club_id": 1102, "hora_to": "18:00"}]
        api = self._api(turnos)
        r = api.cambiar_jugadores("2026-08-15", 1102, "20:00", "21:00", [1, 2, 3], [4, 5, 6])
        self.assertEqual(r["status"], "no_encontrada")
        self.assertEqual(api.canceladas, [], "canceló igual sin saber cuál era")


class TestCanchas(BaseApp):
    C = [{"court_id": 12739, "nombre": "01-QUINTA"},
         {"court_id": 12596, "nombre": "05-KINERET"},
         {"court_id": 12597, "nombre": "06-KINERET"}]

    def test_elige_por_numero(self):
        self.assertEqual(self.core.OnDepor.elegir_cancha(self.C, "6")["nombre"], "06-KINERET")

    def test_elige_por_court_id(self):
        self.assertEqual(self.core.OnDepor.elegir_cancha(self.C, "12597")["nombre"], "06-KINERET")

    def test_zona_pone_las_de_la_zona_primero(self):
        orden = [c["nombre"] for c in self.core.OnDepor.ordenar_canchas(self.C, "KINERET")]
        self.assertEqual(orden[:2], ["05-KINERET", "06-KINERET"])

    def test_sin_preferencia_respeta_el_orden_de_ondepor(self):
        orden = [c["nombre"] for c in self.core.OnDepor.ordenar_canchas(self.C, "")]
        self.assertEqual(orden, [c["nombre"] for c in self.C])

    def test_la_cancha_1_no_matchea_con_la_10_ni_la_11(self):
        """BUG REAL (07/08/2026): se pidió la cancha 1 y el bot reservó la 10. El
        criterio hacía 'in' sobre el nombre, y "1" está dentro de "10-CHACRA". Peor
        todavía: la pre-carga creía que la 1 ya estaba en la lista y no la agregaba."""
        todas = self.C + [{"court_id": 14425, "nombre": "09-CHACRA"},
                          {"court_id": 14426, "nombre": "10-CHACRA"},
                          {"court_id": 14427, "nombre": "11-CHACRA"}]
        coincide = self.core.OnDepor._cancha_coincide
        self.assertTrue(coincide({"court_id": 12739, "nombre": "01-QUINTA"}, "1"))
        for nom in ("10-CHACRA", "11-CHACRA"):
            self.assertFalse(coincide({"court_id": 0, "nombre": nom}, "1"),
                             f"{nom} no es la cancha 1")
        # y al ordenar, la 1 tiene que quedar primera (no la 10)
        orden = [c["nombre"] for c in self.core.OnDepor.ordenar_canchas(todas, "1")]
        self.assertEqual(orden[0], "01-QUINTA")
        # el mismo problema al revés: pedir la 9 no puede traer la 19 si existiera
        self.assertFalse(coincide({"court_id": 0, "nombre": "19-CHACRA"}, "9"))
        # las zonas por nombre siguen andando
        self.assertTrue(coincide({"court_id": 0, "nombre": "05-KINERET"}, "KINERET"))

    def test_si_la_pedida_no_esta_en_la_lista_se_agrega_adelante(self):
        """Consecuencia del bug anterior: como creía que la 1 estaba (por la 10), la
        pre-carga no la agregaba y confirmaba en la 10."""
        sin_la_1 = [{"court_id": 14426, "nombre": "10-CHACRA"},
                    {"court_id": 14427, "nombre": "11-CHACRA"}]
        hay = any(self.core.OnDepor._cancha_coincide(c, "1") for c in sin_la_1)
        self.assertFalse(hay, "creía que la cancha 1 estaba en la lista")

    def test_detecta_la_cancha_tambien_con_precarga(self):
        """La app leía la cancha del log buscando "(OK)" exacto, pero las reservas que
        entran por pre-carga escriben "(OK, pre-carga)": mostraba "(no se sabe)"."""
        import os
        os.makedirs(os.path.join(self.tmp, "logs"), exist_ok=True)
        rid = "log-precarga"
        with open(os.path.join(self.tmp, "logs", rid + ".log"), "w", encoding="utf-8") as f:
            f.write("  RESERVA CONFIRMADA (OK, pre-carga)  cancha=01-QUINTA  ok\n")
        self.assertEqual(self.web.cancha_reservada(rid), "01-QUINTA")

    def test_ordenar_no_pierde_ni_duplica_canchas(self):
        for pref in ("6", "KINERET", "", "99"):
            orden = self.core.OnDepor.ordenar_canchas(self.C, pref)
            self.assertEqual(sorted(c["court_id"] for c in orden),
                             sorted(c["court_id"] for c in self.C), f"falló con {pref!r}")


# ----------------------------------------------------------------------
#  3. Reservas mal formadas: nunca deben tumbar al reloj interno
# ----------------------------------------------------------------------
class TestReservaRota(BaseApp):
    def _armar(self, reserva):
        jug = self.rb._cargar("db_jugadores.json", "jugadores")
        res = self.rb._cargar("db_reservadores.json", "reservadores")
        return self.rb.armar_trabajo(reserva, jug, res)

    def test_sin_fecha_no_explota_y_marca_problema(self):
        job = self._armar({"id": "x", "reservador": "Diego Fride", "jugadores": []})
        self.assertTrue(job["problema"])
        self.assertEqual(job["apertura_epoch"], 0.0)

    def test_reservador_inexistente_marca_problema(self):
        job = self._armar({"id": "x", "fecha": self.dia, "hora_desde": "09:00",
                           "reservador": "Nadie", "jugadores": []})
        self.assertIn("no encontré al reservador", job["problema"])

    def test_reservador_sin_clave_marca_problema(self):
        job = self._armar({"id": "x", "fecha": self.dia, "hora_desde": "09:00",
                           "reservador": "Sin Clave", "jugadores": []})
        self.assertIn("clave", job["problema"])

    def test_una_rota_no_impide_armar_las_demas(self):
        jug = self.rb._cargar("db_jugadores.json", "jugadores")
        res = self.rb._cargar("db_reservadores.json", "reservadores")
        buenas = self.rb._cargar("reservas.json", "reservas")
        jobs = [self.rb.armar_trabajo(r, jug, res) for r in ([{"id": "rota"}] + buenas)]
        self.assertEqual(len(jobs), 2)
        self.assertTrue(jobs[0]["problema"])
        self.assertIsNone(jobs[1]["problema"])


# ----------------------------------------------------------------------
#  4. Pre-carga (enfoque de turno fuente)
# ----------------------------------------------------------------------
class TestPrecarga(BaseApp):
    def test_candidatos_usan_fecha_argentina(self):
        manana = (self.core.hoy_ar() + datetime.timedelta(days=1)).isoformat()
        # 08:00 es el primer turno del día: no hay horas anteriores, el fuente va a ser HOY
        cands = self.rb._candidatos_fuente({"fecha": manana, "hora_desde": "08:00", "club_id": 1102})
        self.assertTrue(cands)
        self.assertEqual(cands[0][0], self.core.hoy_ar().isoformat())

    def test_candidatos_del_mismo_dia_van_primero(self):
        manana = (self.core.hoy_ar() + datetime.timedelta(days=1)).isoformat()
        cands = self.rb._candidatos_fuente({"fecha": manana, "hora_desde": "21:00", "club_id": 1102})
        self.assertEqual(cands[0], (manana, "20:00", "21:00"))

    def test_ultimo_turno_termina_en_24(self):
        """23:00 -> 24:00 (poner 00:00 da 'reservá al menos 1 hora')."""
        manana = (self.core.hoy_ar() + datetime.timedelta(days=1)).isoformat()
        cands = self.rb._candidatos_fuente({"fecha": manana, "hora_desde": "18:00", "club_id": 1102})
        self.assertIn((self.core.hoy_ar().isoformat(), "23:00", "24:00"), cands)

    def test_mapa_de_canchas_completo(self):
        self.assertEqual(len(self.rb.CANCHAS_POR_CLUB[1102]), 11)
        self.assertNotIn(504, self.rb.CANCHAS_POR_CLUB)      # el club "Noche" ya no existe

    def test_un_solo_club_de_8_a_23(self):
        """Septiembre 2026: todos los turnos en el 1102, de 8 a 23 h."""
        self.assertEqual(self.rb.HORAS_POR_CLUB[1102][0], "08:00")
        self.assertEqual(self.rb.HORAS_POR_CLUB[1102][-1], "23:00")
        self.assertEqual(len(self.web.SEDES), 1)
        self.assertEqual(self.web.SEDES["dia"]["club_id"], 1102)
        self.assertEqual(self.web.SEDES["dia"]["horas"], self.rb.HORAS_POR_CLUB[1102])

    def test_de_noche_no_hay_chacra_en_la_precarga(self):
        de_dia = self.rb.canchas_para(1102, "17:00")
        de_noche = self.rb.canchas_para(1102, "18:00")
        self.assertEqual(len(de_dia), 11)
        self.assertEqual(sorted(de_noche, key=int), ["1", "2", "3", "4", "5", "6", "7", "8"])
        self.assertEqual(self.rb.canchas_para(999, "10:00"), {})

    def test_precarga_40s_antes(self):
        self.assertEqual(self.rb.PRECARGA_ANTES_SEG, 40)

    # -- el motivo REAL por el que no abre el formulario (08/08/2026: el carnet de
    #    Lionel estaba ocupado y el log decía "¿svid/sesión?", que no es lo que pasó) --
    def _api_sin_jid(self, respuesta):
        api = self.core.OnDepor.__new__(self.core.OnDepor)
        api.club_id, api.svid, api.csrf = 1102, "x", "y"
        api._ultimo_form = respuesta
        api.abrir_formulario = lambda f, h: None
        return api

    def test_dice_por_que_no_abrio_el_formulario(self):
        api = self._api_sin_jid({"status": "failure",
                                 "message": "  <br>Tenés el máximo de reservas activas "
                                            "permitidas por el club.<br>"})
        r = api.reservar("2026-08-09", "10:00", "11:00", [{"id": 1, "nombre": "a"}], pausa=0)
        self.assertEqual(r["status"], "failure")
        self.assertEqual(r["errors"], "Tenés el máximo de reservas activas permitidas por el club.")

    def test_saca_el_mensaje_del_popup_html(self):
        api = self._api_sin_jid({"error": "<div class='popup'><h4>Reserva</h4>"
                                          "<div>  <br>Carnet ocupado.<br></div></div>"})
        r = api.reservar("2026-08-09", "10:00", "11:00", [{"id": 1, "nombre": "a"}], pausa=0)
        self.assertIn("Carnet ocupado.", r["errors"])

    def test_sin_mensaje_queda_el_texto_viejo(self):
        api = self._api_sin_jid({})
        r = api.reservar("2026-08-09", "10:00", "11:00", [{"id": 1, "nombre": "a"}], pausa=0)
        self.assertIn("svid", r["errors"])

    def test_el_turno_que_no_abrio_sigue_siendo_no_abierto(self):
        """Ese caso NO es un rechazo: hay que reintentar hasta que abra."""
        api = self._api_sin_jid({"message": "No se permite reservar con tanta anticipación"})
        r = api.reservar("2026-08-09", "10:00", "11:00", [{"id": 1, "nombre": "a"}], pausa=0)
        self.assertEqual(r["status"], "no_abierto")

    # -- el plan B no puede depender de lo que le sobre al turno fuente --
    def _api_fuente(self, canchas_del_fuente):
        api = self.core.OnDepor.__new__(self.core.OnDepor)
        api.club_id, api.svid, api.csrf, api._ultimo_form = 1102, "x", "y", {}
        api.abrir_formulario = lambda f, h: "J1"
        api.canchas_disponibles = lambda *a, **k: list(canchas_del_fuente)
        api.preparar_jugadores = lambda j: None
        return api

    def test_plan_b_suma_todas_las_canchas_del_club(self):
        """07/08/2026: el fuente tenía 2 libres, las 2 ocupadas en el objetivo, y el tick
        se quedó sin salida — mientras había 5 canchas libres a esa misma hora."""
        api = self._api_fuente([{"court_id": 12596, "nombre": "05-KINERET"}])
        listo = api.precargar_objetivo([{"id": 1, "nombre": "a"}], cancha_preferida="10",
                                       fuentes=[("2026-08-08", "14:00", "15:00")], pausa=0,
                                       canchas_club=self.rb.CANCHAS_POR_CLUB[1102])
        ids = [c["court_id"] for c in listo["canchas"]]
        self.assertEqual(len(ids), 11)                        # todas las del club
        self.assertEqual(ids[0], 14426)                       # la PEDIDA primera
        self.assertEqual(ids[1], 12596)                       # después la que sí ofreció el fuente
        self.assertEqual(len(set(ids)), 11)                   # sin repetidas

    def test_plan_b_no_repite_la_que_ya_estaba(self):
        api = self._api_fuente([{"court_id": 14426, "nombre": "10-CHACRA"},
                                {"court_id": 12596, "nombre": "05-KINERET"}])
        listo = api.precargar_objetivo([{"id": 1, "nombre": "a"}], cancha_preferida="10",
                                       fuentes=[("2026-08-08", "14:00", "15:00")], pausa=0,
                                       canchas_club=self.rb.CANCHAS_POR_CLUB[1102])
        ids = [c["court_id"] for c in listo["canchas"]]
        self.assertEqual(ids[0], 14426)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 11)

    def test_plan_b_sin_mapa_de_club_no_revienta(self):
        api = self._api_fuente([{"court_id": 12596, "nombre": "05-KINERET"}])
        listo = api.precargar_objetivo([{"id": 1, "nombre": "a"}], cancha_preferida="10",
                                       fuentes=[("2026-08-08", "14:00", "15:00")], pausa=0)
        self.assertEqual([c["court_id"] for c in listo["canchas"]], [12596])


# ----------------------------------------------------------------------
#  5. confirmar_objetivo: paralelo solo con cancha puntual ocupada
# ----------------------------------------------------------------------
class TestConfirmarObjetivo(BaseApp):
    def _api(self, ocupadas):
        api = self.core.OnDepor.__new__(self.core.OnDepor)
        api.club_id, api.svid, api.csrf, api._ultimo_form = 1102, "x", "y", {}
        api.s = type("S", (), {"headers": {}, "cookies": type("C", (), {"update": lambda *a: None})()})()
        api.cont = {"confirm": 0, "paralelo": 0}

        def confirmar(f, hd, hh, court, jid, pids, sd=2, tel="", sess=None):
            api.cont["confirm"] += 1
            if court in ocupadas:
                return {"status": "failure", "errors": {"general": ["La cancha no está disponible"]}}
            return {"status": "success", "result_text": "ok"}
        api.confirmar = confirmar
        orig = self.core.OnDepor._confirmar_paralelo

        def par(*a, **k):
            api.cont["paralelo"] += 1
            return orig(api, *a, **k)
        api._confirmar_paralelo = par
        api._clonar_sesion = lambda: api.s
        return api

    LISTO = {"jid": "J1", "canchas": [{"court_id": 6, "nombre": "06"},
                                      {"court_id": 7, "nombre": "07"},
                                      {"court_id": 8, "nombre": "08"}]}
    P = [{"id": 1, "nombre": "a"}]

    def test_cancha_libre_entra_al_primer_intento(self):
        api = self._api(set())
        r = api.confirmar_objetivo(self.LISTO, "2026-07-26", "22:00", "23:00", self.P, cancha_preferida="6")
        self.assertEqual(r["status"], "success")
        self.assertEqual(api.cont["confirm"], 1)
        self.assertEqual(api.cont["paralelo"], 0)

    def test_cancha_puntual_ocupada_dispara_en_paralelo(self):
        api = self._api({6})
        r = api.confirmar_objetivo(self.LISTO, "2026-07-26", "22:00", "23:00", self.P, cancha_preferida="6")
        self.assertEqual(r["status"], "success")
        self.assertEqual(api.cont["paralelo"], 1)

    def test_cualquiera_libre_va_secuencial(self):
        api = self._api({6})
        r = api.confirmar_objetivo(self.LISTO, "2026-07-26", "22:00", "23:00", self.P, cancha_preferida="")
        self.assertEqual(r["status"], "success")
        self.assertEqual(api.cont["paralelo"], 0)

    def test_error_no_de_cancha_corta_para_el_fallback(self):
        api = self._api(set())
        api.confirmar = lambda *a, **k: {"status": "failure",
                                         "errors": {"general": ["Ocurrió un error, refresca tu pantalla"]}}
        r = api.confirmar_objetivo(self.LISTO, "2026-07-26", "22:00", "23:00", self.P, cancha_preferida="6")
        self.assertEqual(r["status"], "failure")

    # -- insistir con la cancha PEDIDA (07/08/2026: se pedía la 9 y la 10, volvían
    #    "ocupada" en ~1,3 s —una carrera perdida— y no se reintentaban nunca más) --
    def _api_ocupada_n_veces(self, cancha, veces):
        """La cancha 'cancha' contesta ocupada las primeras 'veces' y después entra."""
        api = self._api(set())
        quedan = {"n": veces}

        def confirmar(f, hd, hh, court, jid, pids, sd=2, tel="", sess=None):
            api.cont["confirm"] += 1
            if court == cancha and quedan["n"] > 0:
                quedan["n"] -= 1
                return {"status": "failure", "errors": {"general": ["Cancha no disponible."]}}
            return {"status": "success", "result_text": "ok"}
        api.confirmar = confirmar
        return api

    def test_insiste_con_la_pedida_y_se_la_queda(self):
        """Perdida por milésimas al primer tiro -> al segundo entra Y ES LA PEDIDA.
        Sin abrir el abanico: nada de quedarse con una alternativa (ni con dos canchas)."""
        api = self._api_ocupada_n_veces(6, 1)
        r = api.confirmar_objetivo(self.LISTO, "2026-07-26", "22:00", "23:00", self.P,
                                   cancha_preferida="6")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["_cancha"]["court_id"], 6)   # NO una alternativa
        self.assertEqual(api.cont["paralelo"], 0)       # NO se disparó nada más
        self.assertIsNone(r.get("_multiple"))

    def test_recien_en_la_ultima_ronda_abre_el_abanico(self):
        """Si la pedida se fue de verdad, la última ronda dispara todo junto — y si entran
        varias, la que queda es la pedida (va primera en el orden)."""
        api = self._api_ocupada_n_veces(6, 2)
        r = api.confirmar_objetivo(self.LISTO, "2026-07-26", "22:00", "23:00", self.P,
                                   cancha_preferida="6")
        self.assertEqual(r["status"], "success")
        self.assertEqual(api.cont["paralelo"], 1)
        self.assertEqual(r["_cancha"]["court_id"], 6)
        self.assertEqual(len(r.get("_multiple") or []), 3)   # avisa para cancelar las de sobra

    def test_no_insiste_para_siempre(self):
        api = self._api({6, 7, 8})
        r = api.confirmar_objetivo(self.LISTO, "2026-07-26", "22:00", "23:00", self.P,
                                   cancha_preferida="6", rondas=3)
        self.assertNotEqual(r["status"], "success")
        self.assertEqual(api.cont["paralelo"], 1)            # una sola ronda en paralelo
        self.assertEqual(api.cont["confirm"], 2 + 3)         # 2 tiros a la pedida + las 3 juntas

    def test_deja_de_insistir_si_el_error_no_es_de_cancha(self):
        """Cupo/jugador/'refresca' no se arreglan con otro tiro: cortar para el flujo normal."""
        api = self._api({6})
        n = {"i": 0}
        base = api.confirmar

        def confirmar(*a, **k):
            n["i"] += 1
            if n["i"] == 1:
                return base(*a, **k)                          # 1er tiro: ocupada -> hay ronda 2
            return {"status": "failure",
                    "errors": {"general": ["Juan tiene el máximo de reservas activas"]}}
        api.confirmar = confirmar
        r = api.confirmar_objetivo(self.LISTO, "2026-07-26", "22:00", "23:00", self.P,
                                   cancha_preferida="6")
        self.assertNotEqual(r["status"], "success")
        self.assertEqual(n["i"], 2)                           # cortó en la ronda 2
        self.assertEqual(api.cont["paralelo"], 0)

    def test_zona_no_insiste_ni_dispara_en_paralelo(self):
        api = self._api({6, 7, 8})
        r = api.confirmar_objetivo(self.LISTO, "2026-07-26", "22:00", "23:00", self.P,
                                   cancha_preferida="KINERET")
        self.assertNotEqual(r["status"], "success")
        self.assertEqual(api.cont["paralelo"], 0)
        self.assertEqual(api.cont["confirm"], 3)             # una vuelta y nada más

    def test_tope_de_tiempo_corta_las_rondas(self):
        api = self._api({6, 7, 8})
        api.confirmar_objetivo(self.LISTO, "2026-07-26", "22:00", "23:00", self.P,
                               cancha_preferida="6", limite_seg=0)
        self.assertEqual(api.cont["paralelo"], 0)

    def test_cada_tiro_queda_registrado_con_su_tiempo(self):
        """Sin esto no se distingue 'está ocupada' (0,2-0,4 s) de una carrera perdida (~1,3 s)."""
        api = self._api({6})
        r = api.confirmar_objetivo(self.LISTO, "2026-07-26", "22:00", "23:00", self.P,
                                   cancha_preferida="6")
        tiros = r["_intentos"]
        self.assertEqual(tiros[0]["cancha"], "06")
        self.assertIn("disponible", tiros[0]["error"].lower())
        self.assertTrue(all(isinstance(t["ms"], int) for t in tiros))
        self.assertGreater(len(tiros), 1)

    def test_sin_canchas_no_revienta(self):
        api = self._api(set())
        r = api.confirmar_objetivo({"jid": "J1", "canchas": []}, "2026-07-26", "22:00",
                                   "23:00", self.P, cancha_preferida="6")
        self.assertEqual(r["status"], "sin_cancha")
        self.assertEqual(api.cont["confirm"], 0)


# ----------------------------------------------------------------------
#  6. Validaciones del formulario (backend, no solo el navegador)
# ----------------------------------------------------------------------
class TestFormulario(BaseApp):
    def _form(self, **kw):
        from werkzeug.datastructures import MultiDict
        base = {"sede": "dia", "fecha": self.dia, "hora": "09:00", "cancha": "3",
                "reservador": "Diego Fride", "jug": ["253510", "130962", "304064"]}
        base.update(kw)
        md = MultiDict()
        for k, v in base.items():
            for x in (v if isinstance(v, list) else [v]):
                md.add(k, x)
        return md

    def _guardar(self, form):
        with self.web.app.test_request_context():
            from flask import session as s
            s["usuario"] = "Diego"
            return self.web._guardar_reserva_desde_form(form)

    def test_fecha_pasada_se_rechaza(self):
        ayer = (self.core.hoy_ar() - datetime.timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):
            self._guardar(self._form(fecha=ayer))

    def test_hora_fuera_de_la_grilla_se_rechaza(self):
        with self.assertRaises(ValueError):      # el club abre a las 8
            self._guardar(self._form(sede="dia", hora="07:00"))

    def test_turno_de_noche_se_guarda_en_el_mismo_club(self):
        """Desde septiembre 2026 la noche va en el 1102 (antes era el club 504)."""
        rid = self._guardar(self._form(hora="22:00", cancha="3"))
        r = next(x for x in self.web.cargar("reservas.json", "reservas") if x["id"] == rid)
        self.assertEqual(r["club_id"], 1102)
        self.assertEqual(r["hora_desde"], "22:00")

    def test_de_noche_no_hay_chacra(self):
        with self.assertRaises(ValueError):
            self._guardar(self._form(hora="18:00", cancha="10"))
        with self.assertRaises(ValueError):
            self._guardar(self._form(hora="22:00", cancha="CHACRA"))
        self._guardar(self._form(hora="17:00", cancha="10"))      # hasta las 17 sí
        self._guardar(self._form(hora="22:00", cancha="KINERET"))  # y de noche otra zona

    def test_cancha_inexistente_se_rechaza(self):
        with self.assertRaises(ValueError):
            self._guardar(self._form(cancha="99"))

    def test_exige_3_jugadores(self):
        with self.assertRaises(ValueError):
            self._guardar(self._form(jug=["253510", "130962"]))

    def test_jugadores_duplicados_se_rechazan(self):
        with self.assertRaises(ValueError):      # quedan 2 distintos
            self._guardar(self._form(jug=["253510", "253510", "130962"]))

    def test_id_distingue_reservadores_con_mismo_nombre_de_pila(self):
        """M2 vieja: dos 'Dario' distintos generaban el mismo id y se pisaban."""
        a = self.web._slug("Dario Waingarten")
        b = self.web._slug("Dario Zelione")
        self.assertNotEqual(a, b)


# ----------------------------------------------------------------------
#  7. Permisos y escape de HTML
# ----------------------------------------------------------------------
class TestSeguridad(BaseApp):
    def test_sin_login_redirige(self):
        c = self.web.app.test_client()
        self.assertEqual(c.get("/reservas").status_code, 302)

    def test_no_admin_no_entra_a_usuarios(self):
        c = self.cliente("pablo", "clave2")
        r = c.get("/usuarios", follow_redirects=False)
        self.assertEqual(r.status_code, 302)

    def test_admin_entra_a_usuarios(self):
        self.assertEqual(self.cliente().get("/usuarios").status_code, 200)

    def test_escape_evita_inyeccion_de_html(self):
        self.assertEqual(str(self.web.esc('<script>alert(1)</script>')),
                         "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_limpiar_nombre_saca_tags_y_recorta(self):
        self.assertEqual(self.web.limpiar_nombre("<b>Juan</b>"), "bJuan/b")
        self.assertEqual(len(self.web.limpiar_nombre("x" * 200)), 60)

    def test_entrar_bien_no_gasta_el_cupo_de_intentos(self):
        """Si los logins exitosos contaran, varios usuarios en la misma WiFi se
        bloquearían entre ellos."""
        self.web._rl_hits.clear()
        for _ in range(15):                       # muchos logins CORRECTOS seguidos
            c = self.web.app.test_client()
            c.post("/login", data={"usuario": "diego", "password": "clave1"})
        self.assertEqual(self.cliente().get("/usuarios").status_code, 200)

    def test_intentos_fallidos_terminan_bloqueando(self):
        self.web._rl_hits.clear()
        c = self.web.app.test_client()
        for _ in range(12):
            c.post("/login", data={"usuario": "diego", "password": "MAL"})
        r = c.post("/login", data={"usuario": "diego", "password": "clave1"}, follow_redirects=True)
        self.assertIn("Demasiados intentos", r.get_data(as_text=True))
        self.web._rl_hits.clear()

    def test_salud_no_expone_datos_sensibles(self):
        cuerpo = self.web.app.test_client().get("/salud").get_data(as_text=True)
        for palabra in ("clave", "password", "usuario@", "secreta"):
            self.assertNotIn(palabra, cuerpo.lower())


# ----------------------------------------------------------------------
#  8. Historial, backup y notificaciones
# ----------------------------------------------------------------------
class TestHistorialYBackup(BaseApp):
    def test_backup_copia_los_json_y_no_repite(self):
        self.assertTrue(self.web._backup_diario())          # primera vez: lo hace
        dia = self.core.hoy_ar().isoformat()
        copiados = os.listdir(os.path.join(self.web.BACKUP_DIR, dia))
        self.assertIn("reservas.json", copiados)
        self.assertIn("db_reservadores.json", copiados)
        self.assertFalse(self.web._backup_diario())         # segunda vez del día: no repite

    def test_historial_guarda_y_no_duplica(self):
        job = {"id": "h-test", "fecha": self.dia, "hora_desde": "09:00", "club_id": 1102,
               "cancha": "3", "reservador": "Diego Fride", "jugadores_full": []}
        self.web._al_historial(job, "reservada", "03-QUINTA", "ok", "Diego")
        self.web._al_historial(job, "reservada", "03-QUINTA", "ok", "Diego")   # repetida
        regs = self.rb.leer_json("historial.json", {"historial": []})["historial"]
        self.assertEqual(sum(1 for r in regs if r["id"] == "h-test"), 1)

    def test_historial_queda_el_ultimo_resultado(self):
        # caso real (Darío 27/9): "Reservar ya" antes de la apertura -> fallada; se reprograma
        # y el reloj la gana a la apertura -> el historial tiene que decir reservada
        job = {"id": "h-ultimo", "fecha": self.dia, "hora_desde": "09:00", "club_id": 1102,
               "cancha": "3", "reservador": "Dario Waingarten", "jugadores_full": []}
        self.web._al_historial(job, "fallada", "", "RECHAZO -> todavía no abre", "The Boss")
        self.web._al_historial(job, "reservada", "03-QUINTA", "", "The Boss")
        regs = [r for r in self.rb.leer_json("historial.json", {"historial": []})["historial"]
                if r["id"] == "h-ultimo"]
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0]["estado"], "reservada")
        self.assertEqual(regs[0]["cancha_real"], "03-QUINTA")

    def test_cada_usuario_ve_solo_su_historial(self):
        job = {"id": "h-diego", "fecha": self.dia, "hora_desde": "10:00", "club_id": 1102,
               "cancha": "4", "reservador": "Diego Fride", "jugadores_full": []}
        self.web._al_historial(job, "reservada", "04-QUINTA", "", "Diego")
        propio = self.cliente("pablo", "clave2").get("/historial").get_data(as_text=True)
        self.assertNotIn("h-diego", propio)
        admin = self.cliente().get("/historial").get_data(as_text=True)
        self.assertEqual(admin.count('<li class="reserva"'),
                         len(self.rb.leer_json("historial.json", {"historial": []})["historial"]))

    def test_notificar_sin_suscripciones_no_rompe(self):
        self.assertEqual(self.web.notificar("Diego", "t", "c"), 0)

    def test_notificar_sin_usuario_no_hace_nada(self):
        self.assertEqual(self.web.notificar("", "t", "c"), 0)


# ----------------------------------------------------------------------
#  9. Roles y permisos
# ----------------------------------------------------------------------
class TestRoles(BaseApp):
    def test_formato_viejo_se_lee_como_rol(self):
        """usuarios.json de antes tenía admin: true/false, sin 'rol'."""
        self.assertEqual(self.web.rol_de({"admin": True}), "admin")
        self.assertEqual(self.web.rol_de({"admin": False}), "jugador")
        self.assertEqual(self.web.rol_de({"rol": "operador"}), "operador")

    def test_rol_desconocido_cae_al_mas_restrictivo(self):
        self.assertEqual(self.web.rol_de({"rol": "inventado"}), "jugador")

    def test_solo_admin_entra_al_panel_de_usuarios(self):
        for usuario, clave, entra in (("diego", "clave1", True),
                                      ("oper", "clave3", False),
                                      ("pablo", "clave2", False)):
            st = self.cliente(usuario, clave).get("/usuarios", follow_redirects=False).status_code
            self.assertEqual(st == 200, entra, f"falló para {usuario}")

    def test_operador_y_admin_gestionan_las_bases(self):
        """gestionar_bases: borrar jugadores. El rol 'jugador' no puede."""
        def hay(pid):
            return any(str(j.get("player_id")) == str(pid)
                       for j in self.rb._cargar("db_jugadores.json", "jugadores"))

        def reponer():
            with self.rb._io_lock:
                lista = self.rb._cargar("db_jugadores.json", "jugadores")
                if not hay(253510):
                    lista.append({"nombre": "Pablo Becker", "player_id": 253510})
                    self.web.guardar("db_jugadores.json", "jugadores", lista)

        for usuario, clave, borra in (("pablo", "clave2", False),
                                      ("oper", "clave3", True),
                                      ("diego", "clave1", True)):
            reponer()
            self.cliente(usuario, clave).post("/jugadores/borrar/253510", follow_redirects=True)
            self.assertEqual(not hay(253510), borra, f"falló para {usuario}")
        reponer()

    def test_permisos_editables_cambian_el_comportamiento(self):
        """Darle un permiso a un rol desde el panel tiene efecto real, no es solo la tabla."""
        def hay(pid):
            return any(str(j.get("player_id")) == str(pid)
                       for j in self.rb._cargar("db_jugadores.json", "jugadores"))
        with self.rb._io_lock:                       # jugador de prueba
            lista = self.rb._cargar("db_jugadores.json", "jugadores")
            if not hay(999):
                lista.append({"nombre": "Descartable", "player_id": 999})
                self.web.guardar("db_jugadores.json", "jugadores", lista)

        self.cliente("pablo", "clave2").post("/jugadores/borrar/999", follow_redirects=True)
        self.assertTrue(hay(999), "el rol 'jugador' no debería poder borrar todavía")

        datos = {"p:admin:usuarios": "on", "p:jugador:gestionar_bases": "on"}
        self.cliente().post("/usuarios/permisos", data=datos, follow_redirects=True)
        try:
            self.cliente("pablo", "clave2").post("/jugadores/borrar/999", follow_redirects=True)
            self.assertFalse(hay(999), "con el permiso nuevo sí debería poder borrar")
        finally:
            self.cliente().post("/usuarios/permisos/restaurar", follow_redirects=True)

    def test_no_se_puede_quitar_el_acceso_al_panel(self):
        """Candado: si el admin se sacara 'usuarios', nadie podría volver a entrar."""
        self.cliente().post("/usuarios/permisos", data={"p:operador:todas_reservas": "on"},
                            follow_redirects=True)
        try:
            self.assertIn("usuarios", self.web.ROLES["admin"]["permisos"])
            self.assertEqual(self.cliente().get("/usuarios").status_code, 200)
        finally:
            self.cliente().post("/usuarios/permisos/restaurar", follow_redirects=True)

    def test_solo_el_admin_edita_los_permisos(self):
        for usuario, clave in (("pablo", "clave2"), ("oper", "clave3")):
            r = self.cliente(usuario, clave).post("/usuarios/permisos",
                                                  data={"p:jugador:usuarios": "on"},
                                                  follow_redirects=True)
            self.assertIn("No tenés permiso", r.get_data(as_text=True))
        self.assertNotIn("usuarios", self.web.ROLES["jugador"]["permisos"])

    def test_editar_reservador_exige_permiso(self):
        """Editar un reservador cambia su USUARIO Y CLAVE de OnDepor: no puede hacerlo
        cualquiera que tenga una cuenta del panel."""
        antes = self.rb._cargar("db_reservadores.json", "reservadores")[0]
        self.cliente("pablo", "clave2").post("/reservadores/guardar", data={
            "orig": antes.get("usuario", ""), "nombre": "Pisado",
            "usuario": antes.get("usuario", ""), "clave": "OTRA"}, follow_redirects=True)
        despues = self.rb._cargar("db_reservadores.json", "reservadores")[0]
        self.assertEqual(despues.get("clave"), antes.get("clave"))
        self.assertEqual(despues.get("nombre"), antes.get("nombre"))

    def test_permiso_manda_sobre_el_rol(self):
        """La tabla de permisos promete que el Operador puede cambiar jugadores en
        OnDepor: la ruta tiene que respetar el PERMISO, no pedir rol admin."""
        r = self.cliente("oper", "clave3").post(
            "/reservas/cambiar-jugadores", data={"cambiar_id": "x", "jug": ["1", "2", "3"]},
            follow_redirects=True)
        self.assertNotIn("No tenés permiso", r.get_data(as_text=True))

    def test_jugador_programa_cambios_solo_de_sus_reservas(self):
        """Pedido de Diego: el rol Jugador puede programar el cambio de carnets de
        SUS reservas (las que creó), pero no de las ajenas."""
        manana = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        base = {"club_id": 1102, "cancha": "", "hora_hasta": "16:00", "single_double": 2,
                "reservador": "Diego Fride", "jugadores": [253510, 130962, 304064]}
        with open(os.path.join(self.tmp, "reservas.json"), "w", encoding="utf-8") as f:
            json.dump({"reservas": [
                dict(base, id="mia", fecha=manana, hora_desde="15:00", creado_por="Pablo"),
                dict(base, id="ajena", fecha=manana, hora_desde="16:00", creado_por="Diego"),
            ]}, f)
        with self.web._estados_lock:
            self.web._estados["mia"] = {"estado": "reservada"}
            self.web._estados["ajena"] = {"estado": "reservada"}
        try:
            c = self.cliente("pablo", "clave2")           # rol jugador
            datos = {"reservador": "Diego Fride", "min_antes": "30",
                     "jug": ["253510", "130962", "304064"]}
            c.post("/reservas/programar-cambio",
                   data=dict(datos, programar_id="mia"), follow_redirects=True)
            r = c.post("/reservas/programar-cambio",
                       data=dict(datos, programar_id="ajena"), follow_redirects=True)
            reservas = {x["id"]: x for x in self.web.cargar("reservas.json", "reservas")}
            self.assertTrue(reservas["mia"].get("cambio_prog"),
                            "la suya tenía que quedar programada")
            self.assertFalse(reservas["ajena"].get("cambio_prog"),
                             "la ajena no la puede tocar")
            self.assertIn("quien creó la reserva", r.get_data(as_text=True))
            # y también puede anular el cambio de la suya
            c.post("/reservas/cambio-prog-borrar/mia", follow_redirects=True)
            reservas = {x["id"]: x for x in self.web.cargar("reservas.json", "reservas")}
            self.assertFalse(reservas["mia"].get("cambio_prog"))
        finally:
            with self.web._estados_lock:
                self.web._estados.pop("mia", None)
                self.web._estados.pop("ajena", None)

    def test_encuesta_solo_con_permiso(self):
        import encuesta as enc
        pid = enc.todos_los_jugadores()[0]["id"]
        self.cliente("pablo", "clave2").post("/encuesta/abrir", data={"p": [pid]},
                                             follow_redirects=True)
        self.assertFalse(enc.cargar_encuesta(), "un 'jugador' no debería poder abrirla")
        self.cliente("oper", "clave3").post("/encuesta/abrir", data={"p": [pid]},
                                            follow_redirects=True)
        self.assertTrue(enc.cargar_encuesta(), "un 'operador' sí debería poder")

    def test_probar_login_exige_permiso(self):
        """Usa las credenciales de OnDepor de un reservador."""
        r = self.cliente("pablo", "clave2").get("/api/probar_login?i=0", follow_redirects=True)
        self.assertIn("No tenés permiso", r.get_data(as_text=True))

    def test_no_se_muestran_acciones_sin_permiso(self):
        html = self.cliente("pablo", "clave2").get("/reservadores").get_data(as_text=True)
        self.assertNotIn("reservador_guardar", html)
        self.assertNotIn("?editar=", html)

    def test_jugador_agrega_pero_no_borra(self):
        """Agregar un jugador es inofensivo (lo puede hacer cualquiera); borrar no."""
        def hay(pid):
            return any(str(j.get("player_id")) == str(pid)
                       for j in self.rb._cargar("db_jugadores.json", "jugadores"))

        c = self.cliente("pablo", "clave2")          # rol 'jugador'
        c.post("/jugadores/agregar", json={"id": 7777, "nombre": "Sumado Por Jugador"})
        self.assertTrue(hay(7777), "el rol 'jugador' debería poder agregar")

        c.post("/jugadores/borrar/7777", follow_redirects=True)
        self.assertTrue(hay(7777), "el rol 'jugador' NO debería poder borrar")

        self.cliente().post("/jugadores/borrar/7777", follow_redirects=True)   # admin sí
        self.assertFalse(hay(7777))

    def test_nunca_se_queda_sin_administrador(self):
        us = [{"usuario": "a", "rol": "jugador"}, {"usuario": "b", "rol": "operador"}]
        self.assertFalse(any(self.web.rol_de(u) == "admin" for u in us))   # el guard del guardado lo arregla


# ----------------------------------------------------------------------
#  10. Reservas fijas (recurrentes)
# ----------------------------------------------------------------------
class TestRecurrentes(BaseApp):
    def _crear(self, dia_semana):
        return self.cliente().post("/recurrentes/guardar", data={
            "sede": "dia", "dia_semana": str(dia_semana), "hora": "09:00",
            "reservador": "Diego Fride", "cancha": "3",
            "jug": ["253510", "130962", "304064"]}, follow_redirects=True)

    def test_se_carga_sola_y_no_duplica(self):
        hoy = self.core.hoy_ar()
        self._crear((hoy.weekday() + 1) % 7)          # mañana: entra en el anticipo
        self.web._generar_recurrentes()
        n1 = len(self.rb._cargar("reservas.json", "reservas"))
        self.web._generar_recurrentes()               # segunda corrida: no debe duplicar
        n2 = len(self.rb._cargar("reservas.json", "reservas"))
        self.assertEqual(n1, n2)
        self.assertTrue(any(r.get("de_recurrente")
                            for r in self.rb._cargar("reservas.json", "reservas")))

    def test_no_carga_las_lejanas(self):
        antes = len(self.rb._cargar("reservas.json", "reservas"))
        self._crear((self.core.hoy_ar().weekday() + 5) % 7)   # 5 días: fuera del anticipo
        self.web._generar_recurrentes()
        self.assertEqual(len(self.rb._cargar("reservas.json", "reservas")), antes)

    def test_exige_3_jugadores(self):
        r = self.cliente().post("/recurrentes/guardar", data={
            "sede": "dia", "dia_semana": "1", "hora": "09:00",
            "reservador": "Diego Fride", "cancha": "3", "jug": ["253510"]},
            follow_redirects=True)
        self.assertIn("3 jugadores", r.get_data(as_text=True))

    def test_pausada_no_genera(self):
        self._crear((self.core.hoy_ar().weekday() + 1) % 7)
        with self.rb._io_lock:
            data = self.rb.leer_json("recurrentes.json", {"recurrentes": []})
            for r in data["recurrentes"]:
                r["activa"] = False
            self.rb.escribir_json("recurrentes.json", data)
        with self.rb._io_lock:
            self.rb.escribir_json("reservas.json", {"reservas": []})
        self.web._generar_recurrentes()
        self.assertEqual(self.rb._cargar("reservas.json", "reservas"), [])


# ----------------------------------------------------------------------
#  11. Encuesta semanal
# ----------------------------------------------------------------------
class TestEncuesta(BaseApp):
    def _abrir(self, participantes):
        return self.cliente().post("/encuesta/abrir", data={"p": participantes},
                                   follow_redirects=True)

    def test_solo_votan_los_elegidos(self):
        import encuesta as enc
        todos = enc.todos_los_jugadores()
        self.assertGreater(len(todos), 2, "hacen falta al menos 3 en la base")
        invitados = [todos[0]["id"], todos[1]["id"]]
        afuera = todos[2]["id"]
        self._abrir(invitados)
        e = enc.cargar_encuesta()
        self.assertEqual(set(e["participantes"]), set(invitados))

        c = self.web.app.test_client()
        html = c.get(f"/e/{e['token']}").get_data(as_text=True)
        self.assertIn(todos[0]["nombre"], html)
        self.assertNotIn(f"/soy/{afuera}", html, "muestra a alguien que no participa")

        # el no invitado no puede identificarse
        c.get(f"/e/{e['token']}/soy/{afuera}", follow_redirects=True)
        self.assertIn("¿Quién sos?", c.get(f"/e/{e['token']}").get_data(as_text=True))

    def test_se_puede_sumar_gente_despues(self):
        import encuesta as enc
        todos = enc.todos_los_jugadores()
        self._abrir([todos[0]["id"]])
        nuevo = todos[1]["id"]
        self.cliente().post("/encuesta/participantes",
                            data={"p": [todos[0]["id"], nuevo]}, follow_redirects=True)
        e = enc.cargar_encuesta()
        self.assertIn(nuevo, e["participantes"])
        c = self.web.app.test_client()
        c.get(f"/e/{e['token']}/soy/{nuevo}", follow_redirects=True)
        self.assertIn("Hola,", c.get(f"/e/{e['token']}").get_data(as_text=True))

    def test_borrar_la_encuesta(self):
        """Borrarla se lleva los votos y mata el link. Solo con permiso."""
        import encuesta as enc
        todos = enc.todos_los_jugadores()
        pid = todos[0]["id"]
        self._abrir([pid])
        e = enc.cargar_encuesta()
        c = self.web.app.test_client()
        c.get(f"/e/{e['token']}/soy/{pid}", follow_redirects=True)
        c.post(f"/e/{e['token']}/votar", data={"m5": "juego", "h5": ["10:00"], "m6": "no"})
        self.assertEqual(len(enc.votos_de(e["id"])), 1)

        # sin permiso no se borra
        self.cliente("pablo", "clave2").post("/encuesta/borrar", follow_redirects=True)
        self.assertTrue(enc.cargar_encuesta(), "un 'jugador' no debería poder borrarla")

        self.cliente().post("/encuesta/borrar", follow_redirects=True)
        self.assertFalse(enc.cargar_encuesta())
        self.assertEqual(enc.votos_de(e["id"]), {}, "tenía que llevarse los votos")
        html = self.web.app.test_client().get(f"/e/{e['token']}").get_data(as_text=True)
        self.assertIn("Link no válido", html, "el link viejo debería morir")

    def test_no_se_abre_sin_participantes(self):
        import encuesta as enc
        r = self.cliente().post("/encuesta/abrir", data={}, follow_redirects=True)
        self.assertIn("al menos una persona", r.get_data(as_text=True))

    def test_voto_una_decision_por_dia(self):
        import encuesta as enc
        todos = enc.todos_los_jugadores()
        pid = todos[0]["id"]
        self._abrir([pid])
        e = enc.cargar_encuesta()
        c = self.web.app.test_client()
        c.get(f"/e/{e['token']}/soy/{pid}", follow_redirects=True)
        # no juega ningún día, pero presta el carnet el sábado
        c.post(f"/e/{e['token']}/votar",
               data={"m5": enc.MODO_PRESTA, "m6": enc.MODO_NO}, follow_redirects=True)
        v = enc.votos_de(e["id"])[pid]
        self.assertEqual(v["dias"]["5"]["modo"], enc.MODO_PRESTA)
        self.assertEqual(v["dias"]["6"]["modo"], enc.MODO_NO)

    def test_el_voto_vale_solo_completo(self):
        """Pedido de Diego: para votar hay que contestar TODOS los días (sábado Y
        domingo), aunque sea 'no cuenten conmigo'. Un voto a medias no se guarda."""
        import encuesta as enc
        todos = enc.todos_los_jugadores()
        pid = todos[0]["id"]
        self._abrir([pid])
        e = enc.cargar_encuesta()
        c = self.web.app.test_client()
        c.get(f"/e/{e['token']}/soy/{pid}", follow_redirects=True)

        # solo contesta el sábado: NO se guarda y vuelve con el aviso
        r = c.post(f"/e/{e['token']}/votar", data={"m5": enc.MODO_PRESTA},
                   follow_redirects=True)
        self.assertNotIn(pid, enc.votos_de(e["id"]))
        self.assertIn("no se guardó", r.get_data(as_text=True))

        # dice "juego" el domingo pero sin marcar horario: tampoco
        c.post(f"/e/{e['token']}/votar",
               data={"m5": enc.MODO_NO, "m6": enc.MODO_JUEGO}, follow_redirects=True)
        self.assertNotIn(pid, enc.votos_de(e["id"]))

        # completo: ahora sí
        c.post(f"/e/{e['token']}/votar",
               data={"m5": enc.MODO_NO, "m6": enc.MODO_JUEGO, "h6": ["10:00"]},
               follow_redirects=True)
        self.assertIn(pid, enc.votos_de(e["id"]))

    def test_bolsa_de_carnets_junta_las_tres_fuentes(self):
        """La bolsa = los que juegan + los que prestan en la encuesta + los prestados
        por gente de AFUERA. Sin duplicar a quien está en más de una."""
        import encuesta as enc
        todos = enc.todos_los_jugadores()
        if len(todos) < 4:
            self.skipTest("hacen falta al menos 4 en la base")
        a, b, c, afuera = todos[0]["id"], todos[1]["id"], todos[2]["id"], todos[3]["id"]
        self._abrir([a, b, c])
        e = enc.cargar_encuesta()

        def votar(pid, modo, horas=None):
            cl = self.web.app.test_client()
            cl.get(f"/e/{e['token']}/soy/{pid}")
            datos = {"m5": modo, "m6": enc.MODO_NO}
            if horas:
                datos["h5"] = horas
            cl.post(f"/e/{e['token']}/votar", data=datos)

        votar(a, enc.MODO_JUEGO, ["10:00"])   # juega -> su carnet cuenta
        votar(b, enc.MODO_PRESTA)             # presta sin jugar
        votar(c, enc.MODO_NO)                 # no cuenta

        sab = enc.resumen_votos(enc.cargar_encuesta())["por_dia"][5]
        self.assertEqual(len(sab["bolsa"]), 2, "deberían ser solo a y b")
        self.assertNotIn(c, sab["bolsa"])

        # sumo uno de afuera de la encuesta
        self.cliente().post("/encuesta/carnets", data={"p": [afuera]}, follow_redirects=True)
        sab = enc.resumen_votos(enc.cargar_encuesta())["por_dia"][5]
        self.assertEqual(len(sab["bolsa"]), 3)
        self.assertIn(afuera, sab["bolsa"])
        # el desglose suma exacto
        self.assertEqual(len(sab["juegan"]) + len(sab["prestan"]) + len(sab["extra"]),
                         len(sab["bolsa"]))

    def test_sumar_carnets_ofrece_a_todos(self):
        """Antes escondía a los de la encuesta, y así el que participa pero NO votó
        quedaba fuera de la bolsa sin manera de agregarlo (Diego, 08/08/2026)."""
        import re
        import encuesta as enc
        todos = enc.todos_los_jugadores()
        dentro = [todos[0]["id"], todos[1]["id"]]
        self._abrir(dentro)
        html = self.cliente().get("/encuesta/carnets").get_data(as_text=True)
        ofrecidos = re.findall(r"name='p' value='(\d+)'", html)
        self.assertEqual(sorted(ofrecidos), sorted(j["id"] for j in todos))

    def test_canchas_segun_la_regla_del_grupo(self):
        import encuesta as enc
        for n, esperado in ((3, 0), (4, 1), (7, 1), (8, 2), (11, 2), (12, 3), (20, 3)):
            self.assertEqual(enc.canchas_para(n), esperado, f"con {n} jugadores")


# ----------------------------------------------------------------------
#  14. Armador de cronograma (las reglas están en ARMADOR_CRONOGRAMA.md)
# ----------------------------------------------------------------------
NOMBRES_TEST = ["Ana", "Beto", "Carla", "Dario", "Elena", "Fabio", "Gaby", "Hugo",
                "Ivan", "Jonas", "Karin", "Leo", "Marta", "Nico"]
CON_CUENTA = ("Ana", "Dario", "Hugo", "Leo")     # los que pueden disparar la reserva


class TestCronograma(BaseApp):
    """El armador convierte los votos en el flujo de reservas del finde entero
    (modelo de Darío/Diego, ver ARMADOR_CRONOGRAMA.md). Lo que se prueba acá son
    las reglas duras: un carnet nunca en dos reservas que se pisen, las canchas se
    pisan con prestados, y los cambios corren cuando los que entran se liberan."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pid = {n: 900 + i for i, n in enumerate(NOMBRES_TEST)}
        cls._W("db_jugadores.json", {"jugadores": [
            {"nombre": n, "player_id": cls.pid[n]} for n in NOMBRES_TEST]})
        cls._W("db_reservadores.json", {"reservadores": [
            {"nombre": n, "usuario": f"{n.lower()}@x.com", "clave": "secreta",
             "player_id": cls.pid[n]} for n in CON_CUENTA]})
        cls.sab = (datetime.date.today() + datetime.timedelta(days=9)).isoformat()
        cls.dom = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()

    @classmethod
    def _W(cls, nombre, obj):
        with open(os.path.join(cls.tmp, nombre), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    def _encuesta(self, votos, extra=()):
        import encuesta as enc
        # el armador respeta las reservas YA cargadas: cada escenario arranca limpio
        self._W("reservas.json", {"reservas": []})
        e = {"id": "enc-test", "token": "tok", "dias": [5, 6], "horas": enc.HORAS_POR_DEFECTO,
             "fechas": {"5": self.sab, "6": self.dom}, "estado": "abierta",
             "participantes": [str(self.pid[n]) for n in NOMBRES_TEST],
             "carnets_extra": [str(self.pid[n]) for n in extra]}
        self._W("encuesta_config.json", e)
        self._W("votos.json", {"votos": {"enc-test": {
            str(self.pid[n]): {"nombre": n, "dias": d, "cuando": ""}
            for n, d in votos.items()}}})
        return e

    @staticmethod
    def _sin_errores_de_verificacion(plan):
        return not any("VERIFICACIÓN" in a for r in plan["reservas"] for a in r["avisos"])

    # -- fijar un horario a mano ("quiero 2 a las 16") tiene que MANDAR --
    def _finde_parejo(self):
        """8 juegan sábado a las 15, 16 y 17; el resto presta el carnet."""
        import encuesta as enc
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00", "16:00", "17:00"]}}
                 for n in NOMBRES_TEST[:8]}
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}} for n in NOMBRES_TEST[8:]})
        return votos

    def test_lo_que_fijas_se_cumple_aunque_de_menos_canchas(self):
        """Antes ganaba el plan con más canchas aunque ignorara lo que pediste, y encima
        avisaba 'no alcanzan los carnets libres a esa hora' — que era falso."""
        import encuesta as enc
        e = self._encuesta(self._finde_parejo())
        plan = enc.armar_cronograma(e, fijas={(5, "16:00"): 2})
        self.assertEqual(plan["armadas"][(5, "16:00")], 2)
        self.assertFalse([a for a in plan["avisos"] if "no alcanzan" in a],
                         "avisó que faltaban carnets y sí entraban")

    def test_cuando_no_entra_dice_el_motivo_de_verdad(self):
        """Decía "no alcanzan los carnets libres a esa hora" con la bolsa llena de
        carnets sin usar. El motivo real es otro: la gente de ESE horario tiene el
        carnet en el turno anterior y se le libera con el turno ya empezado
        (Diego: "cómo no quedan carnets libres si hay un montón sin usar")."""
        import encuesta as enc
        # 8 juegan a las 15 y a las 16: si se arma la de las 15, sus carnets quedan
        # tomados hasta las 16:00 en punto y no llegan a la de las 16
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00", "16:00"]}}
                 for n in NOMBRES_TEST[:8]}
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}} for n in NOMBRES_TEST[8:]})
        plan = enc.armar_cronograma(self._encuesta(votos),
                                    fijas={(5, "15:00"): 2, (5, "16:00"): 2})
        fallo = [a for a in plan["avisos"] if "16 h" in a and "no" in a]
        self.assertTrue(fallo, "no avisó que las de las 16 no entran")
        self.assertNotIn("no alcanzan los carnets libres", fallo[0])
        self.assertIn("pueden ir en la reserva", fallo[0])

    def test_si_pediste_2_y_entra_1_arma_1(self):
        """Antes el día 2 probaba solo el número exacto: pedías 2, entraba 1, y armaba
        CERO (Diego: "si pedí 2 canchas y sale 1 sola, ¿por qué no la hacés?")."""
        import encuesta as enc
        # domingo a las 16 votan 5: da para 1 cancha, nunca para 2
        votos = {n: {"6": {"modo": enc.MODO_JUEGO, "horas": ["16:00"]}}
                 for n in NOMBRES_TEST[:5]}
        votos.update({n: {"6": {"modo": enc.MODO_PRESTA}} for n in NOMBRES_TEST[5:]})
        plan = enc.armar_cronograma(self._encuesta(votos), fijas={(6, "16:00"): 2})
        self.assertEqual(plan["armadas"][(6, "16:00")], 1, "pidió 2, entraba 1, armó 0")
        self.assertTrue([a for a in plan["avisos"] if "entra 1" in a],
                        "no avisó que de las 2 entró solo 1")

    def test_rotar_tres_ya_no_es_un_aviso(self):
        """Regla de Diego: rotando afuera se banca hasta 3 si no hay cómo abrir otra."""
        import encuesta as enc
        self.assertEqual(enc.MAX_ROTAN, 3)
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]}}
                 for n in NOMBRES_TEST[:7]}          # 4 juegan, 3 rotan
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}} for n in NOMBRES_TEST[7:]})
        plan = enc.armar_cronograma(self._encuesta(votos))
        self.assertFalse([a for a in plan["avisos"] if "rotan" in a])

    def test_fijar_en_cero_deja_ese_horario_vacio(self):
        import encuesta as enc
        e = self._encuesta(self._finde_parejo())
        plan = enc.armar_cronograma(e, fijas={(5, "15:00"): 0, (5, "16:00"): 1})
        self.assertEqual(plan["armadas"][(5, "15:00")], 0)
        self.assertEqual(plan["armadas"][(5, "16:00")], 1)

    def test_los_horarios_fijados_se_arman_primero(self):
        """Yendo en orden cronológico los turnos de antes se comen los carnets y el que
        pediste llega sin nada (medido con el domingo 16 hs del finde del 15/08)."""
        import encuesta as enc
        e = self._encuesta(self._finde_parejo())
        r = enc.resumen_votos(e)
        a = enc._Armador(e, r, enc.reservadores_con_carnet()[0], {(5, "17:00"): 1}, False)
        self.assertEqual([h for h, _, _ in a._orden_turnos(5)][0], "17:00")

    # -- sumar carnets: cualquiera, esté o no en la encuesta (pedido de Diego 08/08) --
    def test_se_puede_sumar_el_carnet_del_que_no_voto(self):
        """El caso que quedaba trabado: está en la encuesta pero no contestó, así que su
        carnet no aparecía en ninguna lista y no había forma de meterlo en la bolsa."""
        import encuesta as enc
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]}}
                 for n in NOMBRES_TEST[:4]}                 # los demás NO votaron
        mudo = NOMBRES_TEST[5]
        r = enc.resumen_votos(self._encuesta(votos, extra=[mudo]))
        bolsa = r["por_dia"][5]["bolsa"]
        self.assertIn(str(self.pid[mudo]), bolsa)
        self.assertIn(str(self.pid[mudo]), r["por_dia"][6]["bolsa"])   # sirve los dos días

    def test_el_carnet_sumado_no_se_usa_el_dia_que_esa_persona_juega(self):
        """Su carnet lo necesita para su propia cancha: pisarla con él lo deja afuera."""
        import encuesta as enc
        juega = NOMBRES_TEST[0]
        votos = {juega: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]},
                         "6": {"modo": enc.MODO_NO}}}
        r = enc.resumen_votos(self._encuesta(votos, extra=[juega]))
        self.assertNotIn(str(self.pid[juega]), r["por_dia"][5]["extra"])   # sábado juega
        self.assertIn(str(self.pid[juega]), r["por_dia"][6]["extra"])      # domingo no

    def test_el_carnet_sumado_sigue_respetando_los_bloqueados(self):
        import encuesta as enc
        n = NOMBRES_TEST[7]
        e = self._encuesta({}, extra=[n])
        e["carnets_bloqueados"] = [str(self.pid[n])]
        self._W("encuesta_config.json", e)
        r = enc.resumen_votos(e)
        self.assertNotIn(str(self.pid[n]), r["por_dia"][5]["bolsa"])

    def test_los_prestados_se_guardan_para_donde_hacen_falta(self):
        """REGLA (medida, no intuida): el carnet prestado se usa donde el jugador tiene
        el suyo TOMADO. Gastarlo en alguien que ya lo tiene libre quema capacidad — ese
        prestado queda tomado desde el día anterior y no sirve para otra cancha.
        Con los votos reales, pisar de más baja de 6 canchas a 5."""
        import encuesta as enc
        # 8 juegan a las 15 y los mismos 8 a las 17: los de las 17 tienen el carnet
        # tomado por las 15, así que ESA es la cancha que hay que pisar con prestados
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00", "17:00"]}}
                 for n in NOMBRES_TEST[:8]}
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}} for n in NOMBRES_TEST[8:]})
        plan = enc.armar_cronograma(self._encuesta(votos))
        prestan = {str(self.pid[n]) for n in NOMBRES_TEST[8:]}
        for r in plan["reservas"]:
            pisan = {x["id"] for x in r["titulares"] if x["pisa"]}
            self.assertTrue(pisan <= prestan, "pisó con el carnet de alguien que juega")
            if r["hora"] == "15:00":
                self.assertFalse(pisan, "a las 15 nadie tiene el carnet tomado todavía")
            if r["hora"] == "17:00":
                self.assertTrue(pisan, "a las 17 hay que pisar: jugaron a las 15")
        self.assertTrue(self._sin_errores_de_verificacion(plan))

    def test_maniana_y_tarde_del_mismo_dia_no_comparten_carnets(self):
        """Todo el sábado se reserva el viernes: si la tarde ya usó el carnet de
        alguien, ese carnet no puede aparecer también en la mañana."""
        import encuesta as enc
        # 4 juegan SOLO a la mañana y 8 SOLO a la tarde: no hay conflicto y salen las dos
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["09:00"]}} for n in NOMBRES_TEST[:4]}
        votos.update({n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]}}
                      for n in NOMBRES_TEST[4:12]})
        plan = enc.armar_cronograma(self._encuesta(votos))
        horas = sorted(r["hora"] for r in plan["reservas"])
        self.assertEqual(horas, ["09:00", "15:00", "15:00"])
        vistos = set()
        for r in plan["reservas"]:
            ids = {r["reservador"]["id"]} | {x["id"] for x in r["titulares"]}
            self.assertFalse(ids & vistos, "un carnet apareció en dos reservas del sábado")
            vistos |= ids
        self.assertTrue(self._sin_errores_de_verificacion(plan))

    def test_con_ocho_carnets_reparte_en_vez_de_amontonar(self):
        """8 carnets dan para 2 canchas en todo el sábado (se reservan todas el viernes).
        Entre ponerlas las dos a la misma hora o una a la mañana y otra a la tarde, gana
        REPARTIR: amontonar hace que cada uno juegue una sola vez y deja horas vacías."""
        import encuesta as enc
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["09:00", "15:00"]}}
                 for n in NOMBRES_TEST[:8]}
        plan = enc.armar_cronograma(self._encuesta(votos))
        self.assertEqual(len(plan["reservas"]), 2, "8 carnets dan 2 canchas")
        juegan = {x["id"] for r in plan["reservas"] for x in r["juegan"]}
        self.assertEqual(len(juegan), 8, "los 8 tienen que jugar")
        self.assertTrue(self._sin_errores_de_verificacion(plan))

    def test_cada_cancha_tiene_una_cuenta_entre_los_que_juegan(self):
        """Si al elegir los 4 no cae ninguno con cuenta de OnDepor, la cancha terminaba
        disparada por un carnet prestado que ocupaba un lugar (quedaba para 3). Se
        cambian los últimos por los que sí tienen cuenta."""
        import encuesta as enc
        # los que tienen cuenta votan MUCHOS horarios (el orden natural los manda al
        # fondo) y los que no tienen votan uno solo
        votos = {}
        for n in CON_CUENTA:
            votos[n] = {"5": {"modo": enc.MODO_JUEGO,
                              "horas": ["09:00", "10:00", "15:00", "16:00", "17:00"]}}
        for n in [x for x in NOMBRES_TEST if x not in CON_CUENTA][:6]:
            votos[n] = {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]}}
        plan = enc.armar_cronograma(self._encuesta(votos))
        for r in plan["reservas"]:
            self.assertEqual(len(r["juegan"]), 4,
                             f"{r['hora']} quedó para 3: no cayó ninguna cuenta entre los 4")

    def test_flujo_del_finde_domingo_pisado_con_comodines(self):
        """El corazón del modelo: el domingo a la mañana se reserva el sábado con
        carnets que estén libres TODO el sábado (los prestados), y el cambio corre
        cuando los que juegan terminan su turno del sábado — no 10 min antes."""
        import encuesta as enc
        # 8 juegan sáb 15 y dom 10; 4 prestan los dos días
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]},
                     "6": {"modo": enc.MODO_JUEGO, "horas": ["10:00"]}}
                 for n in NOMBRES_TEST[:8]}
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}, "6": {"modo": enc.MODO_PRESTA}}
                      for n in NOMBRES_TEST[8:12]})
        plan = enc.armar_cronograma(self._encuesta(votos))
        dom = [r for r in plan["reservas"] if r["fecha"] == self.dom]
        self.assertEqual(len(dom), 1, "dom mañana capada a 1 cancha (regla de Darío)")
        r = dom[0]
        prestan = {str(self.pid[n]) for n in NOMBRES_TEST[8:12]}
        self.assertTrue(all(x["pisa"] for x in r["titulares"]),
                        "el domingo se pisa entero con comodines")
        self.assertTrue({x["id"] for x in r["titulares"]} <= prestan)
        self.assertIsNotNone(r["cambio"])
        # el sáb 15:00 termina 16:00: el cambio va un rato después, no a las 9:50 del dom
        # el cambio va APENAS SE LIBERA el último que entra: los del sábado terminan a
        # las 16:00, así que cae el sábado a la tarde (eso libera el carnet del que pisó).
        # Lo que NO puede es caer apenas se reserva la cancha (ver test del piso).
        cambio = datetime.datetime.fromtimestamp(
            r["cambio"]["epoch"], datetime.timezone.utc) + datetime.timedelta(hours=-3)
        cambio = cambio.replace(tzinfo=None)
        fin_sab = datetime.datetime.fromisoformat(f"{self.sab}T16:00:00")
        self.assertEqual(cambio - fin_sab, datetime.timedelta(seconds=enc.MARGEN_CAMBIO_SEG),
                         "tenía que correr apenas se liberan los del sábado")
        juegan = {str(self.pid[n]) for n in NOMBRES_TEST[:8]}
        self.assertTrue({x["id"] for x in r["juegan"]} <= juegan,
                        "después del cambio, en la cancha están los que votaron esa hora")
        self.assertTrue(self._sin_errores_de_verificacion(plan))

    def test_sin_votante_con_cuenta_el_cambio_queda_a_mano(self):
        """Decisión de Diego: si ninguno de los que juegan tiene cuenta de OnDepor
        para retomar la reserva, no se programa el cambio: aviso y a mano."""
        import encuesta as enc
        sin_cuenta = [n for n in NOMBRES_TEST if n not in CON_CUENTA]
        # 4 sin cuenta juegan sáb y dom; un reservador juega SOLO el sáb (para que el
        # sáb se pueda disparar); presta Ana (con cuenta, para el disparo del dom) + 3
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]},
                     "6": {"modo": enc.MODO_JUEGO, "horas": ["10:00"]}}
                 for n in sin_cuenta[:4]}
        votos["Dario"] = {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]}}
        votos["Ana"] = {"5": {"modo": enc.MODO_PRESTA}, "6": {"modo": enc.MODO_PRESTA}}
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}, "6": {"modo": enc.MODO_PRESTA}}
                      for n in sin_cuenta[4:7]})
        plan = enc.armar_cronograma(self._encuesta(votos))
        dom = [r for r in plan["reservas"] if r["fecha"] == self.dom]
        self.assertEqual(len(dom), 1)
        self.assertIsNone(dom[0]["cambio"])
        self.assertTrue(any("A MANO" in a for a in dom[0]["avisos"]), dom[0]["avisos"])

    def test_programar_deja_reservas_y_cambios_con_su_hora(self):
        """El botón carga las reservas (cada una dispara a SU apertura, las del
        domingo recién el sábado) y cuelga el cambio con su ej_epoch propio."""
        import encuesta as enc
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]},
                     "6": {"modo": enc.MODO_JUEGO, "horas": ["10:00"]}}
                 for n in NOMBRES_TEST[:8]}
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}, "6": {"modo": enc.MODO_PRESTA}}
                      for n in NOMBRES_TEST[8:12]})
        self._encuesta(votos)
        self._W("reservas.json", {"reservas": []})
        r = self.cliente().post("/encuesta/cronograma/programar", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        reservas = self.web.cargar("reservas.json", "reservas")
        self.assertEqual(len(reservas), 3, "2 del sábado + 1 del domingo")
        dom = [x for x in reservas if x["fecha"] == self.dom]
        self.assertEqual(len(dom), 1)
        cp = dom[0].get("cambio_prog")
        self.assertTrue(cp, "el domingo tenía que quedar con su cambio colgado")
        self.assertEqual(cp["estado"], "pendiente")
        self.assertTrue(cp.get("ej_epoch"), "el cambio lleva su hora propia")
        self.assertEqual(len(cp["jugadores"]), 3)

    def test_el_ejecutor_respeta_la_hora_propia_del_cambio(self):
        """_revisar_acciones_prog tiene que disparar por ej_epoch cuando existe,
        no por el viejo default de turno - 10 min."""
        manana = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        base = {"id": "cp-test", "fecha": manana, "hora_desde": "15:00",
                "club_id": 1102, "reservador": "Ana", "jugadores": [1, 2, 3]}
        disparados = []
        original = self.web._correr_accion
        self.web._correr_accion = lambda campo, rid: disparados.append(rid)
        try:
            with self.web._estados_lock:
                self.web._estados["cp-test"] = {"estado": "reservada"}
            ahora = self.core.hora_real()
            # ej_epoch en el futuro: aunque falten <10 min para nada, NO dispara
            rsv = dict(base, cambio_prog={"estado": "pendiente", "ej_epoch": ahora + 900,
                                          "reservador": "Ana", "jugadores": [1, 2, 3]})
            self.web._revisar_acciones_prog([rsv])
            self.assertEqual(disparados, [])
            # ej_epoch ya pasado (y el turno no): dispara
            self._W("reservas.json", {"reservas": [dict(base, cambio_prog={
                "estado": "pendiente", "ej_epoch": ahora - 5,
                "reservador": "Ana", "jugadores": [1, 2, 3]})]})
            self.web._revisar_acciones_prog()
            self.assertEqual(disparados, ["cp-test"])
        finally:
            self.web._correr_accion = original
            with self.web._estados_lock:
                self.web._estados.pop("cp-test", None)

    def test_programar_cambio_manual_con_minutos_elegidos(self):
        """El form de 'Prog. cambio' deja elegir cuántos minutos antes del turno corre
        el cambio (antes eran 10 fijos): tiene que guardar su ej_epoch propio."""
        manana = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        self._W("reservas.json", {"reservas": [{
            "id": "mc-1", "club_id": 1102, "cancha": "", "fecha": manana,
            "hora_desde": "15:00", "hora_hasta": "16:00", "single_double": 2,
            "reservador": "Ana", "jugadores": [self.pid["Beto"], self.pid["Carla"],
                                               self.pid["Elena"]],
            "creado_por": "Diego"}]})
        with self.web._estados_lock:
            self.web._estados["mc-1"] = {"estado": "reservada"}
        try:
            r = self.cliente().post("/reservas/programar-cambio", data={
                "programar_id": "mc-1", "reservador": "Ana", "min_antes": "45",
                "jug": [str(self.pid["Fabio"]), str(self.pid["Gaby"]),
                        str(self.pid["Hugo"])]}, follow_redirects=True)
            self.assertEqual(r.status_code, 200)
            rsv = self.web.cargar("reservas.json", "reservas")[0]
            cp = rsv.get("cambio_prog")
            self.assertTrue(cp, "no quedó programado")
            turno = self.core._epoch_ar(
                datetime.datetime.fromisoformat(f"{manana}T15:00:00"))
            self.assertEqual(cp["ej_epoch"], turno - 45 * 60)
            self.assertEqual(cp["hora_ej"], "14:15")
        finally:
            with self.web._estados_lock:
                self.web._estados.pop("mc-1", None)

    def test_la_regla_del_carnet_es_una_reserva_activa(self):
        """REFERENCIA (cronograma que armó Diego a mano, sábado 08/08). La regla es UNA
        reserva activa por carnet: se libera cuando TERMINA el turno (o cuando un cambio
        lo saca antes) y se puede reusar EL MISMO DÍA. Su cadena real era:
          15:00 cancha 11 (Farji, Afh, Woslawski, Juaya) termina 16:00
          → 16:10 esos mismos 4 entran a la cancha 10 de las 17:00
        y en paralelo, dos turnos que se reservan el MISMO viernes no pueden compartir
        ningún carnet, porque a esa altura todavía no se liberó nada."""
        import encuesta as enc
        ag = enc._Agenda()
        club = 1102
        d = lambda h, m=0, dia=8: datetime.datetime(2026, 8, dia, h, m)

        # la cancha de las 15 se toma el viernes 15:01 y el turno termina sáb 16:00
        ag.ocupar(club, "farji", d(15, 1, 7), d(16))
        # a las 16:10 el mismo carnet entra al turno de las 17: TIENE que poder
        self.assertTrue(ag.libre(club, "farji", d(16, 10), d(18)),
                        "el carnet se libera al terminar el turno y se reusa el mismo día")
        ag.ocupar(club, "farji", d(16, 10), d(18))

        # pero la cancha de las 17 se TOMA el viernes 17:01, cuando su carnet sigue
        # adentro de la de las 15: por eso hay que pisarla con otro
        self.assertFalse(ag.libre(club, "farji", d(17, 1, 7), d(18)),
                         "el viernes su carnet todavía está en la reserva de las 15")

        # y no puede estar en dos turnos que se solapan
        self.assertFalse(ag.libre(club, "farji", d(17, 30), d(18, 30)))

    def test_el_plan_siempre_pasa_su_propia_verificacion(self):
        """Guarda de fondo: se arme lo que se arme, ningún carnet puede quedar en dos
        reservas que se pisen ni entrar alguien que no votó esa hora."""
        import encuesta as enc
        votos = {}
        for i, n in enumerate(NOMBRES_TEST):
            if i < 10:
                votos[n] = {"5": {"modo": enc.MODO_JUEGO,
                                  "horas": ["10:00", "15:00", "16:00", "17:00"]},
                            "6": {"modo": enc.MODO_JUEGO, "horas": ["10:00", "16:00"]}}
            else:
                votos[n] = {"5": {"modo": enc.MODO_PRESTA}, "6": {"modo": enc.MODO_PRESTA}}
        plan = enc.armar_cronograma(self._encuesta(votos))
        self.assertTrue(plan["reservas"], "con 10 jugadores tendría que salir algo")
        self.assertTrue(self._sin_errores_de_verificacion(plan),
                        [a for r in plan["reservas"] for a in r["avisos"]])
        # y todos los que quedan en la cancha votaron ESA hora
        r0 = plan["resumen"]["por_dia"]
        for rsv in plan["reservas"]:
            votantes = set(r0[rsv["dia"]]["por_hora_ids"].get(rsv["hora"], []))
            self.assertTrue({x["id"] for x in rsv["juegan"]} <= votantes)

    def test_prefiere_el_plan_donde_juega_mas_gente(self):
        """Dos planes con las mismas canchas no son iguales: gana el que deja a menos
        gente afuera del finde."""
        import encuesta as enc
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00", "16:00"]}}
                 for n in NOMBRES_TEST[:8]}
        votos[NOMBRES_TEST[8]] = {"5": {"modo": enc.MODO_JUEGO, "horas": ["16:00"]}}
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}} for n in NOMBRES_TEST[9:]})
        plan = enc.armar_cronograma(self._encuesta(votos))
        juegan = {x["nombre"] for r in plan["reservas"] for x in r["juegan"]}
        self.assertIn(NOMBRES_TEST[8], juegan,
                      "el que solo puede a las 16 tendría que entrar en la de las 16")

    def test_se_puede_fijar_cuantas_canchas_por_horario(self):
        """Pedido de Diego: 'quiero 2 a las 15 y 1 a las 16'. Lo fijado manda; lo que
        no se fija lo resuelve el armador."""
        import encuesta as enc
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00", "16:00", "17:00"]}}
                 for n in NOMBRES_TEST[:12]}
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}} for n in NOMBRES_TEST[12:]})
        e = self._encuesta(votos)
        plan = enc.armar_cronograma(e, {(5, "15:00"): 2, (5, "16:00"): 0})
        cuenta = {}
        for r in plan["reservas"]:
            cuenta[r["hora"]] = cuenta.get(r["hora"], 0) + 1
        self.assertEqual(cuenta.get("15:00"), 2, "no respetó las 2 de las 15")
        self.assertEqual(cuenta.get("16:00", 0), 0, "no respetó el 0 de las 16")
        self.assertTrue(self._sin_errores_de_verificacion(plan))

    def test_si_lo_fijado_no_entra_avisa_y_no_devuelve_vacio(self):
        """Antes, un horario fijado imposible tiraba abajo el plan entero sin explicar."""
        import encuesta as enc
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00", "16:00"]}}
                 for n in NOMBRES_TEST[:8]}
        e = self._encuesta(votos)
        # 2 canchas a las 16 es imposible: los 8 juegan a las 15 y su carnet recién se
        # libera a las 16:00, justo cuando arranca el turno
        plan = enc.armar_cronograma(e, {(5, "15:00"): 2, (5, "16:00"): 2})
        self.assertTrue(plan["reservas"], "no tenía que devolver un plan vacío")
        self.assertTrue(any("pediste" in a for a in plan["avisos"]), plan["avisos"])
        self.assertTrue(self._sin_errores_de_verificacion(plan))

    def test_la_dispara_un_carnet_prestado_si_ninguno_tiene_cuenta(self):
        """Como en el cronograma de Diego: si entre los que juegan no hay cuenta de
        OnDepor, la reserva la toma un prestado que sí tenga y ocupa un lugar."""
        import encuesta as enc
        sin_cuenta = [n for n in NOMBRES_TEST if n not in CON_CUENTA]
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]}} for n in sin_cuenta[:5]}
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}} for n in CON_CUENTA})
        plan = enc.armar_cronograma(self._encuesta(votos))
        self.assertEqual(len(plan["reservas"]), 1)
        r = plan["reservas"][0]
        self.assertIn(r["reservador"]["nombre"], CON_CUENTA)
        self.assertTrue(any("tiene cuenta de OnDepor" in a for a in r["avisos"]), r["avisos"])
        self.assertTrue(self._sin_errores_de_verificacion(plan))

    def test_el_reservador_prestado_no_dispara_dos_canchas(self):
        """Bug real: al no anotarlo en la agenda, el mismo carnet prestado terminaba
        disparando dos canchas que se pisaban en el tiempo."""
        import encuesta as enc
        sin_cuenta = [n for n in NOMBRES_TEST if n not in CON_CUENTA]
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00", "17:00"]}}
                 for n in sin_cuenta[:9]}
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}} for n in CON_CUENTA})
        plan = enc.armar_cronograma(self._encuesta(votos))
        disparan = [r["reservador"]["id"] for r in plan["reservas"]]
        self.assertEqual(len(disparan), len(set(disparan)),
                         "el mismo carnet no puede disparar dos canchas del día")
        self.assertTrue(self._sin_errores_de_verificacion(plan))

    def test_mira_cuando_se_libera_cada_carnet_no_solo_si_esta_libre(self):
        """Bug que cazó Diego: "una a las 10, una a las 15 y 2 a las 16 te entran
        perfecto" — y entraban. El armador descartaba a los de las 10 junto con los de
        las 15, cuando el de las 10 devuelve el carnet a las 11:00 y llega de sobra al
        turno de las 16 con un cambio; el de las 15 lo devuelve a las 16:00 en punto y
        NO llega. Tratarlos igual hacía perder canchas enteras."""
        import encuesta as enc
        # hacen falta 16 carnets (4 canchas), así que este caso va con un grupo más
        # grande; al terminar se restauran las bases de la clase
        gente = [f"P{i:02d}" for i in range(20)]
        pid = {n: 800 + i for i, n in enumerate(gente)}
        cuentas = gente[:1] + gente[4:5] + gente[8:9] + gente[12:13]
        self._W("db_jugadores.json", {"jugadores": [
            {"nombre": n, "player_id": pid[n]} for n in gente]})
        self._W("db_reservadores.json", {"reservadores": [
            {"nombre": n, "usuario": f"{n}@x", "clave": "k", "player_id": pid[n]}
            for n in cuentas]})
        self.addCleanup(self.setUpClass)
        m, t, s = gente[:4], gente[4:8], gente[8:12]      # 10 h · 15 h · solo 16 h
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["10:00", "16:00"]}} for n in m}
        votos.update({n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00", "16:00"]}} for n in t})
        votos.update({n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["16:00"]}} for n in s})
        votos.update({n: {"5": {"modo": enc.MODO_PRESTA}} for n in gente[12:]})
        e = {"id": "enc-test", "token": "tok", "dias": [5], "horas": enc.HORAS_POR_DEFECTO,
             "fechas": {"5": self.sab}, "estado": "abierta",
             "participantes": [str(pid[n]) for n in gente], "carnets_extra": []}
        self._W("encuesta_config.json", e)
        self._W("votos.json", {"votos": {"enc-test": {
            str(pid[n]): {"nombre": n, "dias": d, "cuando": ""} for n, d in votos.items()}}})
        plan = enc.armar_cronograma(e, {(5, "10:00"): 1, (5, "15:00"): 1, (5, "16:00"): 2})
        horas = sorted(r["hora"] for r in plan["reservas"])
        self.assertEqual(horas, ["10:00", "15:00", "16:00", "16:00"],
                         "las 2 de las 16 entran: los de las 10 se liberan a las 11:00")
        # y los que entran a las 16 son los de las 10 (no los de las 15, que no llegan)
        de_las_15 = {str(pid[n]) for n in t}
        c16 = [r for r in plan["reservas"] if r["hora"] == "16:00"]
        entran = {p for r in c16 if r["cambio"] for p in r["cambio"]["entran"]}
        self.assertFalse(entran & de_las_15,
                         "metió a alguien de las 15, que se libera recién a las 16:00")
        self.assertTrue(self._sin_errores_de_verificacion(plan))

    def test_respeta_los_carnets_de_las_reservas_ya_cargadas(self):
        """Bug que cazó Diego: "Furmanski no tiene libre el carnet hasta las 17 del
        sábado" — tenía una reserva YA cargada a las 16 y el armador planificaba como si
        el finde estuviera vacío, proponiendo canchas sobre carnets comprometidos."""
        import encuesta as enc
        ocupados = NOMBRES_TEST[:4]
        self._W("reservas.json", {"reservas": [{
            "id": "ya-1", "club_id": 1102, "cancha": "", "fecha": self.sab,
            "hora_desde": "16:00", "hora_hasta": "17:00", "single_double": 2,
            "reservador": ocupados[0],
            "jugadores": [self.pid[n] for n in ocupados[1:]],
            "creado_por": "Diego"}]})
        # ...y ahora se pide una cancha a las 15 con ESA misma gente
        votos = {n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]}} for n in ocupados}
        e = {"id": "enc-test", "token": "tok", "dias": [5], "horas": enc.HORAS_POR_DEFECTO,
             "fechas": {"5": self.sab}, "estado": "abierta",
             "participantes": [str(self.pid[n]) for n in NOMBRES_TEST], "carnets_extra": []}
        self._W("encuesta_config.json", e)
        self._W("votos.json", {"votos": {"enc-test": {
            str(self.pid[n]): {"nombre": n, "dias": d, "cuando": ""}
            for n, d in votos.items()}}})
        plan = enc.armar_cronograma(e)
        # sus carnets están tomados desde el viernes 16:01: no se pueden usar a las 15
        usados = {x["id"] for r in plan["reservas"]
                  for x in r["titulares"] + [r["reservador"]]}
        self.assertFalse(usados & {str(self.pid[n]) for n in ocupados},
                         "usó un carnet que ya estaba en una reserva cargada")
        self.assertTrue(self._sin_errores_de_verificacion(plan))

    def test_mensaje_para_whatsapp(self):
        """El botón arma el texto con las canchas REALES del finde (no la propuesta), y
        cuando hay un cambio de carnets agendado lista a los que van a jugar DESPUÉS."""
        import encuesta as enc
        self._encuesta({n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]}}
                        for n in NOMBRES_TEST[:4]})
        self._W("reservas.json", {"reservas": [{
            "id": "m-1", "club_id": 1102, "cancha": "3", "fecha": self.sab,
            "hora_desde": "15:00", "hora_hasta": "16:00", "single_double": 2,
            "reservador": "Ana", "jugadores": [self.pid[n] for n in NOMBRES_TEST[1:4]],
            "creado_por": "Diego",
            "cambio_prog": {"estado": "pendiente", "reservador": "Dario",
                            "jugadores": [self.pid[n] for n in NOMBRES_TEST[4:7]],
                            "hora_ej": "14:00", "ej_epoch": 1}}]})
        c = self.cliente()
        r = c.get("/encuesta/mensaje")
        self.assertEqual(r.status_code, 200)
        h = r.get_data(as_text=True)
        self.assertIn("Canchas del finde", h)
        self.assertIn("15:00", h)
        # los del cambio, no los originales
        self.assertIn(NOMBRES_TEST[4], h)
        self.assertNotIn("&gt;" + NOMBRES_TEST[1], h)
        with self.web.app.test_request_context():
            txt = self.web._mensaje_finde(self.web.enc.cargar_encuesta())
        self.assertIn("Dario", txt)
        # los originales NO van como jugadores, pero SÍ en la aclaración de con qué
        # carnet se tomó la cancha (pedido de Diego)
        linea_juegan = next(l for l in txt.splitlines() if "Dario" in l and "carnet" not in l)
        self.assertNotIn("Beto", linea_juegan, "listó a los originales como jugadores")
        self.assertIn("tomada con el carnet de", txt)
        self.assertIn("Beto", txt, "no aclaró con qué carnets se reservó")
        self.assertIn("se cambia antes del turno", txt)
        self.assertIn("(a confirmar)", txt, "esa reserva todavía no se disparó")

    def test_el_mensaje_pide_permiso(self):
        r = self.cliente("pablo", "clave2").get("/encuesta/mensaje", follow_redirects=True)
        self.assertIn("No tenés permiso", r.get_data(as_text=True))

    def test_sin_permiso_no_se_programa(self):
        import encuesta as enc
        self._encuesta({n: {"5": {"modo": enc.MODO_JUEGO, "horas": ["15:00"]}}
                        for n in NOMBRES_TEST[:8]})
        self._W("reservas.json", {"reservas": []})
        self.cliente("pablo", "clave2").post("/encuesta/cronograma/programar",
                                             follow_redirects=True)
        self.assertEqual(self.web.cargar("reservas.json", "reservas"), [],
                         "un 'jugador' no debería poder programar el cronograma")


class TestVincularReservadores(BaseApp):
    """En db_reservadores.json el player_id es OPCIONAL y casi ninguno lo tiene: el
    resto de la app trabaja por nombre. El armador SÍ lo necesita (el carnet del
    reservador entra en la reserva). Esto se escapó en la primera versión y dejaba
    el cronograma casi vacío, así que queda cubierto."""

    def _bases(self, jugadores, reservadores):
        for nombre, obj in (("db_jugadores.json", {"jugadores": jugadores}),
                            ("db_reservadores.json", {"reservadores": reservadores})):
            with open(os.path.join(self.tmp, nombre), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)

    def test_resuelve_por_nombre_acentos_y_apodos(self):
        import encuesta as enc
        self._bases(
            [{"nombre": "Ana Rossi", "player_id": 1},
             {"nombre": "Darío Zelione", "player_id": 2},     # el reservador va sin tilde
             {"nombre": "Rafa Juaya", "player_id": 3}],       # y este con apodo
            [{"nombre": "Ana Rossi", "usuario": "a@x", "clave": "k"},
             {"nombre": "Dario Zelione", "usuario": "d@x", "clave": "k"},
             {"nombre": "Rafael Juaya", "usuario": "r@x", "clave": "k"}])
        listos, sin_vincular = enc.reservadores_con_carnet()
        self.assertEqual(sin_vincular, [])
        self.assertEqual(set(listos), {"1", "2", "3"})
        self.assertEqual(listos["3"], "Rafael Juaya", "guarda el nombre del RESERVADOR")

    def test_no_adivina_cuando_hay_dos_candidatos(self):
        """Meter el carnet equivocado en una reserva es peor que avisar."""
        import encuesta as enc
        self._bases(
            [{"nombre": "Jonathan Enser", "player_id": 10},
             {"nombre": "Jacobo Enser", "player_id": 11}],
            [{"nombre": "Jony Enser", "usuario": "j@x", "clave": "k"}])
        listos, sin_vincular = enc.reservadores_con_carnet()
        self.assertEqual(listos, {})
        self.assertEqual(sin_vincular, ["Jony Enser"])

    def test_el_player_id_guardado_gana(self):
        import encuesta as enc
        self._bases([{"nombre": "Ana Rossi", "player_id": 99}],
                    [{"nombre": "Ana Rossi", "usuario": "a@x", "clave": "k", "player_id": 7}])
        listos, _ = enc.reservadores_con_carnet()
        self.assertEqual(set(listos), {"7"})

    def test_al_guardar_un_reservador_se_vincula_solo(self):
        """Para que no haya que arreglarlo a mano cada vez que se suma alguien."""
        import encuesta as enc
        self._bases([{"nombre": "Ana Rossi", "player_id": 55}], [])
        self.cliente().post("/reservadores/guardar",
                            data={"nombre": "Ana Rossi", "usuario": "a@x.com", "clave": "k"},
                            follow_redirects=True)
        guardado = self.web.cargar("db_reservadores.json", "reservadores")[0]
        self.assertEqual(guardado["player_id"], 55)

    def test_sin_cuenta_de_ondepor_no_cuenta(self):
        import encuesta as enc
        self._bases([{"nombre": "Ana Rossi", "player_id": 1}],
                    [{"nombre": "Ana Rossi", "clave": "k"}])       # sin usuario
        listos, sin_vincular = enc.reservadores_con_carnet()
        self.assertEqual((listos, sin_vincular), ({}, []))


# ----------------------------------------------------------------------
#  15 bis. Cambio de carnets sobre reservas que TODAVÍA NO se dispararon
#  (los casos los sacó una revisión adversarial antes de habilitarlo)
# ----------------------------------------------------------------------
class TestCambioEnProgramadas(BaseApp):
    def _parche(self, nombre, valor):
        """Parchea webapp y lo DEJA COMO ESTABA al terminar. Sin esto el parche se filtra
        a las otras clases (el módulo es uno solo) y rompe tests que no tienen nada que
        ver — pasó con notificar()."""
        original = getattr(self.web, nombre)
        setattr(self.web, nombre, valor)
        self.addCleanup(setattr, self.web, nombre, original)

    def setUp(self):
        self.manana = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        self._reserva()

    def _reserva(self, cp=None):
        obj = {"id": "prog-1", "club_id": 1102, "cancha": "3", "fecha": self.manana,
               "hora_desde": "15:00", "hora_hasta": "16:00", "single_double": 2,
               "reservador": "Diego Fride", "jugadores": [253510, 130962, 304064],
               "creado_por": "Diego", "apertura_iso": None, "horas_antes": 24,
               "minutos_extra": 1, "reintentos": 40, "espera_entre_reintentos_seg": 0.3}
        if cp:
            obj["cambio_prog"] = cp
        with open(os.path.join(self.tmp, "reservas.json"), "w", encoding="utf-8") as f:
            json.dump({"reservas": [obj]}, f, ensure_ascii=False)
        with self.web._estados_lock:
            self.web._estados.pop("prog-1", None)     # sin estado = "programada"
        return obj

    def _cp(self):
        return (self.web.cargar("reservas.json", "reservas")[0] or {}).get("cambio_prog")

    def test_avisa_al_que_cargo_la_reserva_aunque_la_cancele_otro(self):
        """Regla de Diego: confirmada, cancelada, carnets cambiados o fallada, el push va
        SIEMPRE al usuario que la programó — no al reservador de OnDepor."""
        avisos = []
        self._parche("notificar", lambda u, t, c, url="/reservas": avisos.append((u, t)))
        self._parche("_cancelar_en_ondepor", lambda o: "✅ Turno cancelado también en OnDepor.")
        self.web.set_estado("prog-1", "reservada")
        self.cliente().post("/reservas/borrar/prog-1", data={"cancelar_ml": "1"},
                            follow_redirects=True)
        self.assertEqual([u for u, _ in avisos], ["Diego"])
        self.assertIn("cancelado", avisos[0][1].lower())

    def test_avisa_al_que_cargo_la_reserva_en_el_cambio_a_mano(self):
        avisos = []
        self._parche("notificar", lambda u, t, c, url="/reservas": avisos.append((u, t)))
        self._parche("_hacer_cambio_jugadores", lambda *a, **k: ("success", "listo"))
        self.web.set_estado("prog-1", "reservada")
        self.cliente().post("/reservas/cambiar-jugadores", data={
            "cambiar_id": "prog-1", "reservador": "Diego Fride",
            "jug": ["253510", "130962", "304064"]}, follow_redirects=True)
        self.assertEqual([u for u, _ in avisos], ["Diego"])

    def test_avisa_por_push_cuando_corre_el_cambio(self):
        """Corre solo y sin nadie mirando: si no entró hay que enterarse ANTES del turno.
        El push va SOLO al que creó la reserva (Diego preguntó a quién le avisa)."""
        avisos = []
        self._parche("notificar", lambda u, t, c, url="/reservas": avisos.append((u, t, c)))
        for st, esperado in (("success", "cambiados"), ("perdida", "NO salió")):
            avisos.clear()
            self._reserva({"reservador": "Diego Fride", "jugadores": [253510, 130962, 304064],
                           "estado": "en_curso", "detalle": "", "hora_ej": "14:50",
                           "ej_epoch": 0, "creado_por": "Diego"})
            self._parche("_hacer_cambio_jugadores", lambda *a, **k: (st, "mensaje de prueba"))
            self.web._correr_accion("cambio_prog", "prog-1")
            self.assertEqual(len(avisos), 1, f"no avisó nada con {st}")
            self.assertEqual(avisos[0][0], "Diego", "el push tiene que ir al creador")
            self.assertIn(esperado, avisos[0][1])

    # -- CANCELACIÓN programada (pedido de Diego, 12/08/2026) --
    def _kp(self):
        return (self.web.cargar("reservas.json", "reservas")[0] or {}).get("cancel_prog")

    def test_programar_cancelacion(self):
        r = self.cliente().post("/reservas/programar-cancelacion", data={
            "cancelar_prog_id": "prog-1", "min_antes": "60"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        kp = self._kp()
        self.assertEqual(kp["estado"], "pendiente")
        self.assertEqual(kp["hora_ej"], "14:00")     # turno 15:00 - 60 min

    def test_anular_la_cancelacion_conserva_el_turno(self):
        self.cliente().post("/reservas/programar-cancelacion",
                            data={"cancelar_prog_id": "prog-1", "min_antes": "60"})
        c = self.cliente()
        c.post("/reservas/cancel-prog-borrar/prog-1", follow_redirects=True)
        self.assertIsNone(self._kp())
        self.assertTrue(self.web.cargar("reservas.json", "reservas"), "borró la reserva")

    def test_no_cancela_una_reserva_que_no_esta_confirmada(self):
        """⚠ Sobre una reserva que todavía no se disparó, cancelar en OnDepor se
        llevaría OTRO turno del día. Misma garantía que el cambio de nombres."""
        self.cliente().post("/reservas/programar-cancelacion",
                            data={"cancelar_prog_id": "prog-1", "min_antes": "60"})
        with self.web.rb._io_lock:
            lista = self.web.cargar("reservas.json", "reservas")
            lista[0]["cancel_prog"]["ej_epoch"] = 0      # ya pasó la hora
            self.web.guardar("reservas.json", "reservas", lista)
        llamadas = []
        self._parche("_cancelar_en_ondepor", lambda o: llamadas.append(o) or "✅ ok")
        self.web._revisar_acciones_prog()
        self.assertEqual(llamadas, [], "canceló sin turno confirmado")
        self.assertEqual(self._kp()["estado"], "pendiente")

    def test_si_el_turno_ya_empezo_no_cancela_nada(self):
        ayer = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        with self.web.rb._io_lock:
            lista = self.web.cargar("reservas.json", "reservas")
            lista[0]["fecha"] = ayer
            lista[0]["cancel_prog"] = {"estado": "pendiente", "detalle": "",
                                       "hora_ej": "14:00", "ej_epoch": 0,
                                       "creado_por": "Diego"}
            self.web.guardar("reservas.json", "reservas", lista)
        self.web.set_estado("prog-1", "reservada")
        llamadas = []
        self._parche("_cancelar_en_ondepor", lambda o: llamadas.append(o) or "✅ ok")
        self.web._revisar_acciones_prog()
        self.assertEqual(llamadas, [])
        self.assertEqual(self._kp()["estado"], "vencido")

    def test_cancelar_en_ondepor_no_se_lleva_el_turno_equivocado(self):
        """⚠ Antes matcheaba solo por fecha+club: con dos turnos el mismo día cancelaba
        el primero que encontraba. Corriendo solo, eso es un desastre."""
        obj = {"reservador": "Diego Fride", "fecha": self.manana, "club_id": 1102,
               "hora_desde": "15:00", "hora_hasta": "16:00"}
        cancelados = []

        class ApiFalsa:
            def login(self):
                pass

            def mis_reservas(self, dias=30):
                return [{"id": 111, "fecha": obj["fecha"], "club_id": 1102, "hora_to": "12:00"},
                        {"id": 222, "fecha": obj["fecha"], "club_id": 1102, "hora_to": "16:00"}]

            def cancelar_reserva(self, rid):
                cancelados.append(rid)
                return {"status": "success"}
        self._parche("OnDepor", lambda *a, **k: ApiFalsa())
        msg = self.web._cancelar_en_ondepor(obj)
        self.assertEqual(cancelados, [222], "canceló el turno de otra hora")
        self.assertIn("✅", msg)

    def test_se_puede_programar_sobre_una_no_disparada(self):
        r = self.cliente().post("/reservas/programar-cambio", data={
            "programar_id": "prog-1", "reservador": "Diego Fride", "min_antes": "30",
            "jug": ["253510", "130962", "304064"]}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        cp = self._cp()
        self.assertTrue(cp, "tenía que quedar agendado sobre la reserva programada")
        self.assertEqual(cp["hora_ej"], "14:30")

    def test_el_cambio_inmediato_sigue_exigiendo_confirmada(self):
        """CRÍTICO: cambiar_jugadores CANCELA en OnDepor y ubica el turno por fecha+club,
        así que sobre una reserva no tomada cancelaría OTRO turno del mismo día."""
        r = self.cliente().post("/reservas/cambiar-jugadores", data={
            "cambiar_id": "prog-1", "reservador": "Diego Fride",
            "jug": ["253510", "130962", "304064"]}, follow_redirects=True)
        self.assertIn("todavía no se disparó", r.get_data(as_text=True))

    def test_el_form_inmediato_no_se_abre_ni_forzando_la_url(self):
        h = self.cliente().get("/reservas?cambiar=prog-1").get_data(as_text=True)
        self.assertNotIn("cambiar_id", h)

    def test_editar_la_reserva_no_borra_el_cambio_agendado(self):
        """CRÍTICO: _guardar_reserva_desde_form rearma el objeto de cero. Sin arrastrar
        el cambio_prog, editar la reserva lo hacía desaparecer en silencio."""
        self.cliente().post("/reservas/programar-cambio", data={
            "programar_id": "prog-1", "reservador": "Diego Fride", "min_antes": "30",
            "jug": ["253510", "130962", "304064"]}, follow_redirects=True)
        self.assertTrue(self._cp())
        # ahora edita la reserva moviéndola una hora más tarde
        self.cliente().post("/reservas/crear", data={
            "edit_orig": "prog-1", "sede": "dia", "fecha": self.manana, "hora": "16:00",
            "reservador": "Diego Fride", "cancha": "3",
            "jug": ["253510", "130962", "304064"]}, follow_redirects=True)
        rsv = self.web.cargar("reservas.json", "reservas")[0]
        cp = rsv.get("cambio_prog")
        self.assertTrue(cp, "el cambio agendado se perdió al editar")
        self.assertEqual(cp["hora_ej"], "15:30",
                         "tenía que seguir corriendo 30 min antes del turno NUEVO")

    def test_un_cambio_de_una_reserva_que_nunca_entro_se_marca_vencido(self):
        """Antes quedaba 'pendiente' para siempre, prometiendo algo que no iba a pasar."""
        ayer = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        with open(os.path.join(self.tmp, "reservas.json"), "w", encoding="utf-8") as f:
            json.dump({"reservas": [{
                "id": "vieja", "club_id": 1102, "cancha": "", "fecha": ayer,
                "hora_desde": "15:00", "hora_hasta": "16:00", "reservador": "Diego Fride",
                "jugadores": [253510, 130962, 304064], "creado_por": "Diego",
                "cambio_prog": {"estado": "pendiente", "reservador": "Diego Fride",
                                "jugadores": [1, 2, 3], "ej_epoch": 1}}]}, f)
        self.web._revisar_acciones_prog()
        cp = self.web.cargar("reservas.json", "reservas")[0]["cambio_prog"]
        self.assertEqual(cp["estado"], "vencido")

    def test_no_dispara_si_la_reserva_se_confirmo_tarde(self):
        """Si entró recién DESPUÉS de la hora agendada (la cazó la vigilancia), cancelar
        y volver a tomar es jugarse la cancha recién ganada: se marca vencido y avisa."""
        ahora = self.core.hora_real()
        turno = ahora + 3600                      # el turno todavía no empezó
        with open(os.path.join(self.tmp, "reservas.json"), "w", encoding="utf-8") as f:
            json.dump({"reservas": [{
                "id": "tarde", "club_id": 1102, "cancha": "", "fecha": self.manana,
                "hora_desde": "15:00", "hora_hasta": "16:00", "reservador": "Diego Fride",
                "jugadores": [253510, 130962, 304064], "creado_por": "Diego",
                "cambio_prog": {"estado": "pendiente", "reservador": "Diego Fride",
                                "jugadores": [1, 2, 3],
                                "ej_epoch": ahora - 3600}}]}, f)   # 1 h de atraso
        with self.web._estados_lock:
            self.web._estados["tarde"] = {"estado": "reservada"}
        disparos = []
        orig = self.web._ejecutar_cambio_prog
        self.web._ejecutar_cambio_prog = lambda rid: disparos.append(rid)
        try:
            rsv = self.web.cargar("reservas.json", "reservas")[0]
            rsv["fecha"], rsv["hora_desde"] = self.manana, "15:00"
            self.web._revisar_acciones_prog()
        finally:
            self.web._ejecutar_cambio_prog = orig
            with self.web._estados_lock:
                self.web._estados.pop("tarde", None)
        self.assertEqual(disparos, [], "no tenía que ejecutar el cambio con tanto atraso")
        self.assertEqual(self.web.cargar("reservas.json", "reservas")[0]["cambio_prog"]["estado"],
                         "vencido")

    def test_no_acepta_jugadores_inventados_ni_repetidos(self):
        """Esto queda GUARDADO y se ejecuta solo: un id inventado haría que el bot
        cancele la cancha y después no la pueda recuperar."""
        self.cliente().post("/reservas/programar-cambio", data={
            "programar_id": "prog-1", "reservador": "Diego Fride", "min_antes": "30",
            "jug": ["999999", "888888", "777777"]}, follow_redirects=True)
        self.assertIsNone(self._cp(), "aceptó player_ids que no existen")
        self.cliente().post("/reservas/programar-cambio", data={
            "programar_id": "prog-1", "reservador": "Diego Fride", "min_antes": "30",
            "jug": ["253510", "253510", "253510"]}, follow_redirects=True)
        self.assertIsNone(self._cp(), "aceptó el mismo jugador 3 veces")

    def test_el_boton_aparece_en_programadas_pero_no_en_falladas(self):
        h = self.cliente().get("/reservas").get_data(as_text=True)
        self.assertIn("?programar=prog-1", h)
        self.assertNotIn("?cambiar=prog-1", h, "el cambio inmediato no va en una no disparada")
        with self.web._estados_lock:
            self.web._estados["prog-1"] = {"estado": "fallada"}
        try:
            h2 = self.cliente().get("/reservas").get_data(as_text=True)
            self.assertNotIn("?programar=prog-1", h2, "en una fallada no hay nada que cambiar")
        finally:
            with self.web._estados_lock:
                self.web._estados.pop("prog-1", None)

    def test_el_form_avisa_que_la_reserva_se_toma_con_los_nombres_de_hoy(self):
        h = self.cliente().get("/reservas?programar=prog-1").get_data(as_text=True)
        self.assertIn("se va a tomar igual con los nombres de ahora", h)
        self.assertIn("?editar=prog-1", h, "tiene que ofrecer Editar como alternativa")


# ----------------------------------------------------------------------
#  16. Huella (WebAuthn) — mismo esquema que el Jalomi Manager
# ----------------------------------------------------------------------
class TestHuella(BaseApp):
    """No se prueba la criptografía (eso es de la librería y necesita un aparato
    real): se prueba lo NUESTRO — permisos, el challenge en cookie firmada, que
    una credencial desconocida no entre y que cada uno borre solo las suyas."""

    def _pk(self, usuario, cid):
        return {"usuario": usuario, "credential_id": cid, "public_key": "cGs",
                "sign_count": 0, "dispositivo": "Test", "creado": "2026-07-29 10:00",
                "ultimo_uso": ""}

    def test_pantalla_y_registro_piden_login(self):
        c = self.web.app.test_client()
        self.assertEqual(c.get("/huella").status_code, 302)
        self.assertEqual(c.post("/passkeys/registro/opciones").status_code, 302)

    def test_opciones_de_login_dan_challenge_y_cookie(self):
        c = self.web.app.test_client()
        r = c.post("/passkeys/login/opciones")
        self.assertEqual(r.status_code, 200)
        self.assertIn("challenge", r.get_json())
        self.assertTrue(any("reservame_wa_reto" in h
                            for h in r.headers.getlist("Set-Cookie")),
                        "el challenge tiene que viajar en la cookie firmada")

    def test_credencial_desconocida_no_entra(self):
        c = self.web.app.test_client()
        c.post("/passkeys/login/opciones")          # deja la cookie del reto
        r = c.post("/passkeys/login/verificar", json={"id": "no-existe"})
        self.assertEqual(r.status_code, 401)
        # y la sesión NO quedó abierta
        self.assertEqual(c.get("/reservas").status_code, 302)

    def test_sin_reto_no_se_verifica(self):
        c = self.web.app.test_client()
        r = c.post("/passkeys/login/verificar", json={"id": "x"})
        self.assertEqual(r.status_code, 400)

    def test_cada_uno_borra_solo_sus_huellas(self):
        import json as _json
        with open(os.path.join(self.tmp, "passkeys.json"), "w", encoding="utf-8") as f:
            _json.dump({"passkeys": [self._pk("diego", "cid-diego"),
                                     self._pk("pablo", "cid-pablo")]}, f)
        # pablo intenta borrar la de diego: no pasa nada
        self.cliente("pablo", "clave2").post("/huella/borrar", data={"cid": "cid-diego"},
                                             follow_redirects=True)
        quedan = {p["credential_id"] for p in self.web.cargar("passkeys.json", "passkeys")}
        self.assertEqual(quedan, {"cid-diego", "cid-pablo"})
        # la suya sí
        self.cliente("pablo", "clave2").post("/huella/borrar", data={"cid": "cid-pablo"},
                                             follow_redirects=True)
        quedan = {p["credential_id"] for p in self.web.cargar("passkeys.json", "passkeys")}
        self.assertEqual(quedan, {"cid-diego"})

    def test_el_origen_es_https_detras_de_caddy(self):
        """Bug real: detrás de Caddy la request llega por http a 127.0.0.1 y el origen
        salía "http://…", así que el navegador firmaba https y WebAuthn rechazaba."""
        with self.web.app.test_request_context(
                "/", base_url="http://reservatucancha.duckdns.org"):
            self.assertEqual(self.web._wa_origen(), "https://reservatucancha.duckdns.org")
            self.assertEqual(self.web._wa_rp_id(), "reservatucancha.duckdns.org")
        # si el proxy sí manda el header, manda el header
        with self.web.app.test_request_context(
                "/", base_url="http://reservatucancha.duckdns.org",
                headers={"X-Forwarded-Proto": "https"}):
            self.assertEqual(self.web._wa_origen(), "https://reservatucancha.duckdns.org")
        # en local sigue siendo http (para poder probar sin certificado)
        with self.web.app.test_request_context("/", base_url="http://localhost:8080"):
            self.assertEqual(self.web._wa_origen(), "http://localhost:8080")
            self.assertEqual(self.web._wa_rp_id(), "localhost")

    def test_la_pantalla_lista_solo_las_propias(self):
        import json as _json
        with open(os.path.join(self.tmp, "passkeys.json"), "w", encoding="utf-8") as f:
            _json.dump({"passkeys": [self._pk("diego", "cid-d2"),
                                     self._pk("pablo", "cid-p2")]}, f)
        h = self.cliente("pablo", "clave2").get("/huella").get_data(as_text=True)
        self.assertIn("cid-p2", h)
        self.assertNotIn("cid-d2", h)


if __name__ == "__main__":
    unittest.main(verbosity=2)
