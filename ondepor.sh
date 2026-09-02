#!/usr/bin/env bash
# Reemplaza en Linux a los .bat de la carpeta:
#   "0 - probar (sin reservar).bat"  ->  ./ondepor.sh probar [id|all]
#   "1 - ver reservas.bat"           ->  ./ondepor.sh reservas
#   "agregar jugador.bat"            ->  ./ondepor.sh agregar-jugador [nombre]
#   "instalar.bat"                   ->  ./ondepor.sh instalar
# Los de _archivo_local (panel, programar eventos) quedan como estaban: no se portan.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# Python: el venv del proyecto si existe, si no el del sistema (PYTHON=... lo pisa).
PY=${PYTHON:-}
if [ -z "$PY" ]; then
  if [ -x ../.venv/bin/python ]; then PY=../.venv/bin/python; else PY=python3; fi
fi

uso() {
  cat <<EOF
Uso: ./ondepor.sh <accion> [argumentos]

  probar [id|all]        loguea y ubica el turno, NO reserva nada (default: all)
  reservas               lista las reservas cargadas (= reservar_bot.py listar)
  agregar-jugador [nom]  agrega un jugador a db_jugadores.json
  buscar-jugador <texto> busca un jugador en OnDepor
  instalar               instala lo que necesita el bot (requirements.txt)

Python en uso: $PY
EOF
}

case "${1:-}" in
  probar)
    shift
    echo "Probando login y ubicacion del turno (NO reserva nada)..."
    echo
    "$PY" reservar_bot.py probar "${1:-all}"
    ;;
  reservas|listar)
    "$PY" reservar_bot.py listar
    ;;
  agregar-jugador)
    shift
    "$PY" reservar_bot.py agregar-jugador "$@"
    ;;
  buscar-jugador)
    shift
    "$PY" reservar_bot.py buscar-jugador "$@"
    ;;
  instalar)
    echo "Instalando lo que necesita el bot (requirements.txt)..."
    ~/.local/bin/uv pip install --python "$PY" --upgrade -r requirements.txt
    ;;
  -h|--help|help|"")
    uso
    ;;
  *)
    echo "No conozco la accion '$1'." >&2
    uso >&2
    exit 2
    ;;
esac
