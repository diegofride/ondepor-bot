# Auditoría integral: Reservame ésta! (bot de reservas de pádel)

- **Fecha:** 2026-09-24.
- **Código auditado:** `f660315`, que es `origin/main` del 02/09, lo último que hay en GitHub.
  - ⚠ El `main` de esta PC está 4 commits atrás, en `56424f9`. Antes de tocar nada, hacer `git pull`.
- **Producción:** el 24/09 a las 14:06, `/salud` respondía `reloj_interno: vivo`, `push: true` y 1 reserva pendiente.
- **Método:** superpowers (`dispatching-parallel-agents`, `requesting-code-review` y `verification-before-completion`).
  - Seis revisores trabajaron en paralelo y en solo lectura sobre una copia aislada: seguimiento de auditorías, motor, estados y concurrencia, seguridad OWASP 2025, revisión de los 4 commits del 02/09, y encuesta/tests.
  - **Cada hallazgo crítico se reprodujo offline una segunda vez**, con la red bloqueada a nivel socket. Nada tocó OnDepor.
  - Los 166 tests pasan en `f660315`, en 7 s.
- **Antecedentes:** esta es la tercera auditoría del proyecto. Vienen después de la [auditoría integral del 18/08](auditoria-2026-08-18.md) y de la de arquitectura del 20/08 (`../AUDITORIAS/arquitectura-ondepor-2026-08-20.md`).

## Resumen

| | |
|---|---|
| Graves del 18/08 | **0 arreglados**, 1 parcial (G2), 10 sin arreglar |
| Medios del 18/08 | 1 arreglado (esta_abierta), 2 parciales, 24 sin arreglar |
| Etapa 1 de arquitectura | 5 de 6 hechos. El push de "vencida" nunca sale |
| Etapa 2 de arquitectura | parcial. El logging trajo el crítico N1 |
| **Críticos nuevos** | **6 confirmados + 1 condicional a los datos del server** |
| Importantes nuevos | 14 |

El trabajo del 20/08 fue en la dirección correcta: reconciliación con OnDepor, `ubicar_turno` único, latido de /salud, backups completos y motor único de acciones. La migración a Linux y el paso a "un solo club" funcionan para datos nuevos.

Pero la lista del 18/08 quedó casi entera sin tocar. Y el paso de `print()` a `logging` dejó una trampa: **el hilo que dispara las reservas se muere ante el primer error, sin avisar y sin que systemd lo levante**.

---

## Paso 0: qué hacer hoy

1. **Sacar `flush=True` de `webapp.py:582` y de `webapp.py:1168`.** Es el crítico N1, se arregla en minutos.
2. **En el server:**
   - `journalctl -u <servicio> | grep "unexpected keyword argument 'flush'"`: muestra si el reloj ya se murió alguna vez.
   - `grep -c '"club_id": 504' recurrentes.json reservas.json`: si da más de 0, N7 es crítico.
   - Confirmar que la clave del usuario `diego` en el `usuarios.json` del server **no** sea `cambiame`. El código la siembra en el primer arranque si no hay `PANEL_PASSWORD` (webapp.py:140/147/305).
3. `git pull` en la PC.

---

## Hallazgos CRÍTICOS (todos reproducidos)

### N1 · El reloj interno se muere ante cualquier error, y nadie lo levanta

- **Dónde:** `webapp.py:582` (`notificar`) y `webapp.py:1168` (el `except` del ciclo del manager). Viene del commit 96115c8 del 20/08.
- **Esfuerzo:** 10 min el arreglo, 30 min con red y test.

**Qué pasa**

Los dos lugares llaman al logger con `flush=True`, y `Logger._log()` no acepta ese argumento, así que tira `TypeError`.

- `notificar()` revienta **después** de mandar cada push, siempre que el push esté activo y el usuario tenga un dispositivo.
- El `except` del manager vuelve a reventar al querer loguear el error. Eso termina el `while True`.
- El proceso waitress sigue vivo, así que `Restart=always` no hace nada. La única señal es /salud en 503.

Reproducido así: `cargar_todo` falla una vez y queda `hilo manager vivo: False` después de 1 ciclo.

