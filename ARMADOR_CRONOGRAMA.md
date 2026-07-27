# Armador de cronograma — de los votos de la encuesta a las reservas

Cómo se pasa de "27 personas votaron" a "estas canchas, con estos carnets, disparadas
por estos reservadores". Documento de diseño: acá está la lógica acordada con Diego.

**Estado: IMPLEMENTADO.** La lógica está en `encuesta.armar_cronograma()` y la pantalla
en `/encuesta/cronograma`. Nada se programa hasta que el admin toca el botón.

---

## 1. Qué entra

| Dato | De dónde sale |
|---|---|
| Quién juega cada día y a qué hora | votos de la encuesta (`modo = juego` + `horas`) |
| Quién presta el carnet sin venir | votos (`modo = presta`) |
| Carnets de afuera de la encuesta | `encuesta_config.json → carnets_extra` (los elige Diego a mano) |
| Quién puede disparar una reserva | `db_reservadores.json` (los que tienen cuenta de OnDepor) |
| Canchas del club | `reservar_bot.CANCHAS_POR_CLUB[1102]` (11 canchas: QUINTA, KINERET, CHACRA) |

---

## 2. Las reglas del grupo

**Cuántas canchas** (regla textual de Diego, ya implementada en `canchas_para()`):

| Anotados | Canchas |
|---|---|
| menos de 4 | 0 — no se reserva |
| 4 a 7 | 1 |
| 8 a 11 | 2 |
| 12 o más | 3 |

Tope duro de **3 canchas** por ahora.

**Cuántos carnets hacen falta:** 4 por cancha. La bolsa del día es
`juegan + prestan + carnets_extra`. El tope real es el menor de los dos:

```
canchas = min( canchas_para(anotados_en_esa_hora), len(bolsa) // 4 )
```

Esto ya se muestra en el panel: cuando los carnets no alcanzan, avisa
*"(los carnets alcanzan para 2)"*.

---

## 3. La regla que manda: un carnet, una reserva

**Un carnet no puede estar en dos reservas activas del mismo club al mismo tiempo.**
Es el límite de OnDepor y es lo que obliga a repartir con cuidado.

Un carnet se libera en dos momentos:

1. cuando **pasa** el turno, o
2. cuando el **cambio de nombres** (10 min antes del turno) lo saca de la reserva —
   es decir, cuando el carnet prestado se reemplaza por el que realmente juega.

De ahí salen dos consecuencias que cambian todo el armado:

- ✅ **Los carnets del sábado se reciclan para el domingo.** El cambio de nombres del
  sábado 9:50 los libera, y el turno del domingo abre 10:01 → hay 11 minutos de margen.
- ❌ **NO se reciclan entre dos turnos del mismo día.** Los dos se reservan el día
  anterior, con una hora de diferencia, mucho antes de que se haga ningún cambio de
  nombres. Si el sábado hay turno a las 10 y a las 16, **cada uno necesita sus propios
  4 carnets por cancha**.

Esta última es la restricción más cara: dos turnos el mismo sábado con 2 canchas cada
uno son **16 carnets distintos**, no 8.

---

## 4. Cómo se arma cada cancha

- Cada cancha la dispara un **reservador** (uno de los que tienen cuenta de OnDepor).
  Su carnet cuenta como uno de los 4 → hacen falta 3 carnets más.
- En la reserva tienen que estar **los que juegan**. Vale meter a alguien que jugó el
  turno anterior aunque no juegue este (regla del club, sirve de comodín cuando el
  cuarto no aparece).
- Un reservador **no puede figurar como jugador en otra reserva** del mismo turno, ni al
  revés. La app ya lo detecta y **avisa** (no bloquea — así lo pidió Diego).
- **La rotación no es problema del armador.** Si son 9 y hay 2 canchas (8 lugares), el
  noveno rota adentro del turno: rota la gente en la cancha, no los carnets de la
  reserva.

---

## 5. Qué tendría que devolver

Para cada día votado, una propuesta como esta, **para que Diego apruebe antes de que se
programe nada**:

```
SÁBADO 2/8 — 10:00 h  ·  9 anotados → 2 canchas
  Cancha A   reservador: Diego G.      + Pablo B. · Darío W. · Jonathan E.
  Cancha B   reservador: Daniel W.     + Marcelo S. · Gustavo K. · Leo P.
  Rota: Ariel M. (9º)
  Cambio de nombres programado 9:50 → entran los que juegan de verdad

DOMINGO 3/8 — 16:00 h  ·  5 anotados → 1 cancha
  Cancha A   reservador: Pablo B.      + Diego G. · Darío W. · Daniel W.
  (usa carnets liberados por el cambio del sábado)
```

Aprobado eso, se cargan las reservas programadas y los cambios de nombre, que es todo
maquinaria que **ya existe** en la app.

---

## 6. Los criterios que quedaron elegidos

Ninguno es una regla del club: son los desempates que había que definir para que el
armador pudiera decidir solo. Se cambian en `_armar_turno()`.

1. **Qué hora se juega cada día.** Por defecto la más votada (si empatan, la más
   temprana), pero en la pantalla hay un chip por horario con cuánta gente hay en cada
   uno, y se puede marcar más de uno: la propuesta se rearma sola. Ahí se ve el costo
   real de jugar dos turnos el mismo día — el segundo se come otra tanda de carnets y
   suele dejar al día siguiente sin nada.
2. **Quién entra en la reserva.** Primero los que juegan (su carnet iba a estar en la
   cancha igual, y así no hay que cambiarle el nombre a nadie), después los prestados,
   y último los de afuera. Se reparten por turnos entre las canchas.
3. **Quién dispara.** Entre los que tienen cuenta de OnDepor con el carnet libre, se
   prefiere a los que además juegan: el reservador queda sí o sí adentro de la reserva,
   así que si no juega ocupa un lugar al pedo (y el armador lo avisa).

---

## 7. Dónde está cada cosa en el código

| Pieza | Dónde está |
|---|---|
| El armador entero | `encuesta.armar_cronograma()` → `_armar_turno()` |
| Ventanas en que cada carnet está tomado | `encuesta._Agenda` |
| Cuántas canchas dan N anotados | `encuesta.canchas_para(n)` |
| Bolsa de carnets y anotados por hora | `encuesta.resumen_votos()` → `por_dia[d]` |
| Carnets prestados de afuera | `encuesta_config.json → carnets_extra` |
| La pantalla de revisión | `webapp.encuesta_cronograma()` (`/encuesta/cronograma`) |
| El botón que lo vuelve reservas | `webapp.encuesta_cronograma_programar()` |
| Programar el cambio de nombres | `webapp._programar_cambio_del_cronograma()` |
| Tests | `test_bot.TestCronograma` (8 casos) |

El cambio de nombres se cuelga de la reserva al crearla, pero el ejecutor solo lo corre
si para entonces la reserva está **confirmada** — si la cancha no entró, no se toca nada.
