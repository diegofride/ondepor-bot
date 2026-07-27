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
        cands = self.rb._candidatos_fuente({"fecha": manana, "hora_desde": "18:00", "club_id": 504})
        self.assertTrue(cands)
        self.assertEqual(cands[0][0], self.core.hoy_ar().isoformat())

    def test_candidatos_del_mismo_dia_van_primero(self):
        manana = (self.core.hoy_ar() + datetime.timedelta(days=1)).isoformat()
        cands = self.rb._candidatos_fuente({"fecha": manana, "hora_desde": "21:00", "club_id": 504})
        self.assertEqual(cands[0], (manana, "20:00", "21:00"))

    def test_ultimo_turno_termina_en_24(self):
        """23:00 -> 24:00 (poner 00:00 da 'reservá al menos 1 hora')."""
        manana = (self.core.hoy_ar() + datetime.timedelta(days=1)).isoformat()
        cands = self.rb._candidatos_fuente({"fecha": manana, "hora_desde": "18:00", "club_id": 504})
        self.assertIn((self.core.hoy_ar().isoformat(), "23:00", "24:00"), cands)

    def test_mapa_de_canchas_completo(self):
        self.assertEqual(len(self.rb.CANCHAS_POR_CLUB[1102]), 11)
        self.assertEqual(len(self.rb.CANCHAS_POR_CLUB[504]), 8)

    def test_precarga_40s_antes(self):
        self.assertEqual(self.rb.PRECARGA_ANTES_SEG, 40)


# ----------------------------------------------------------------------
#  5. confirmar_objetivo: paralelo solo con cancha puntual ocupada
# ----------------------------------------------------------------------
class TestConfirmarObjetivo(BaseApp):
    def _api(self, ocupadas):
        api = self.core.OnDepor.__new__(self.core.OnDepor)
        api.club_id, api.svid, api.csrf, api._ultimo_form = 504, "x", "y", {}
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

    def test_hora_que_no_es_del_turno_se_rechaza(self):
        with self.assertRaises(ValueError):      # 22:00 es nocturno, no diurno
            self._guardar(self._form(sede="dia", hora="22:00"))

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

    def test_no_se_abre_sin_participantes(self):
        import encuesta as enc
        r = self.cliente().post("/encuesta/abrir", data={}, follow_redirects=True)
        self.assertIn("al menos una persona", r.get_data(as_text=True))

    def test_voto_separa_jugar_de_prestar_carnet(self):
        import encuesta as enc
        todos = enc.todos_los_jugadores()
        pid = todos[0]["id"]
        self._abrir([pid])
        e = enc.cargar_encuesta()
        c = self.web.app.test_client()
        c.get(f"/e/{e['token']}/soy/{pid}", follow_redirects=True)
        # no juega ningún día, pero SÍ presta el carnet
        c.post(f"/e/{e['token']}/votar",
               data={"no5": "on", "no6": "on", "carnet": "si"}, follow_redirects=True)
        v = enc.votos_de(e["id"])[pid]
        self.assertEqual(v["juega"], {})
        self.assertTrue(v["presta_carnet"])

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

        def votar(pid, juega, presta):
            cl = self.web.app.test_client()
            cl.get(f"/e/{e['token']}/soy/{pid}")
            datos = {"carnet": "si" if presta else "no", "no6": "on"}
            if juega:
                datos["h5"] = ["10:00"]
            else:
                datos["no5"] = "on"
            cl.post(f"/e/{e['token']}/votar", data=datos)

        votar(a, True, True)      # juega y presta -> cuenta UNA vez
        votar(b, False, True)     # presta sin jugar
        votar(c, False, False)    # ni juega ni presta -> fuera de la bolsa

        r = enc.resumen_votos(enc.cargar_encuesta())
        self.assertEqual(len(r["bolsa"]), 2, "deberían ser solo a y b")
        self.assertNotIn(c, r["bolsa"])

        # sumo uno de afuera de la encuesta
        self.cliente().post("/encuesta/carnets", data={"p": [afuera]}, follow_redirects=True)
        r = enc.resumen_votos(enc.cargar_encuesta())
        self.assertEqual(len(r["bolsa"]), 3)
        self.assertIn(afuera, r["bolsa"])
        # el desglose tiene que sumar exacto (sin superposición)
        self.assertEqual(len(r["juegan"]) + len(r["solo_prestan"]) + len(r["extra"]),
                         len(r["bolsa"]))

    def test_carnets_de_afuera_no_ofrece_a_los_de_la_encuesta(self):
        import re
        import encuesta as enc
        todos = enc.todos_los_jugadores()
        dentro = [todos[0]["id"], todos[1]["id"]]
        self._abrir(dentro)
        html = self.cliente().get("/encuesta/carnets").get_data(as_text=True)
        ofrecidos = re.findall(r"name='p' value='(\d+)'", html)
        for pid in dentro:
            self.assertNotIn(pid, ofrecidos, "no debería ofrecer a quien ya participa")

    def test_canchas_segun_la_regla_del_grupo(self):
        import encuesta as enc
        for n, esperado in ((3, 0), (4, 1), (7, 1), (8, 2), (11, 2), (12, 3), (20, 3)):
            self.assertEqual(enc.canchas_para(n), esperado, f"con {n} jugadores")


if __name__ == "__main__":
    unittest.main(verbosity=2)