**Qué lo dispara en la vida real** (4 de 6 revisores llegaron a esto por su cuenta)

- **Una recurrente que se carga para alguien con push.** Se ejecuta desde adentro del ciclo del manager (`webapp.py:798`). Con N2, esto pasa todas las semanas.
- **Una reserva "rota" dentro de las 24 h** (`_avisar_reservas_rotas`, 845). La marca `aviso_problema` se escribe **después** del push, así que nunca queda guardada. Resultado: **se muere en cada reinicio del servicio** y repite el mismo push cada vez.
- **Acciones programadas** (1064, 1108), **reconciliación** (944, en su propio hilo) y **cualquier otra excepción** del ciclo, aunque no tenga que ver con push.

**Rutas web que actúan en OnDepor y después devuelven 500**

- **Cancelar un turno confirmado:** cancela en OnDepor, devuelve 500 y la fila sigue "Reservada". El reintento vuelve a cancelar en OnDepor, y con el fallback de N5 se puede llevar otro turno.
- **Cambiar jugadores:** el cambio sale bien pero la respuesta es 500.
- **El botón "Activar avisos":** también da 500.

**Por qué no lo agarró la suite:** el único test de `notificar` no tiene suscripciones (test_bot.py:890).

**Hoy en producción:** el reloj está vivo, pero con `push: true` las condiciones para que se caiga están dadas.

**Arreglo**
- Sacar `flush=True` de las dos líneas.
- En 1168, usar `registro("manager").exception("error en el ciclo")`.
- Envolver cada tarea auxiliar del ciclo en su propio try.
- Como última red: si `_manager` termina, `os._exit(1)`, para que systemd reinicie el proceso.
- Test de `notificar` con una suscripción y `webpush` simulado.

### N2 · Las recurrentes "resucitan" la reserva jugada o borrada a mano

- **Dónde:** `webapp.py:768-780`.
- **Esfuerzo:** ~1 h.

**Qué pasa**

Para no duplicar, `_generar_recurrentes` solo mira si el id existe en `reservas.json`. El mismo día del turno, `faltan = 0`, así que `fecha = hoy`.

- **Después de jugar.** El turno de las 20 se confirma y se juega. A las 21, la limpieza lo borra. En el ciclo siguiente se vuelve a crear para hoy y queda "vencida" para siempre. Encima manda otro push "🔁 Reserva recurrente cargada", que con N1 mata el reloj.
  - Pasa todas las semanas con toda recurrente de hasta las 22 h.
- **"Esta semana no jugamos".** Se borra con la X la ocurrencia ya cargada. A los 12 s vuelve a aparecer, "programada", y **se dispara sola y toma una cancha que nadie quería**.
  - El confirm de la X dice "todavía no reservó nada", como si borrar alcanzara.
  - La única salida hoy es pausar la recurrente (`/recurrentes/pausar`) y acordarse de reactivarla después.

Reproducido: en el caso "después de jugar", aparece `ciclo siguiente: programada → vencida` con 2 pushes de "recurrente cargada". En el caso "esta semana no jugamos", la reserva borrada reaparece `programada` 12 s después.

**Arreglo:** guardar en la recurrente las fechas ya generadas (`rec["generadas"]`, bajo el mismo lock) y no regenerar si la fecha está ahí o si el turno ya pasó. Borrar la ocurrencia equivale entonces a "saltear esta semana".

### N3 · Un solo error en el login hace perder la apertura entera

- **Dónde:** `reservar_bot.py:312-317`.
- **Esfuerzo:** 15 min.

**Qué pasa:** el login se hace unos 3,5 min antes (`VENTANA_PRE_SEG=210`). Si falla una sola vez (un timeout o un 502 a las 21:57, que es hora pico de OnDepor), `disparar()` devuelve False y la reserva queda "fallada". Quedaban más de 3 minutos para reintentar.

Reproducido: `disparar() -> False | intentos de login: 1 | faltaban 210 s para la apertura`.

**Arreglo:** reintentar el login cada ~10 s hasta `apertura − 45 s`, y hacer un último intento a T−5.

