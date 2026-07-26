# 🎾 Reservame ésta!

Bot que reserva canchas de pádel en **OnDepor** apenas se habilitan.

Los turnos del club abren exactamente **24 h + 1 min antes** (un turno de mañana 22:00 se
habilita hoy a las 22:01). El bot deja las reservas precargadas y las dispara solo, al
segundo exacto, para no depender de estar con el celular en la mano.

Corre 24/7 en una VM chica (Ubuntu + Python) detrás de Caddy con HTTPS, y es además una
**PWA instalable** en el celular con notificaciones push.

---

## Qué hace

- **Dispara solo** a la apertura del turno, con precisión de reloj NTP.
- **Pre-carga** todo 40 s antes usando otro turno ya abierto → la reserva entra en el
  mismo segundo en que abre (ver *La optimización clave*).
- **Fallback de cancha**: si la que pediste está ocupada, salta a las otras (en paralelo
  si pediste una puntual).
- **Caza cancelaciones**: si no entraste, vigila cada 4 min por si alguien libera.
- **Reservas recurrentes**: "todos los martes 22 hs" se carga sola cada semana.
- **Avisos push** al que creó la reserva: confirmada, fallida, o "revisá esto que no va a
  poder dispararse".
- **Cambio de jugadores** de una reserva ya confirmada (cancela y vuelve a tomar la misma
  cancha, con red de seguridad), inmediato o programado para 10 min antes del turno.
- **Historial** con estadísticas: cuántas entraron y cuántas veces tocó la cancha pedida.
- **Usuarios con roles** y permisos configurables desde el panel.

## Arquitectura

| Archivo | Rol |
|---|---|
| `ondepor_core.py` | **Motor**: habla con OnDepor (login, abrir turno, canchas, confirmar, cancelar). |
| `reservar_bot.py` | **Orquestador**: arma los "trabajos" y los dispara al segundo exacto. |
| `webapp.py` | **App web (Flask)**: pantallas + un hilo demonio que cada 12 s revisa qué disparar, limpiar, avisar o vigilar. |
| `test_bot.py` | 53 tests automáticos, sin red (~1 s). |

Persistencia en **archivos JSON** con escritura atómica (`tmp` + `os.replace`) y un `RLock`
global. No hay base de datos: el volumen es chico y el deploy es copiar archivos.

## El flujo de reserva de OnDepor

No hay API pública: esto es ingeniería inversa del sitio.

```
1. POST /app/api/login                          → cookies de sesión
   (después hay que leer del HTML el 'svid' y el token CSRF)
2. POST /player/reservation/my-reservation-new  → devuelve 'jid' (token del turno)
3. POST /player/reservation/courtsavailable     → canchas libres
4. POST /player/reservation/add-reservation-new → confirma (jid + court_id + player_id[])
```

Tres detalles que rompen todo si no se respetan:

- **Todos** los POST a `/player` necesitan `svid`, o el server redirige a la home.
- Hay que "buscar" a cada jugador (`search-players`) antes de confirmar, o rechaza el cupo
  con *"Ocurrió un error, refresca tu pantalla"*.
- Hace falta una pausa de ~0,35 s entre pasos, por el mismo motivo.

El `jid` **no se puede calcular**: lo genera el servidor al abrir el formulario.

## La optimización clave

El flujo completo tarda ~4 s. Demasiado para pelear un turno cuando abre.

La solución (idea del dueño del proyecto, no del bot) es **pre-cargar usando otro turno que
ya esté abierto**:

1. **40 s antes** de la apertura: se abre el formulario de un *turno fuente* (eso da un
   `jid` vivo), se piden sus canchas (eso "activa" el `court_id`) y se registran los
   jugadores.
2. **Al abrir el turno objetivo**: queda solo el POST de confirmar.

Medido en producción: la reserva entra en **el mismo segundo** de la apertura. Está
verificado que el estado precargado sobrevive la espera y el cambio de horario. Si algo
falla, cae automáticamente al flujo completo de siempre.

## Detalles que valen la pena

- **Precisión**: offset NTP cacheado + spin-loop con reloj local, para no meter jitter de
  red en el momento crítico.
- **Zona horaria**: el server corre en UTC y el club en ARG (UTC−3). Hay helpers
  `hoy_ar()` / `ahora_ar()`; usar `date.today()` causó un bug real (entre las 21 y las 24
  hs "desaparecían" las reservas del día).
- **Concurrencia**: varias reservas se disparan en hilos paralelos; el ciclo
  leer-modificar-escribir de los JSON va siempre bajo lock, y las llamadas de red quedan
  **fuera** del lock.
- **Resiliencia**: una reserva mal formada nunca tumba el reloj interno (`armar_trabajo`
  jamás lanza excepción: marca `problema` y sigue).

## Cómo correrlo

```bash
pip install -r requirements.txt

# copiar los ejemplos y completarlos con datos reales
cp usuarios.ejemplo.json usuarios.json
cp db_reservadores.ejemplo.json db_reservadores.json
cp db_jugadores.ejemplo.json db_jugadores.json
cp webconfig.ejemplo.json webconfig.json

python webapp.py        # http://localhost:8080
python test_bot.py      # 53 tests, ~1 s, sin tocar la red
```

Para el deploy en una VM, ver [`DEPLOY_ORACLE.md`](DEPLOY_ORACLE.md).

## ⚠️ Estado y limitaciones

- Las **contraseñas de OnDepor se guardan en texto plano** en `db_reservadores.json`. Es la
  deuda pendiente conocida del proyecto: los archivos con credenciales están excluidos del
  repo, pero conviene cifrarlas.
- El bot está atado a un club puntual (dos "sedes": diurna y nocturna, que en OnDepor son
  clubes distintos). Los `court_id` están mapeados a mano en `reservar_bot.py`.
- OnDepor **no permite editar los jugadores** de una reserva en este club (probado contra
  el endpoint real: responde *"El club no permite modificar la reserva"*), por eso el
  cambio de jugadores se hace cancelando y volviendo a tomar.
