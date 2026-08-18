# Auditoría integral — Reservame ésta! (bot de reservas de pádel)
**Fecha:** 2026-08-18 · **Alcance:** todo el sistema en producción (reservatucancha.duckdns.org)
**Método:** 6 revisores en paralelo (integración / flujos / código web / código core / seguridad OWASP 2025 / UX+eficiencia) sobre el código real, y cada hallazgo grave verificado por un agente adversarial independiente que intentó refutarlo leyendo el código. **14 graves confirmados, 0 refutados**, más 51 medios/bajos. Es la **primera auditoría** de este proyecto (no hay anterior para comparar).

## Paso 0 — Criterios vigentes aplicados
1. **OWASP 2025**: A01 Broken Access Control sigue primero; A02 pasa a Security Misconfiguration; nuevo foco en supply chain ([outpost24](https://outpost24.com/blog/owasp-top-10-2025-what-changed/), [aikido](https://www.aikido.dev/blog/owasp-top-10-2025-changes-for-developers), [owasp.org](https://owasp.org/www-project-top-ten/)). → acá: autorización endpoint por endpoint y estados forzables.
2. **Flask 2026**: Secure/HttpOnly/SameSite (✅ ya está), CSRF token en POST con cookie de sesión (❌ falta, mitigado por SameSite=Lax), SECRET_KEY fuerte, deps pineadas ([corgea](https://corgea.com/learn/flask-security-best-practices-2025), [escape.tech](https://escape.tech/blog/best-practices-protect-flask-applications/)).
3. **PWA/push**: opt-in contextual (✅), moderación de frecuencia (✅), feedback visible de cada acción — NN/g "visibility of system status" ([toptal](https://www.toptal.com/designers/ux/notification-design), [uxpilot](https://uxpilot.ai/blogs/ux-best-practices)).
4. Objetivo táctil ≥44 px en móvil; autocomplete en login; sin estados contradictorios en pantalla.
5. Para un sistema que ejecuta acciones DESTRUCTIVAS solo (cancelar/retomar canchas de madrugada): identificar el objetivo sin ambigüedad, verificar después de actuar, y dejar rastro de todo.

## Paso 1 — Integración de lo último (5 commits de la jornada)
- ✅ `server == HEAD == working tree` archivo por archivo (solo difieren CRLF).
- ✅ 146 tests verdes en local (3.14) y en el server (3.10). Dependencias modernas (Flask 3.1.3, urllib3 2.7, waitress 3.0.2).
- ⚠ `requirements.txt` no incluye `pywebpush` ni `webauthn` está verificado post-install: una instalación desde cero queda sin push. (fix: 1 línea)
- ⚠ Lo nuevo (cancel_prog) nació COPIANDO a cambio_prog en vez de compartir (3 pares de funciones gemelas) y dejó dos huecos graves que están abajo (C1, C2).

## Hallazgos GRAVES confirmados (verificación adversarial)

### G1 · [ALTO] El manager pisa el estado nuevo 'cancelada' y lo convierte en 'vencida' en menos de 12 segundos
**Dónde:** `webapp.py:1006` · **Esfuerzo:** minutos

bf1cad8 promete que la reserva cancelada por cancel_prog 'queda marcada cancelada con el resultado a la vista' (_ejecutar_cancel_prog hace set_estado(rid, 'cancelada') en la linea 908). Pero el loop del manager solo saltea est in ('en_curso', 'reservada', 'fallada', 'vencida'): 'cancelada' no esta en la tupla, y como la apertura paso hace horas, falta < -VENTANA_VENCIDA_SEG y al siguiente ciclo (MANAGER_CICLO_SEG=12 s) hace set_estado(rid, 'vencida') pisando el estado Y el detalle con el mensaje de la cancelacion. La fila queda con chip 'Vencida' + nota 'Turno cancelado a las HH:MM' (contradictorio), y encima con est='vencida' aparece el boton 'Vigilar cancelaciones' (linea 2073): un click ahi y el bot RE-TOMA la cancha que se acaba de cancelar a proposito.

**Fix:** Agregar 'cancelada' a la tupla de estados que el manager saltea en la linea 1006. Revisar tambien que la fila cancelada no caiga en la rama de botones de 'programada' (linea 2062) y que _fila_reserva no ofrezca vigilar sobre una cancelada.

### G2 · [ALTO] El fix de matchear por hora se aplico a _cancelar_en_ondepor pero NO a cambiar_jugadores, que cancela igual
**Dónde:** `ondepor_core.py:676` · **Esfuerzo:** horas

bf1cad8 arreglo en _cancelar_en_ondepor (webapp.py:2950-2959) el bug de matchear el turno solo por fecha+club ('con dos turnos del mismo carnet ese dia cancelaba el primero que encontraba... corriendo solo es un desastre'). Pero cambiar_jugadores tiene EXACTAMENTE el mismo matching (fecha+club, sin hora, linea 676) y tambien CANCELA (linea 696) antes de re-tomar — y el cambio programado (_ejecutar_cambio_prog) corre solo y sin nadie mirando, igual que la cancelacion programada. Con dos turnos confirmados del mismo reservador el mismo dia, el cambio de carnets del turno de las 17 puede cancelar y re-tomar el de las 15. La funcion recibe hora_desde/hora_hasta como parametros y no los usa para ubicar la reserva. El comentario de webapp.py:970-974 reconoce el problema pero solo lo guarda para el caso 'reserva no confirmada'.

**Fix:** Portar a cambiar_jugadores el mismo criterio de bf1cad8: filtrar mis_reservas por fecha+club+hora_to==hora_hasta, aceptar el unico si hay uno solo ese dia, y devolver no_encontrada sin cancelar si hay varios y ninguno coincide. Idealmente extraer el matching a un helper compartido con _cancelar_en_ondepor.

### G3 · [ALTO] POST /reservas/vigilar sin guard de estado: sobre una reserva confirmada arma la caza de una SEGUNDA cancha
**Dónde:** `webapp.py:2984` · **Esfuerzo:** minutos

reserva_vigilar (2970-2989) no valida el estado y es un toggle (`nuevo = not obj.get('vigilar')`). Flujo real: la vigilancia caza una cancha (estado pasa a reservada, vigilar=False), el usuario tiene la pagina vieja con 'Dejar de vigilar' y lo toca -> el toggle REACTIVA vigilar y set_estado(rid,'programada') pisa el 'reservada'. En <=12 s el manager ve programada con apertura pasada y lo marca 'vencida' (1011-1012). Resultado: una reserva CONFIRMADA queda como vencida (la limpieza de 1 h ya no la borra nunca, el mensaje del finde la lista 'a confirmar') y _revisar_vigilancias (837 solo saltea 'reservada') dispara _cazar_cancelacion cada 4 min, que puede tomar una SEGUNDA cancha para el mismo turno. El boton solo se muestra en fallada/vencida, pero el endpoint acepta cualquier estado.

**Fix:** En reserva_vigilar: rechazar activar si get_estado(rid) in ('reservada','en_curso'); no tocar el estado al desactivar; y mandar el estado deseado en el form (name=on/off) en vez de togglear, para que la pagina vieja no invierta la intencion.

### G4 · [ALTO] Timeout en el paso CANCELAR de cambiar_jugadores: la cancha puede perderse sin revert ni aviso veraz
**Dónde:** `ondepor_core.py:696` · **Esfuerzo:** horas

cancelar_reserva postea con timeout=20 y sin retry. Si OnDepor procesa la cancelacion pero la respuesta se pierde (timeout / 502 / respuesta no-JSON), la excepcion sube y _hacer_cambio_jugadores la caza en webapp.py:2667 devolviendo 'error': no se intenta ni la re-toma ni la red de seguridad (esas solo corren cuando el cancel devolvio success), reservas.json y el estado quedan 'reservada', y el aviso dice 'El cambio de carnets NO salio' — que implica que no cambio nada, cuando el turno puede estar ya cancelado y la cancha en el mercado. Igual con status 'error_http' (657), que se reporta como fallo_cancelar 'la reserva quedo como estaba' sin verificarlo. En el cambio_prog esto corre solo 10 min antes del turno: nadie lo ve hasta llegar al club.

**Fix:** Ante excepcion o respuesta ambigua del cancel, consultar mis_reservas(): si el turno ya no esta, saltar directo a la re-toma con los nuevos y, si falla, a la red de seguridad con los viejos; recien si tampoco, devolver 'perdida' (no 'error'). Distinguir en el mensaje 'no toque nada' de 'cancele y no se si retome'.

### G5 · [ALTO] Crear una reserva con mismos datos pisa en silencio la reserva de OTRO usuario y le borra el estado confirmado
**Dónde:** `webapp.py:2570` · **Esfuerzo:** minutos

En _guardar_reserva_desde_form, remove_ids = {rid} | {edit_orig} y se filtra la lista sin chequear dueño ni estado del rid nuevo. El chequeo de permiso (linea 2565) solo cubre edit_orig. Si un usuario (rol jugador) carga una reserva con el mismo reservador+fecha+hora+cancha+club que una existente de otro usuario, la reemplaza sin aviso: se pierde el creado_por original y en las lineas 2593-2596 se hace pop del estado, o sea que una reserva ya RESERVADA vuelve a 'programada' y el tracking del turno confirmado se esfuma.

**Fix:** CORREGIR: antes de filtrar, si existe un objeto con id==rid que no es el que se esta editando, rechazar con ValueError ('ya hay una reserva identica cargada por X'); y nunca pisar un rid cuyo get_estado() sea 'reservada' o 'en_curso'.

### G6 · [ALTO] _cancelar_en_ondepor: el fallback 'uno solo ese dia' puede cancelar un turno que NO es el del bot
**Dónde:** `webapp.py:2955` · **Esfuerzo:** minutos

Se matchea por hora_to == hora_hasta (bien), pero si no matchea y hay exactamente 1 turno ese dia en ese club, se lo cancela igual (lineas 2955-2956). Que no haya matcheado por hora significa justamente que ESE turno termina a otra hora: no es el de la reserva del bot (p.ej. el turno del bot ya fue cancelado a mano y el reservador tiene otro turno personal ese dia). Con la cancelacion PROGRAMADA esto corre solo, sin nadie mirando, y se lleva un turno ajeno — el mismo desastre que el comentario de la linea 2947 dice haber arreglado.

**Fix:** CORREGIR: eliminar el fallback de las lineas 2955-2956; si ningun turno termina a la hora esperada, devolver 'no encontre ese turno' (ya existe ese mensaje en la linea 2961).

### G7 · [ALTO] Bug conocido (dos jobs vivos al editar): el punto exacto es que el alta/edicion no mira si el estado es 'en_curso'
**Dónde:** `webapp.py:2564` · **Esfuerzo:** minutos

Profundizacion del bug ya conocido: el manager snapshotea jobs al inicio del ciclo y larga el hilo con el dict viejo (linea 1016); _guardar_reserva_desde_form genera un rid NUEVO (el id codifica reservador/fecha/hora/cancha), borra el estado del rid viejo (2593-2596) y el nuevo nace 'programada', asi que el proximo ciclo lo dispara tambien: dos hilos reservando a la vez en la ventana de ~3.5 min.

**Fix:** CORREGIR: en _guardar_reserva_desde_form, si edit_orig y get_estado(edit_orig) == 'en_curso' (o falta menos de VENTANA_PRE_SEG para su apertura), rechazar la edicion con ValueError ('la reserva esta por disparar, esperala'); es un if de 3 lineas dentro del lock.

### G8 · [ALTO] Timeout/error de red en el confirm se trata como rechazo definitivo (corta con 40 reintentos sin usar)
**Dónde:** `ondepor_core.py:440` · **Esfuerzo:** minutos

_confirmar_reg convierte cualquier excepción de red (Read timeout de 20 s, connection reset) en {"status":"error","errors":str(e)}. _es_ocupada da False sobre ese texto, así que reservar() retorna en la línea 401 ("error que NO es de cancha: cortar"). En disparar() (reservar_bot.py:394-412) ese texto no matchea "refresca/intenta/ocurrió un error" → se clasifica RECHAZO definitivo y hace return False. Un solo hipo de red en el segundo crítico de la apertura mata la corrida entera con los 40 reintentos intactos. Lo mismo pasa con status "error_http" (confirmar devuelve no-JSON, ej. 500 o 302).

**Fix:** En reservar_bot.py:396 sumar los estados "error"/"error_http" (o los textos "timed out"/"connection") al criterio de 'transitorio' para que reintenten; alternativamente, en _confirmar_reg distinguir excepción de red (retryable) de respuesta de OnDepor.

### G9 · [ALTO] Turno de 23:00: la web guarda hora_hasta "00:00" y OnDepor lo rechaza siempre ("reservá al menos 1 hora")
**Dónde:** `webapp.py:2573` · **Esfuerzo:** minutos

fin = datetime(2000,1,1,hh,mm)+1h y luego fin.strftime("%H:%M") produce "00:00" para hora=23:00, que la sede noche SÍ ofrece en el form (webapp.py:60, validada en 2535). Nada normaliza a "24:00": armar_trabajo (reservar_bot.py:150) y confirmar() pasan hora_hasta tal cual. El propio código documenta que 00:00 falla: reservar_bot.py:257 ("23:00 -> 24:00 (no 00:00)") y test_bot.py:244 ("poner 00:00 da 'reservá al menos 1 hora'"). Toda reserva de 23:00 creada por la web fracasa el confirm de forma determinística a la apertura.

**Fix:** Al armar hora_hasta usar "24:00" cuando hh+1==24 (mismo criterio que fin() de reservar_bot.py:257), y defensivamente en OnDepor.confirmar/canchas_disponibles convertir time_to "00:00"→"24:00".

### G10 · [ALTO] Credencial de admin por defecto "cambiame" (A07/A05)
**Dónde:** `webapp.py:84` · **Esfuerzo:** minutos

El fallback de contraseña del panel es el literal "cambiame" (linea 84) y el sembrado inicial crea al usuario admin 'diego' con esa clave (linea 243). Verificado en el entorno local: usuarios.json tiene UN solo usuario, login 'diego', admin=True, clave == "cambiame" (8 chars). Ojo: una vez sembrado, definir PANEL_PASSWORD por env NO cambia la clave guardada (login valida contra usuarios.json, no contra PASSWORD), asi que si el primer arranque en produccion uso el default, el panel expuesto por Caddy/HTTPS queda con admin diego/cambiame. Esto profundiza (no repite) el item ya conocido de claves en plano: no es el storage plano lo que reporto sino el VALOR debil por defecto.

**Fix:** Cambiar ya la clave del usuario diego en produccion. Ademas: abortar el arranque (o forzar cambio en el primer login) si la clave activa == "cambiame" o si no hay PANEL_PASSWORD/clave fuerte configurada. Confirmar el usuarios.json de produccion.

### G11 · [ALTO] El auto-refresh de /reservas pisa el formulario a medio llenar
**Dónde:** `webapp.py:2416` · **Esfuerzo:** minutos

tick() corre cada 1 s y, si CUALQUIER reserva está a <20 s de su apertura, agenda location.reload() a los 12 s. Solo se saltea si la URL trae ?editar/?cambiar/?programar — una reserva NUEVA no tiene query param. Un usuario eligiendo reservador, fecha y 3 jugadores a las 21:59 (hora típica de aperturas) pierde todo lo tildado sin aviso cuando la página se recarga sola. Además el setTimeout se re-agenda en cada tick (hasta 12 timeouts apilados).

**Fix:** Marcar el form como 'sucio' con un listener de input/change y saltear el reload si hay datos cargados (o refrescar solo las listas vía fetch en vez de recargar la página entera). Bonus: agendar el reload una sola vez con un flag.


## Hallazgos MEDIOS

| Dónde | Qué | Fix propuesto | Esfuerzo |
|---|---|---|---|
| `webapp.py:1018` | Sin interlock entre cambio_prog y cancel_prog sobre la misma reserva: pueden disparar a la vez | Al programar la cancelacion, rechazar (o anular con aviso) un cambio_prog pendiente y viceversa; y como cinturon, que _revisar_cancel_prog saltee reservas con cambio_prog en estado en_curso (y al reves) dentro del mismo  | horas |
| `encuesta.py:606` | reservas_existentes no excluye 'cancelada': un turno cancelado sigue comprometiendo carnets en el armador | Agregar 'cancelada' al filtro de la linea 606. Buscar con grep todos los `in ("fallada", "vencida")` / tuplas de estados del proyecto y decidir caso por caso donde entra 'cancelada', para que no vuelva a quedar afuera. | minutos |
| `webapp.py:916` | cancel_prog nacio copiando a cambio_prog en vez de compartir: 3 pares de funciones gemelas | Extraer _set_prog(rid, campo, estado, detalle), un esqueleto comun de barrido (vencimiento -> hora -> estado -> en_curso+hilo) parametrizado por campo y ejecutor, y una ruta generica de borrar-prog. Hacer que reserva_pro | horas |
| `webapp.py:986` | cambio_prog/cancel_prog quedan 'en_curso' para siempre si el proceso se reinicia en el medio | Al arrancar (junto a _cargar_estados) o en cada revisor: todo cambio_prog/cancel_prog 'en_curso' cuyo ej_epoch quedo atras se pasa a 'pendiente' (si el turno no empezo) o a 'fallado' con detalle 'quedo a medias por un re | minutos |
| `webapp.py:837` | La vigilancia solo saltea 'reservada': con el disparo principal 'en_curso' puede tomar una cancha en paralelo | En 837 saltear estado in ('reservada','en_curso'); re-chequear el estado dentro de _cazar_cancelacion justo antes de rb.disparar; y usar un set de rids 'cazando' (agregar al entrar, sacar en finally) en lugar de solo el  | minutos |
| `webapp.py:2994` | Borrar una reserva 'en_curso' no frena el disparo: la cancha se toma igual y queda sin registro en el panel | En reserva_borrar: si get_estado(rid) == 'en_curso', rechazar con 'se esta disparando ahora: espera el resultado y despues cancelala'. Es el espejo barato del guard que falta en _guardar_reserva_desde_form. | minutos |
| `webapp.py:2428` | XSS reflejado: ?programar=<payload> se inyecta sin escapar en el value del hidden | CORREGIR: pasar programar_id (y cambiar_id/editar_id de la linea 2426 por consistencia) por esc(), y ademas vaciar programar_id cuando no exista una reserva con ese id (hoy solo se vacia por permiso o estado). | minutos |
| `webapp.py:2625` | 'Reservar ahora' sobre un turno cuya apertura no llego mata el disparo automatico | CORREGIR: antes de _disparar_job, si hora_real() < jobs[0]['apertura_epoch'], no disparar: flash 'ese turno abre recien el ... — quedo programada' y dejarla en 'programada'. | minutos |
| `encuesta.py:432` | Los votos de participantes sacados de la encuesta siguen contando para el cronograma | CORREGIR: en el loop de la linea 432 filtrar pid contra el set de ids de _jugadores(e) (los carnets_extra ya tienen su canal propio). | minutos |
| `encuesta.py:118` | esta_abierta hardcodea que el server corre en UTC (+3*3600 sobre timestamp de un naive) | CORREGIR: comparar con _epoch_ar(ahora_ar()) < cierre + 1 (ya esta importado en la linea 35), sin el +3*3600. | minutos |
| `webapp.py:3568` | Abrir la encuesta viernes/sabado/domingo genera cierre y finde inconsistentes | CORREGIR: derivar todo del MISMO finde objetivo: sab = _proximo(5), dom = sab + 1 dia, y cierre = min(jueves proximo, viernes anterior al sab) o directamente sab - 2 dias a las 23:59. | minutos |
| `webapp.py:3013` | reserva_borrar saca la reserva de la lista aunque la cancelacion en OnDepor haya FALLADO | CORREGIR: si el mensaje no empieza con exito, NO borrar: dejar la reserva con una nota de 'cancelacion fallo, reintenta o hacelo a mano' (y boton para forzar el borrado solo-local). | minutos |
| `webapp.py:1821` | Renombrar un usuario del panel rompe el vinculo con sus reservas y sus push, y permite usuarios duplicados | CORREGIR: al renombrar, actualizar creado_por en reservas/recurrentes y la clave en push_subs bajo el mismo lock; y rechazar el guardado si el usuario nuevo ya existe en otro indice. | horas |
| `webapp.py:2549` | Alta de reserva no valida que los player_ids ni el reservador existan en la base (solo el navegador lo garantiza) | CORREGIR: replicar en el alta los dos chequeos de _validar_cambio_form: set(jugadores) <= conocidos y pid del reservador not in jugadores. | minutos |
| `ondepor_core.py:419` | _es_ocupada lee la respuesta contaminada con _intentos: errores de cupo/jugador se clasifican como "cancha ocupada" en el loop secuencial | Evaluar _es_ocupada(r2) ANTES de colgarle _intentos/_canchas_ofrecidas, o cambiar _es_ocupada para que mire solo los campos errors/message/error de la respuesta cruda. | minutos |
| `ondepor_core.py:83` | Si el sync NTP forzado falla, el spin de precisión bloquea hasta 3 s por iteración reintentando NTP | En el except, setear _ntp_ts = time.monotonic() (conservando el último offset conocido) o guardar un timestamp de 'último intento fallido' con backoff, para que el spin nunca vuelva a pegarle a la red. | minutos |
| `reservar_bot.py:363` | Confirm que entra en OnDepor pero cuya respuesta se pierde (timeout) dispara el flujo normal de nuevo: doble reserva sin detección | Ante un status error/error_http proveniente de un confirm (no de abrir formulario), consultar api.mis_reservas() filtrando fecha+hora y cortar como éxito si la reserva ya figura, antes de caer al flujo normal. | horas |
| `ondepor_core.py:466` | _confirmar_paralelo espera a TODOS los hilos: una request colgada demora 20 s el resultado y revienta el límite del tick | Usar concurrent.futures.as_completed: al primer success retornar (o al menos loguearlo) y dejar que los rezagados terminen en background solo para detectar _multiple; bajar el timeout de los confirms del tick a ~5 s. | horas |
| `ondepor_core.py:472` | Éxitos múltiples del paralelo solo generan un log ("cancelá a mano"); cambiar_jugadores directamente descarta el aviso | Tras un éxito con _multiple, resolver los reservation_id de las canchas sobrantes vía mis_reservas() y auto-cancelarlas con cancelar_reserva(), dejando la pedida (primera del orden); propagar _multiple en el retorno de c | horas |
| `ondepor_core.py:244` | Sesión vencida (302) indistinguible de "todavía no hay canchas": no existe re-login en todo el disparo | Detectar r.status_code in (301,302) o content-type HTML en los POST de /player y ejecutar self.login() una vez antes de reintentar; en disparar, ante N "sin_cancha" seguidos forzar re-login. | horas |
| `ondepor_core.py:499` | La pre-carga no tiene presupuesto de tiempo: con OnDepor lento el spin arranca después de la apertura | Pasarle a precargar_objetivo un deadline (apertura_epoch - 2 s): chequearlo antes de cada fuente y abortar devolviendo None; usar timeout de 4-5 s en las requests de pre-carga en lugar de 20 s. | minutos |
| `webapp.py:1037` | A01: la sesion no se revalida (borrar/degradar usuario no corta el acceso) | En login_requerido/permiso_requerido revalidar contra usuarios.json cada request (que el usuario exista y refrescar session['rol'] desde el archivo), o llevar una version de sesion / lista de revocacion que invalide cook | horas |
| `webapp.py:2157` | Falladas/vencidas/canceladas quedan para siempre bajo el título "Reservas programadas" | Sección propia ("Terminadas") o limpiar también fallada/vencida/cancelada N horas después del turno — la fallada ya está en historial vía _registrar_resultado, no se pierde nada. Registrar la vencida en el historial al v | horas |
| `webapp.py:1933` | _leer_log abre un archivo por fila en cada GET de /reservas (y en el mensaje del finde) | _registrar_resultado (607-610) ya calcula cancha_real y detalle al terminar el disparo: guardarlos en estados.json junto al estado (set_estado ya persiste 'detalle') y que _fila_reserva y _mensaje_finde lean de ahí; el l | horas |
| `webapp.py:1994` | Botones "anular" de ~26 px de alto: bajo el mínimo táctil de 44 px | Subir los 'anular' a min-height:40-44px con padding 8px 14px (entran igual en la nota con flex-wrap), y llevar los compacta de 40 a 44 px. Es solo CSS/atributos. | minutos |
| `webapp.py:2625` | "Reservar ahora" corre sincrónico: hasta ~40 reintentos bloqueando el request y un thread de waitress | Para el modo 'ya' bajar reintentos a ~5 (el turno del día o está libre o no está), o dispararlo en un hilo y volver con flash 'reservando…' + el estado en_curso ya visible en la lista (que se actualiza sola). | horas |
| `encuesta.py:398` | La encuesta pública responde el POST del voto con HTML directo (sin redirect): re-envío al refrescar | Patrón PRG: tras guardar el voto, redirect a url_for('encuesta.ver', token=token, ok=1) y mostrar ahí la confirmación con el resumen (el voto ya guardado se puede releer para armarlo). | minutos |

## Hallazgos BAJOS

| Dónde | Qué | Fix propuesto | Esfuerzo |
|---|---|---|---|
| `webapp.py:909` | El push de la cancelacion programada rompe dos convenciones de f7fb3b9: sin emoji y sin avisar al creador de la reserva | En _ejecutar_cancel_prog (y _ejecutar_cambio_prog) notificar a rsv['creado_por'] ademas de cp['creado_por'] cuando difieren, diciendo quien lo programo, y alinear los titulos con emoji (🚫/⚠️) como el resto. | minutos |
| `encuesta.py:1110` | Docstring de _sugerencias quedo obsoleto tras MAX_ROTAN=3 | Cambiar el docstring a 'donde rotan mas de MAX_ROTAN' (o 'mas de 3') para que no vuelva a desincronizarse si el tope cambia de nuevo. | minutos |
| `webapp.py:3440` | Parametro 'excluir' de _pantalla_participantes quedo muerto tras 'Sumar carnets' | Eliminar el parametro excluir y el filtrado asociado de _pantalla_participantes. | minutos |
| `webapp.py:2855` | Resto de copy-paste en la ruta de programar cancelacion: parametro rid muerto | Sacar rid=None de la firma y leer directo request.form['cancelar_prog_id']. | minutos |
| `webapp.py:2625` | Carrera reserva_ya vs manager: ventana sin estado entre guardar y marcar 'en_curso' puede duplicar el disparo | En reserva_ya, set_estado(rid,'en_curso') inmediatamente despues de _guardar_reserva_desde_form (antes de buscar_trabajos); _disparar_job ya lo re-setea sin dano. Cero costo y cierra el hueco. | minutos |
| `webapp.py:3236` | recurrente_guardar no valida la cancha contra la sede (canchas 9-11 no existen de noche) | CORREGIR: copiar el chequeo canchas_validas del alta normal (3 lineas) antes de appendear. | minutos |
| `webapp.py:905` | Exito de cancelacion detectado por el emoji del mensaje (msg.startswith('✅')) | MEJORAR: que _cancelar_en_ondepor devuelva (ok: bool, msg: str) y los llamadores usen el bool. | minutos |
| `webapp.py:2530` | Variable local hoy_ar pisa la funcion importada y duplica su logica hardcodeando UTC-3 | ELIMINAR la reimplementacion: usar d < hoy_ar() con la funcion importada (renombrando la local). | minutos |
| `encuesta.py:222` | Codigo muerto: 'juega' asignada y nunca usada en la pagina de la encuesta | ELIMINAR la linea 222. | minutos |
| `webapp.py:707` | El formato del id de reserva esta duplicado entre recurrentes y el alta (si divergen, se duplican reservas) | MEJORAR: extraer una funcion _rid(reservador, fecha, hora, cancha, club) (y opcionalmente un _nueva_reserva(...)) usada por los dos caminos. | minutos |
| `webapp.py:677` | DIAS_SEMANA duplica enc.DIAS y zona_de duplica las zonas ya declaradas en SEDES | ELIMINAR DIAS_SEMANA (usar enc.DIAS) y MEJORAR zona_de para derivar el mapa numero->zona desde SEDES al importar. | minutos |
| `webapp.py:618` | Parseo de la cancha real ('05-KINERET' -> '5') repetido 6 veces con variantes | MEJORAR: una funcion num_cancha(real) -> str usada por todos. | minutos |
| `reservar_bot.py:389` | "no_abierto"/"sin_cancha" consumen los mismos 40 reintentos que los intentos reales: apertura demorada agota el presupuesto en ~1-2 minutos | No descontar del presupuesto los estados no_abierto/sin_cancha (contarlos aparte con un tope de tiempo, ej. 10 min desde la apertura), reservando los 40 reintentos para intentos que llegaron a confirmar. | minutos |
| `ondepor_core.py:464` | Las sesiones clonadas del paralelo nunca se cierran: fuga de conexiones en el proceso de larga vida | En intentar(): s = self._clonar_sesion(); try: return self._confirmar_reg(..., sess=s) finally: s.close(). | minutos |
| `ondepor_core.py:204` | _leer_tokens depende del orden exacto de atributos del HTML y falla en silencio conservando el csrf viejo | Regex tolerante al orden (buscar name="csrf-token" y content="..." en cualquier orden dentro del tag <meta>) y loguear explícitamente cuando el token no aparece en el HTML. | minutos |
| `webapp.py:123` | CSRF: forms POST sin token (mitigado por SameSite=Lax) + efectos de estado en GET | Agregar token CSRF (flask-wtf o token firmado en sesion validado en cada POST). Pasar las acciones con efecto (cronograma-cfg, logout) a POST para no mutar estado en GET. | horas |
| `webapp.py:3076` | DOM-XSS en el buscador de Jugadores (innerHTML con datos de OnDepor) | Construir los <li> con createElement/textContent en vez de innerHTML, o escapar j.nombre/d.error antes de insertarlos. | minutos |
| `webapp.py:1230` | /salud sin auth expone metricas operativas | Devolver solo {ok:true/false} y el latido para el monitor externo; mover el conteo de reservas y demas metricas detras de login o de un token de monitoreo. | minutos |
| `webapp.py:2148` | /reservas muestra TODAS las reservas a cualquier usuario logueado | Si se busca privacidad entre usuarios, filtrar el listado a las propias salvo permiso 'todas_reservas', igual que hace historial() (lineas 4011-4013). | horas |
| `encuesta.py:320` | Encuesta publica: se puede votar como cualquier participante | Aceptable por diseno. Si importa la integridad del voto: links personalizados por participante (token por persona) o un PIN corto al identificarse. | horas |
| `estilo.css:119` | var(--texto-suave) no existe: tres lugares pierden el gris suave | Reemplazar las 3 apariciones por var(--texto-2) (o definir --texto-suave: #5c6470 en :root como alias). | minutos |
| `webapp.py:2505` | /reservas consulta /api/push/clave dos veces en cada carga | Un solo fetch al cargar la página que resuelva ambas cajas (guardar la promesa en una variable y que los dos bloques la consuman). | minutos |
| `webapp.py:4033` | El historial corta en 100 filas sin decirlo | Agregar al pie '<small>mostrando las últimas 100 de N</small>' (minutos) o un ?todos=1 / paginado simple por offset. | minutos |
| `webapp.py:2382` | Acentos perdidos en textos generados por JS (alert y optgroups al cambiar de sede) | Unificar los textos: 'acompañantes', 'tenés', 'Cancha específica' en los 3 strings JS (los f-strings aguantan UTF-8 sin problema, ya se usa en el resto). | minutos |
| `webapp.py:1189` | Login sin autocomplete: los gestores de claves y el autofill del celular no enganchan bien | Agregar autocomplete="username" al input usuario y autocomplete="current-password" al de password; en el alta de usuarios (1779) usar autocomplete="new-password" para que el navegador no autocomplete la clave del admin. | minutos |
| `webapp.py:2203` | En modo Cambiar/Programar los radios de sede quedan habilitados y desarman los selects bloqueados | Sumar {dis} a los radios de sede igual que a los selects (una palabra en el f-string de radios_sede). | minutos |
| `webapp.py:4180` | El resultado de "Probar" login del reservador aparece abajo de toda la lista | Pintar el resultado en la propia fila (junto al chip ok/sin clave) o hacer scrollIntoView() sobre #res_estado al recibir la respuesta. | minutos |
| `webapp.py:2745` | El POST de cambio de jugadores carga los 3 JSON dos veces | Que _validar_cambio_form devuelva también jug_db/res_db ya cargados y _hacer_cambio_jugadores los acepte como parámetros opcionales. | minutos |

## Comparación con auditorías anteriores
Primera auditoría del proyecto. Los dos bugs cazados en producción esta semana (cancha pedida perdida sin reintento; mensaje "¿svid/sesión?" tapando el motivo real) ya estaban corregidos antes de esta corrida y no reaparecen.

## Plan de acción propuesto (en orden)
1. **HOY, minutos** — G2: agregar `'cancelada'` a los estados que el manager saltea + sacar el botón Vigilar de filas canceladas/vencidas confirmadas. Es el único que corrompe estado en el 100% de los casos de la funcionalidad nueva.
2. **HOY, minutos** — G4/G8: guard de estado en `/reservas/vigilar` y en el alta/edición (rid en uso con estado `reservada`/`en_curso` → error, no pisar). Cierra también el bug conocido de los dos jobs vivos.
3. **HOY, horas** — G1: matching por hora en `cambiar_jugadores` (helper compartido con `_cancelar_en_ondepor`) y endurecer el fallback "único del día" (G7): exigir hora o no cancelar. Es la acción destructiva que corre sola.
4. **Esta semana, horas** — G5/G9: errores de red ≠ rechazo definitivo (retry en `cancelar_reserva`, clasificar timeout como transitorio en el confirm, y verificación post-cancel con `mis_reservas` antes de declarar "quedó como estaba").
5. **Esta semana, minutos** — G10 (23:00→"24:00"), G11 (auto-refresh con dirty-check del form), requirements completo.
6. **Cuando toque** — G12 password por defecto (ligado al pendiente eterno de cifrar claves), medios de a tandas empezando por los de flujos (interlock cambio/cancel, en_curso huérfano tras reinicio, vigilancia vs en_curso), después UX (44px, autocomplete, historial), después limpieza (duplicaciones, código muerto).

*Nada de esto se tocó: informe primero, Diego decide.*