### N4 · Cambio de jugadores: un error de red después de cancelar pierde la cancha, y el aviso miente

- **Dónde:** `ondepor_core.py:730-754` y `webapp.py:2888`. Amplía G4.
- **Esfuerzo:** 20-40 min.

**Qué pasa**

El paso 4 (re-tomar) y el 5 (red de seguridad) llaman a `abrir_formulario` y `canchas_disponibles` sin try. Si cualquier POST lanza una excepción después del cancel, se saltean los dos pasos:

- el panel sigue diciendo "Reservada";
- el push dice "El cambio de carnets NO salió", o sea que "no cambió nada";
- en OnDepor, el turno ya está cancelado y libre.

El cambio programado corre 10 min antes del turno: la reconciliación avisa cuando el partido ya empezó.

El paso de cancelar sigue sin verificación (G4): un `error_http` se reporta como "quedó como estaba" sin comprobarlo.

Reproducido: `eventos: ['CANCELADO turno 77', 'abrir_formulario #1'] → reintentos y red de seguridad: NO corrieron`.

**Arreglo:** try en cada vuelta de los pasos 4 y 5, contándolo como un intento fallido. Antes de devolver cualquier resultado que no sea éxito, consultar `mis_reservas()` y decir la verdad: "no toqué nada" o "cancelé y no pude retomar".

### N5 · Cancelar o cambiar puede llevarse OTRO turno del mismo día y reportar éxito

- **Dónde:** `ondepor_core.py:167-168`, `737` y `webapp.py:3171-3177`. Es G6, que sigue y ahora también alcanza al cambio de jugadores.
- **Esfuerzo:** 30-45 min.

**Qué pasa**

Si ningún turno coincide en hora, pero la cuenta tiene uno solo ese día, se lo toma igual. Ese fallback sigue vivo en:
- el modo no estricto de `ubicar_turno`, que es el que usa `cambiar_jugadores`;
- `_cancelar_en_ondepor`, que ni siquiera usa `ubicar_turno`.

Además, el paso 4 del cambio re-toma con la hora del **parámetro**, no con la del turno cancelado.

**El cambio a un solo club agravó esto.** Antes, un turno personal de la noche caía en el club 504 y el filtro por club lo dejaba afuera. Ahora es 1102 igual que el turno del bot.

Reproducido: el turno del bot de 17-18 ya fue cancelado por afuera y la cuenta tiene un turno personal de 20-21. El cambio **cancela el de 20-21, toma 17-18 y devuelve `success`**.

Con `hora_to "00:00"`, la red de seguridad calcula `"-1:00"`.

**Arreglo:**
- `estricto=True` en los tres flujos.
- Que `_cancelar_en_ondepor` use `ubicar_turno`.
- En el paso 4, usar la hora real.
- Normalizar `00:00` a `24:00` de los dos lados de la comparación.
- Ajustar los 2 tests que hoy fijan el comportamiento viejo.

### N6 · El armador de cronograma arma planes con el mismo carnet en dos reservas, y `verificar_plan` los aprueba

- **Dónde:** `encuesta.py`.
- **Esfuerzo:** 2-3 h.

Hay cuatro caminos, todos reproducidos con datos inventados:

| Camino | Dónde | Resultado |
|---|---|---|
| Una reserva ya cargada que **empieza dentro** del turno (p.ej. fija el domingo a las 10, que toma el carnet desde el sábado 10:01) | `encuesta.py:577-584` (`libre_desde` solo mira las ventanas que empiezan antes), `999-1002` y `1165` (`verificar_plan` arranca vacío) | El cambio del viernes 10:06 mete a Juan en el sábado a las 10 y el carnet queda en dos reservas. La fija del domingo falla en su apertura |
| Reservador con `player_id` **solo** en db_reservadores | `encuesta.py:622`, `632` | Su reserva cargada no bloquea su carnet y se lo usa de reservador una hora antes |
| Cambio "A MANO" | `encuesta.py:1066-1071`, `1087-1090`, `1190` | Los que entran no se anotan en la agenda y el mismo plan los pone de titulares en otro turno |
| "La dispara un prestado" | `encuesta.py:1036-1080` | El cambio queda con **2** acompañantes (3 carnets). `cambiar_jugadores` **cancela primero** la cancha confirmada y OnDepor pide 4 |

