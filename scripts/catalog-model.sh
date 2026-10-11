#!/bin/sh
# Corre el CLI del modelo como un usuario sin privilegios y con el entorno vacío.
#
# El modelo lee texto que escribió un tercero. Aunque ese texto consiguiera que el
# modelo intentara algo, este proceso no hereda ninguna variable del job, no puede
# escribir en el código ni en el directorio de la corrida, y no puede elevarse.
# Los argumentos pasan tal cual, sin shell de por medio que los reinterprete.
set -eu
exec sudo -n -u catalog-model -H \
  env -i HOME=/home/catalog-model PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 \
  sh -c 'cd "$HOME" && if [ "${1-}" = "--version" ]; then exec opencode --version; fi; exec opencode run --pure "$@"' sh "$@"
