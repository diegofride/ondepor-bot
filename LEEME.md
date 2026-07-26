# Bot de reservas OnDepor

Toma turnos **apenas se habilitan** (24 h antes). Vos dejás las reservas **precargadas**
y el bot crea un **evento en Windows** que se dispara solo, al segundo exacto de la apertura.
Si cargás varias reservas, se disparan **en paralelo**.

> Ejemplo: querés jugar el **15/07 a las 16:00**. El turno abre el **14/07 a las 16:01:00**.
> El bot crea un evento que ese 14/07 a las 16:01:00 reserva solo, aunque no estés en la compu
> (solo tiene que estar **prendida y con tu usuario de Windows iniciado**).

---

## ⭐ Forma fácil: el PANEL visual

Instalá una vez con **`instalar.bat`** y después abrí **`PANEL.bat`**. Es una sola ventana
con 3 pestañas:

1. **Jugadores** — escribís un apellido, busca en OnDepor y con un clic **guardás** los
   jugadores (con su `player_id`). A la derecha ves los guardados.
2. **Reservadores** — cargás quién reserva (nombre + usuario + contraseña de OnDepor).
   Botón **"Probar login"** para verificar.
3. **Reservas** — elegís reservador, fecha, hora, cancha, tipo y tildás los acompañantes.
   Botones: **"Probar (sin reservar)"**, **"Ver cuándo se dispara"** y
   **"PROGRAMAR"** (crea el evento y se dispara solo). "Ver / borrar programadas" para
   administrarlas.

Con el panel no necesitás tocar ningún archivo a mano. Lo de abajo es la alternativa por
archivos/comandos, por si preferís.

---

## Las 3 bases de datos (se editan con el Bloc de notas)

**`db_jugadores.json`** — los jugadores que agregás a un turno (compañeros).
Cada uno con su `player_id` de OnDepor y un nombre para reconocerlo:
```json
{ "nombre": "Juan Perez", "player_id": 111111 }
```

**`db_reservadores.json`** — los que **reservan** (el jugador 1). Son los que se loguean,
así que llevan **usuario y clave**:
```json
{ "nombre": "Diego", "usuario": "diego@mail.com", "clave": "TU_CLAVE", "player_id": 999999, "telefono": "" }
```

**`reservas.json`** — las reservas precargadas. Cada una dice qué turno, quién reserva y con quiénes:
```json
{
  "id": "sabado-16hs",
  "club_id": 1102,
  "cancha": "",                       // vacío = cualquier cancha libre
  "fecha": "2026-07-15",
  "hora_desde": "16:00",
  "hora_hasta": "17:00",
  "single_double": 2,                 // 2 = dobles (4 jug.), 1 = singles (2 jug.)
  "reservador": "Diego",              // nombre de db_reservadores (el jugador 1)
  "jugadores": ["Juan Perez", 222222] // los OTROS jugadores: nombre o player_id directo
}
```
La hora de apertura se calcula sola (turno − 24 h + 1 min). Si tu club abre en otro momento,
poné `"apertura_iso": "2026-07-14T16:01:00"` en esa reserva.

---

## Cómo usarlo (doble clic, sin escribir comandos)

1. **`instalar.bat`** — una sola vez (instala lo necesario).
2. Cargá tus jugadores con **`agregar jugador.bat`**: escribís un apellido, te muestra
   los socios que encuentra en OnDepor y, al elegirlos, **los guarda solos** en
   `db_jugadores.json` (con su `player_id`). Repetilo por cada jugador. Después completá
   `db_reservadores.json` (usuario y clave) y `reservas.json` (tus turnos).
3. **`1 - ver reservas.bat`** — muestra tus reservas y a qué hora se dispara cada una.
   Avisa si falta algún dato.
4. **`2 - programar eventos.bat`** — crea los eventos de Windows. **¡Listo!** Se disparan solos.
5. **`3 - ver eventos programados.bat`** — para chequear que quedaron creados.
6. **`4 - desprogramar todo.bat`** — si querés cancelarlos.

Cada vez que cambies `reservas.json`, volvé a apretar **`2 - programar eventos.bat`**.

---

## Por comandos (avanzado)

Abrí PowerShell en esta carpeta:
```
python reservar_bot.py listar                  # ver reservas y horarios de disparo
python reservar_bot.py agregar-jugador "perez" # buscar y GUARDAR jugadores en la base
python reservar_bot.py buscar-jugador "perez"  # solo mostrar (sin guardar)
python reservar_bot.py probar sabado-16hs   # ensayo: loguea y ubica el turno, NO reserva
python reservar_bot.py ya sabado-16hs        # reservar YA (si ya abrió)
python reservar_bot.py correr sabado-16hs    # esperar apertura y reservar al segundo
python reservar_bot.py programar all         # crear los eventos de Windows
python reservar_bot.py desprogramar all      # borrarlos
python reservar_bot.py tareas                # ver los eventos creados
```
En lugar de un `id` podés poner `all` para todas. Con varias, corren en paralelo.

Los registros de cada disparo quedan en la carpeta **`logs/`** (un archivo por reserva),
así podés ver qué pasó aunque no estuvieras mirando.

---

## Notas importantes

- La compu tiene que estar **prendida y con tu sesión de Windows iniciada** a la hora de apertura.
- El club limita las **reservas activas por jugador**: si un compañero llegó al tope, OnDepor
  rechaza (queda anotado en el log). Cambiá de jugador.
- Usa tu propia cuenta y hace lo mismo que harías a mano, solo que en el segundo justo.

## Las canchas del club 1102

El bot elige la cancha por el campo `cancha` de cada reserva:
- **vacío** → la primera cancha libre (cualquiera).
- **un número o nombre** → esa: `"5"`, `"KINERET"` o `"05-KINERET"`.

Canchas del club: `01-04 QUINTA`, `05-08 KINERET`, `09-11 CHACRA`. Horarios de **8 a 17h**.

## Probar antes (recomendado)

Antes de un turno importante, corré **`0 - probar (sin reservar).bat`**. Loguea con tu
cuenta, abre el turno y **lista las canchas disponibles sin reservar nada**. Si ves
"Login OK" y la lista de canchas, está todo listo para que se dispare solo.

> Verificado el 12/07/2026 con la cuenta real: login, apertura de formulario, listado de
> canchas y confirmación funcionan. La reserva entra siempre que estén cargados los
> jugadores que pide el club (4 para dobles).