**Arreglo:**
- `libre_desde` tiene que considerar las ventanas que empiezan antes del fin del turno.
- Exigir `libre(cambio_dt, fin)` para los que entran.
- Pasarle a `verificar_plan` las reservas cargadas.
- Resolver el reservador por el mismo criterio que `reservadores_con_carnet`.
- Ocupar en la agenda a los que entran "A MANO".
- Exigir 3 acompañantes en todo cambio.
- Que `mejor_plan` descarte los planes que no pasan la verificación.

### N7 · (Condicional) Reservas y recurrentes guardadas con el club 504 disparan contra un club que ya no existe

- **Dónde:** `webapp.py:773` y `reservar_bot.py:172`. f660315 no migró datos.
- **Esfuerzo:** 20 min.

**Qué pasa:** una recurrente nocturna creada antes del 02/09 genera cada semana una reserva `...-c504` sin marcar `problema`. No sale ningún aviso previo y en el listado se ve igual que una válida. A la apertura, OnDepor contesta "actividad no habilitada" y se pierde la cancha fija.

**Cuándo es crítico:** si el `grep` del Paso 0 da más de 0. En la copia de esta PC (21/08) hay una sola recurrente, del club 1102.

**Arreglo:**
- Migrar una sola vez de 504 a 1102, en recurrentes y en reservas programadas. Ojo: el id lleva `-c504`, así que hay que deduplicar para que no disparen dos.
- En `armar_trabajo`, marcar `problema` si el club no está en `CANCHAS_POR_CLUB` o si pide CHACRA de 18 h en adelante.

---

## Hallazgos IMPORTANTES (nuevos)

