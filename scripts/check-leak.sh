#!/usr/bin/env bash
# Este repositorio es público. Este chequeo falla si en el código del agente de
# catálogo aparece algo que identifique al negocio, a sus proveedores o a su
# base de datos, o una de las construcciones que el diseño prohíbe.
#
# No sustituye a leer lo que se sube; atrapa lo que se cuela por descuido.
set -euo pipefail
cd "$(dirname "$0")/.."

TARGETS=(scripts/catalog scripts/catalog-model.sh tests .github/workflows/catalog-agent.yml .github/workflows/catalog-ci.yml)
fail=0

check() { # nombre, patrón (ERE)
  local hits
  hits=$(grep -rnIE --exclude-dir=__pycache__ -- "$2" "${TARGETS[@]}" 2>/dev/null || true)
  if [ -n "$hits" ]; then
    echo "✗ $1"; echo "$hits" | cut -c1-160 | head -10; fail=1
  fi
}

# Identificadores reales. Los de prueba son deliberadamente más cortos o repetidos.
check "clave de proveedor con forma real"   'PRV_[A-Za-z0-9]{12}([^A-Za-z0-9]|$)'
check "referencia a un proyecto de Supabase" '[a-z]{20}\.supabase\.(co|in)'
check "JWT o llave embebida"                 'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'
check "token personal"                       '(sbp|ghp|gho|github_pat)_[A-Za-z0-9_]{20,}'
check "correo electrónico"                   '[A-Za-z0-9._%+-]+@(gmail|outlook|hotmail|yahoo|proton)\.[a-z]+'
check "teléfono con prefijo de país"         '\+53[ -]?[0-9]{8}'

# Construcciones prohibidas, buscadas sólo en código ejecutable de Python
# (los comentarios y docstrings pueden nombrarlas para explicar por qué no están).
forbidden=$(python3 - <<'PY'
import io, sys, tokenize
from pathlib import Path
BAD = {"extractall", "service_execute_sql", "eval", "exec", "pickle"}
for path in sorted(Path("scripts/catalog").glob("*.py")):
    tokens = tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline)
    for token in tokens:
        if token.type == tokenize.NAME and token.string in BAD:
            print(f"{path}:{token.start[0]}: {token.string}")
PY
)
if [ -n "$forbidden" ]; then echo "✗ construcción prohibida en código ejecutable"; echo "$forbidden"; fail=1; fi

# El paso del modelo no puede declarar credenciales de base de datos.
if awk '/name: Leer los chats y consultar al modelo/,/name: Validar, confrontar y aplicar/' \
     .github/workflows/catalog-agent.yml | grep -q 'secrets\.'; then
  echo "✗ el paso del modelo declara un secreto"; fail=1
fi
# Ni a nivel de job.
if awk '/^env:/,/^jobs:/' .github/workflows/catalog-agent.yml | grep -q 'secrets\.'; then
  echo "✗ hay un secreto en el entorno del job"; fail=1
fi
if grep -q 'upload-artifact' .github/workflows/catalog-agent.yml; then
  echo "✗ el workflow sube artefactos desde un repositorio público"; fail=1
fi

[ "$fail" = 0 ] && echo "check-leak: limpio"
exit "$fail"
