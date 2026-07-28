# Armador de cronograma — de los votos de la encuesta al flujo del finde

Modelo definido por Darío (el que ayuda a Diego) el 28/7, con las decisiones de Diego
anotadas abajo. Reemplaza a los tres intentos anteriores, que trataban cada turno como
una isla. **La unidad no es el turno: es el finde entero**, porque las reservas y los
cambios de carnets están encadenados en el tiempo.

Implementado en `encuesta.armar_cronograma()` (clase `_Armador`); pantalla en
`/encuesta/cronograma`. Nada se programa hasta que el admin toca el botón.

---

## 1. La línea de tiempo del finde

- **VIERNES** — se reservan TODOS los turnos del sábado (cada uno a su apertura,
  turno −24h +1min). Como todavía no se liberó nada, las reservas del sábado usan
  **carnets todos distintos entre sí**.
- **SÁBADO a la mañana** — se reserva el turno del domingo a la mañana, con carnets
  que estén libres durante todo el sábado (los "comodines").
- **SÁBADO al terminar cada turno** — los carnets de los que jugaron se van liberando
  y corren los CAMBIOS que los estaban esperando.
- **SÁBADO a la tarde** — se reservan los turnos del domingo a la tarde, pisados con
  los prestados que ya se liberaron.
- **DOMINGO** — ídem: los cambios corren apenas se libera la gente del turno anterior.

**El cambio de carnets NO va 10 minutos antes del turno: va apenas se liberan los
carnets de los que entran** (`ej_epoch` propio en `cambio_prog`; el viejo default de
−10 min sigue para los cambios manuales). Cambiar temprano es lo que libera los
prestados a tiempo para reservar lo que sigue.

## 2. Las reglas duras

1. **Un carnet no puede estar en dos reservas que se pisen en el tiempo** (por club).
   El que juega lo devuelve al TERMINAR su turno; el que pisa lo devuelve en el
   CAMBIO. `_Agenda` guarda las ventanas y se consulta siempre por RANGO, nunca por
   instante (`libre(desde, hasta)` / `libre_desde` mira ventanas que se crucen).
2. **Cuando se juega, en la cancha están los que votaron ESE horario.** Si en algún
   turno no se puede, se marca con el motivo (pedido explícito de Diego).
3. Regla de canchas por turno: 4-7 anotados → 1, 8-11 → 2, 12+ → 3 (tope 3). El que
   sobra de los 4 titulares ROTA adentro del turno (juega igual).
4. 4 carnets por cancha; el reservador (cuenta de OnDepor) es uno de ellos.

## 3. Prioridad cuando los carnets no alcanzan (decisión de Diego)

**sáb tarde → dom mañana → sáb mañana → dom tarde**

- La tarde siempre manda ("siempre hay más gente a la tarde").
- Las MAÑANAS van en **un solo horario, el más votado, con 1 cancha** aunque por
  cantidad dieran más (regla de Darío: "1 cancha salvo que sobren carnets").
- El dom tarde va último porque es el más barato: se pisa con prestados que vuelven
  solos (se liberan con los cambios del día).

## 4. Cómo se arma cada cancha

- **Directos**: los que juegan esa hora y tienen el carnet libre TODO el rango
  [apertura → fin del turno] entran con su propio carnet. Sin cambio.
- **Pisados**: al que juega pero tiene el carnet tomado (por otro turno anterior) lo
  pisa un carnet del pool — orden: **prestados → de afuera → sueltos** (carnets de
  gente que juega pero quedó rotando). El cambio se programa para cuando el último de
  los que entran queda libre (+5 min, `MARGEN_CAMBIO_SEG`).
- Si los que entran **no se liberan antes del turno**, la cancha no se arma (esto es
  lo que hace que la mañana del sábado nunca se pueda pisar: a sus jugadores los
  tiene tomados la tarde, que termina después).
- **El cambio = cancelar + retomar** (el club no permite editar). Lo retoma una
  cuenta de OnDepor de los que juegan; si ninguno tiene cuenta, el cambio queda **A
  MANO** con aviso (decisión de Diego — no se mete un colado).
- En la elección de titulares de la tarde, los votantes de la mañana rotan (así su
  carnet queda libre para la mañana), y los que además tienen cuenta rotan primero
  (la mañana necesita quién la dispare). Cada cancha garantiza al menos una cuenta
  entre los que juegan.

## 5. Verificación

`verificar_plan()` barre el plan terminado contra las dos reglas duras y marca
cualquier violación con "⚠ VERIFICACIÓN". Si aparece una, la pantalla bloquea el
botón de programar. No debería saltar nunca: está para que un bug no llegue a
OnDepor (en el desarrollo ya cazó dos).

## 6. Trampa de datos: el `player_id` del reservador

En `db_reservadores.json` el `player_id` es opcional y casi nadie lo tenía. El
armador lo necesita (el carnet del reservador entra en la reserva). Se resuelve por
nombre contra `db_jugadores` con acentos y apodos (`vincular_carnet()`), sin adivinar
si hay dos candidatos; al guardar un reservador nuevo se vincula solo.

## 7. Dónde está cada cosa

| Pieza | Dónde |
|---|---|
| Armador completo | `encuesta._Armador` (+ `armar_cronograma()`) |
| Ventanas de carnets | `encuesta._Agenda` |
| Reparto de gente por franja | `encuesta.repartir_franja()` (la tarde; la mañana va al horario más votado) |
| Verificación | `encuesta.verificar_plan()` |
| Pantalla + botón | `webapp.encuesta_cronograma()` / `encuesta_cronograma_programar()` |
| Cambio con hora propia | `cambio_prog.ej_epoch`, ejecutado por `webapp._revisar_cambios_prog()` |
| Tests | `test_bot.TestCronograma` |