| # | Dónde | Qué | Fix | Esfuerzo |
|---|---|---|---|---|
| I1 | `ondepor_core.py:543-561` | **La pre-carga pierde la zona pedida.** Las canchas del "plan B" se nombran `"5"`, sin zona. Se pidió KINERET, el fuente la tenía llena y el tick reservó `01-QUINTA` con KINERET libre en el objetivo (reproducido). El push además informa la cancha pedida, no la real | Nombrar el plan B `"05-KINERET"` y reordenar con `ordenar_canchas(orden, preferida)` | 20 min |
| I2 | `ondepor_core.py:543`, `reservar_bot.py:251-257` | **CHACRA se cuela en la pre-carga de las 18:00.** Solo se filtra la lista del club, no la del fuente: aparecen `10-CHACRA` y `11-CHACRA` en un turno de las 18 (reproducido, 2 revisores). Si OnDepor lo rechaza con otro texto, el tick se corta y pasa al flujo lento | Filtrar `orden` por los court_id válidos a esa hora; testear la **salida** de `precargar_objetivo` (el test actual solo prueba el helper) | 10 min |
| I3 | `reservar_bot.py:260-288` | **Los fuentes de pre-carga cruzan de franja.** Para un objetivo de las 08:00 usa los turnos de hoy de 23 a 18 (noche, sin CHACRA). Nadie verificó que un jid nocturno sirva para una CHACRA de la mañana | Preferir fuentes de la misma franja | 20 min |
| I4 | `webapp.py:912-947` vs `2893-2903` | **La reconciliación cae en el hueco de un cambio de carnets.** Queda "fallada" para siempre, con el push "Te quedaste sin la cancha" seguido de "Carnets cambiados". La cancelación programada ya no corre (exige `reservada`) y la fila ofrece Vigilar. También pasa si el cambio movió el turno a otra cuenta (reproducido, 2 revisores) | Saltear reservas con operación en curso; volver a leer bajo lock antes de degradar; exigir 2 ausencias seguidas; `set_estado(rid,"reservada")` al terminar bien un cambio | 1 h |
| I5 | `ondepor_core.py:652`, `webapp.py:933` | **La reconciliación no ve el caso más común.** `mis_reservas` devuelve `[]` tanto si hubo error como si la lista está vacía, así que el **único** turno de una cuenta que se cae nunca se detecta | Devolver `None` cuando hay error | 10 min |
| I6 | `webapp.py:3120-3152`, `3049-3058`, `4219-4235` | **Anular o reprogramar una acción mientras corre.** El usuario ve "Cancelación anulada: el turno se conserva" y el turno queda cancelado. Reprogramar un cambio en vuelo deja el nuevo como "hecho" sin haberse ejecutado (reproducido) | Rechazar si la acción está `en_curso` | minutos |
| I7 | varias | **Sin exclusión por reserva.** Cambio manual, cancelación manual, cambio y cancelación programados, vigilancia y el reintento tras el 500 de N1 pueden operar a la vez sobre el mismo turno | Un set de "reservas en operación" con lock en `_hacer_cambio_jugadores`, `_cancelar_en_ondepor`, `_cazar_cancelacion` y `_disparar_job` | 30 min |
| I8 | `ondepor_core.py:736-753`, `webapp.py:2892` | **El cambio de jugadores muda de cancha sin avisar.** Si en el hueco se llevaron la original, toma otra. El panel, el push y el mensaje del finde siguen mostrando la vieja; en el caso revertido dice "no cambió nada" | Escribir la cancha nueva en el log de la reserva y en el mensaje | 15 min |
| I9 | `ondepor_core.py:606-608` | **El tick abandona ante "el turno todavía no abrió".** Con OnDepor más de 50 ms atrasado o una muestra NTP mala, se pierde toda la ventaja de la pre-carga | Reintentar ese mismo tiro cada ~80 ms, hasta 1,5 s | 15 min |
| I10 | `webapp.py:1144` | **El push de "vencida" nunca sale**: el job no trae `creado_por`. ⚠ Arreglarlo **después** de G1 y G3, porque si no manda "fijate si la cancha sigue libre" sobre cancelaciones hechas a propósito | Tomar `creado_por` de la reserva | minutos |
| I11 | `encuesta.py:726-745`, `webapp.py:4143-4183` | **El armador planifica turnos cuya apertura ya pasó.** Armado el sábado a las 9: 0 reservas, cuando el domingo entraba. "Desde cero" + Programar sigue habilitado y puede programar carnets en conflicto | Ignorar esos turnos con `ahora_ar()`; bloquear Programar en "desde cero" | minutos |
| I12 | `encuesta.py:491-501` | **`club_de()` sigue devolviendo 504 desde las 18 h.** Hoy no tiene efecto (la encuesta pregunta de 9 a 17), pero el día que se agregue la noche, el mismo carnet queda en dos turnos (2 revisores) | Que devuelva siempre 1102 | minutos |
| I13 | `webconfig.ejemplo.json` | **`secret_key` de ejemplo conocida.** Si alguien copia el ejemplo en vez de dejar que se autogenere, se pueden forjar la cookie de admin y el challenge de passkeys | `null` en el ejemplo y regenerar si es null o placeholder | minutos |
| I14 | deploy | **Deploy en el VPS sin documentar en el repo.** No hay unit systemd de `webapp.py` en ningún lado. `DEPLOY_ORACLE.md` es de Oracle (`User=ubuntu`, `pip3`, sin límites de memoria ni TZ ni UMask) y la línea 52 quedó rota. `README.md:114` y `LEEME.md:116` hablan de dos sedes o de "8 a 17 h". `requirements.txt` no trae `pywebpush`: un venv nuevo deja el push apagado en silencio. `ondepor.sh` no es ejecutable en git (100644) y en esta PC está con CRLF | Unit `reservame.service` con el venv, `Restart=always`, `MemoryMax` y `UMask=0077`; guía del VPS; `.gitattributes` con `*.sh eol=lf`; `git update-index --chmod=+x` | 1 h |

---

## Siguen del 18/08 (verificado en el código de f660315)

**Graves**

| ID | Qué | Estado | Dónde hoy |
|---|---|---|---|
| G1 | El manager pasa 'cancelada' a 'vencida'. Empeoró: ahora el push dice "se pasó la hora" | SIN ARREGLAR | `webapp.py:1134` (sumar también en 3983 y `encuesta.py:613`) |
| G2 | cambiar_jugadores sin mirar la hora | **PARCIAL** | Ya usa `ubicar_turno` por hora, pero no estricto (ver N5) |
| G3 | /vigilar es un toggle sin guard de estado | SIN ARREGLAR | `webapp.py:3202-3205` |
| G4 | Timeout en el cancelar del cambio | SIN ARREGLAR | `ondepor_core.py:730` (ver N4) |
| G5 | Crear una reserva con el mismo id pisa la de otro y su estado | SIN ARREGLAR | `webapp.py:2791`, `2815` |
| G6 | Fallback "uno solo ese día" | SIN ARREGLAR, **y creció** | ver N5 |
| G7 | Editar una reserva `en_curso` deja dos hilos disparando (también desde el cronograma, 4198) | SIN ARREGLAR | `webapp.py:2786` |
| G8 | Timeout en el confirm tomado como rechazo definitivo | SIN ARREGLAR | `ondepor_core.py:469`, `reservar_bot.py:408` |
| G9 | Turno de 23:00 guardado con fin "00:00". **Más expuesto**: ahora está en la única grilla y en el cronograma | SIN ARREGLAR | `webapp.py:2757`, `2794` |
| G10 | Clave por defecto "cambiame" | SIN ARREGLAR en el código | `webapp.py:140/147/305` (ver Paso 0) |
| G11 | El auto-refresh borra el formulario | SIN ARREGLAR | `webapp.py:2631` |

**Medios:** siguen 24 de 27. Los que más pesan:
- el XSS reflejado de `?programar=` (`webapp.py:2643`), confirmado con el test client para usuarios con `cambiar_ondepor`, y también `?editar=`;
- cambio y cancelación programados sin exclusión mutua;
- acciones `en_curso` colgadas tras un reinicio;
- la vigilancia no saltea `en_curso` (961);
- borrar una reserva `en_curso` o cuando la cancelación falló (3233);
- "Reservar ahora" antes de la apertura;
- NTP caído que frena el spin (`ondepor_core.py:83`);
- pre-carga sin límite de tiempo, que además no mira si la apertura ya pasó;
- confirm perdido que termina en doble reserva;
- sesión vencida (302) sin re-login;
- `_es_ocupada` leyendo una respuesta contaminada;
- sesión del panel no revalidada;
- CSRF, y GET con efectos (`/encuesta/cronograma?...` guarda configuración);
- votos de gente sacada de la encuesta;
- finde y cierre de la encuesta según el día en que se abre (abierta un viernes cierra **después** del finde; abierta un domingo, el "domingo" queda antes que el "sábado").

**Bajos:** 1 arreglado, 1 que ya no aplica, 2 parciales y 24 pendientes.

---

## Tests

Hay 166 tests verdes y la capa de unidades prueba comportamiento real. Pero **la capa que coordina todo nunca corre**:

- 19 stubs reemplazan justo a `notificar`, `_correr_accion` y `_hacer_cambio_jugadores`.
- Tienen 0 % de cobertura: `_manager`, `_disparar_job`, `_registrar_resultado`, `_avisar_reservas_rotas`, `_limpiar_confirmadas_viejas`, la vigilancia y `disparar()` (0 de 107 líneas).
- Cobertura por líneas: encuesta 94 %, webapp 67 %, ondepor_core 40 %, reservar_bot 33 %.
- 11 tests del cronograma usan `verificar_plan` como verificador, y ese verificador tiene los mismos puntos ciegos que N6.

**Tests frágiles**
- Los jueves entre las 23:59:01 y las 23:59:59 (hora AR) fallan 6 de TestEncuesta.
- Con TZ UTC+9 o UTC+14 fallan 3 que usan `date.today()−1` (líneas 258, 2060 y 2139).
- `test_nunca_se_queda_sin_administrador` (1070) es una tautología.

**Los 5 tests que más valen, en orden**
1. **Un ciclo del manager** (extraer `_ciclo(datos)`).
   - Una 'cancelada' sigue 'cancelada' y no manda push.
   - Si `notificar` lanza, el ciclo sigue.
   - Una vencida avisa al creador.
   - Una reserva dentro de la ventana se dispara **una** sola vez.
2. **`notificar` con push activo y una suscripción.**
   - Devuelve 1 y nunca lanza.
   - Una suscripción que responde 410 se borra.
3. **Invariante del armador con un verificador independiente**, con los 4 caminos de N6 y votos aleatorios con semilla.
   - Ningún carnet queda en dos ventanas que se pisan.
   - Cada reserva y cada cambio tienen 1+3 carnets.
4. **`disparar()` con un OnDepor falso y reloj simulado.**
   - El primer confirm sale a la apertura +0,05 s o después.
   - La pre-carga ocurre a −40 s.
   - Si la pre-carga entra, no se pasa al flujo normal.
   - Un error de login transitorio se reintenta (N3).
   - "Refresca" ×8 se trata como error pasajero, "máximo de reservas" corta al primer intento.
5. **Cronograma con reloj simulado.**
   - Abrir la encuesta cualquier día da sábado < domingo y un cierre antes de la primera apertura.
   - No se planifican turnos con la apertura ya pasada.
   - Un segundo "Programar" no crea nada ni pisa estados.

---

## Lo que está bien hecho (no tocar)

- **Seguridad**
  - **Control de acceso:** no hay IDOR explotable ni escalada de rol. Todas las mutaciones sensibles chequean creador o permiso, y un jugador no llega a `/usuarios*`.
  - **Passkeys:** challenge firmado de 300 s, origin, rp_id, sign_count y verificación de usuario obligatoria. Solo se registran para el usuario logueado.
  - **Cookies:** HttpOnly, SameSite=Lax y Secure.
  - **Escapado:** `esc()` se aplica en casi todo el HTML.
  - **Red y límites:** bind a `127.0.0.1`, rate limit en login y búsquedas, ProxyFix.
  - **Push:** no se le puede mandar un push a otro usuario.
  - **Historia de git limpia de secretos:** los JSON con credenciales nunca se commitearon.
- **Persistencia y concurrencia:** escritura atómica con fsync y lock, orden de locks consistente (sin deadlocks) y red siempre fuera del lock.
- **Timing:** NTP cacheado con spin local y hora argentina en todo el motor (no queda ningún `date.today()`). Reintento en paralelo solo para cancha puntual, con la pedida primera.
- **Diseño de lo nuevo:**
  - La reconciliación es conservadora: si no hay respuesta, no toca nada.
  - `ubicar_turno` es un criterio único y tiene tests.
  - El motor de acciones marca `en_curso` antes de largar el hilo y lo vuelve a chequear.
  - El latido de /salud está antes de las tareas auxiliares.
- **Un solo club:** la validación está en el backend y no solo en el formulario. Los formularios viejos cacheados caen bien en 1102.
- **Portabilidad a Linux:** UTF-8 en todos los `open()` y rutas desde `__file__`.

## Qué no se pudo verificar offline

Todas estas son dudas sobre cómo responde OnDepor:
- si la lista de reservas viene paginada;
- si excluye los turnos ya empezados, lo que daría falsos "fallada" durante el partido;
- si devuelve `00:00` o `24:00` como fin del turno de las 23;
- si un jid nocturno sirve para una CHACRA de la mañana (I3);
- si un login nuevo invalida la sesión de un disparo en curso: reconciliación y vigilancia se loguean mientras disparan, y si fuera así se vería "¿svid/sesión?" en los logs del server.

## Plan sugerido

1. **Hoy (≤ 1 h):** N1, el Paso 0 (N7 y la clave), G1 + G3, N5 (`estricto=True`) y G9 (`24:00`).
2. **Esta semana:** N3, N4 + G4, N2, I4 + I5, I1 + I2, el XSS y el test 1 del manager.
3. **Después:** N6 con el test 3, I6 + I7, G5/G7/G8, la unit systemd y la guía del VPS (I14), y los tests 4 y 5.
