#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# generate-post.sh <post_id>
#
# Genera la publicación de un poster (boceto_posts) y escribe el
# resultado en la MISMA fila. Sin storage intermedio: es una sola
# publicación por petición y el estado vive en la fila (PENDING →
# READY/FAILED), igual que rpc.sh escribe el estado del workspace.
#
#   1. GET  {SUPABASE_URL}/rest/v1/boceto_posts?id=eq.<post_id>
#          (service role: pasa RLS) — trae brief + poster_type.
#   2. POST a OpenRouter (o Groq) con el template y el brief.
#   3. PATCH boceto_posts: status=READY, caption, blocks, meta.
#      Cualquier fallo → status=FAILED, error=<motivo>.
#
# Dependencias: curl, jq, python3 (solo json.dumps para escapar seguro).
# ─────────────────────────────────────────────────────────────
set -euo pipefail

POST_ID="${1:?usage: generate-post.sh <post_id>}"
: "${SUPABASE_URL:?SUPABASE_URL requerido}"
: "${SUPABASE_SERVICE_ROLE:?SUPABASE_SERVICE_ROLE requerido}"

API="$SUPABASE_URL/rest/v1"
AUTH=( -H "apikey: $SUPABASE_SERVICE_ROLE" -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE" )

echo "::group::1. Brief del post $POST_ID"
ROW=$(curl -sS --fail-with-body "$API/boceto_posts?id=eq.$POST_ID&select=id,draft_id,poster_type,status,brief" "${AUTH[@]}")
echo "$ROW" | jq -c .
STATUS=$(echo "$ROW" | jq -r '.[0].status // empty')
if [ -z "$STATUS" ]; then
  echo "::error::post $POST_ID no encontrado"; exit 1
fi
BRIEF=$(echo "$ROW" | jq -c '.[0].brief')
POSTER_TYPE=$(echo "$ROW" | jq -r '.[0].poster_type')
DRAFT_ID=$(echo "$ROW" | jq -r '.[0].draft_id')
echo "status=$STATUS poster_type=$POSTER_TYPE draft_id=$DRAFT_ID"
echo "::endgroup::"

# Idempotencia: un post ya en curso/terminado no se regenera.
if [ "$STATUS" != "PENDING" ]; then
  echo "::warning::post en estado $STATUS — no se regenera. Salida."
  exit 0
fi

# Marcar GENERATING (con guard: solo si sigue PENDING, anti doble-dispatch).
curl -sS --fail-with-body -X PATCH "$API/boceto_posts?id=eq.$POST_ID&status=eq.PENDING" \
  "${AUTH[@]}" -H "Content-Type: application/json" -H "Prefer: return=minimal" \
  -d '{"status":"GENERATING"}' >/dev/null || {
    echo "::warning::otra corrida tomó el post. Salida."; exit 0; }

fail() {
  echo "::error::$1"
  MSG=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1")
  curl -sS -X PATCH "$API/boceto_posts?id=eq.$POST_ID" \
    "${AUTH[@]}" -H "Content-Type: application/json" -H "Prefer: return=minimal" \
    -d "{\"status\":\"FAILED\",\"error\":$MSG}" >/dev/null || true
  exit 1
}

# ── 2. Template del generador (repo automation; checkout opcional) ──
TEMPLATE_FILE="${POST_TEMPLATE:-/tmp/generate_post_prompt.md}"
if [ ! -f "$TEMPLATE_FILE" ]; then
  echo "::group::Descargando template del generador (d1mano-automation, main)"
  # El orquestador público NO contiene prompts privados: el template vive
  # en el repo privado automation (branch main, path scripts/).
  if [ -f "$(dirname "$0")/generate_post_prompt.md" ]; then
    echo "Uso fallback commiteado ($(dirname "$0")/generate_post_prompt.md)"
    cp "$(dirname "$0")/generate_post_prompt.md" "$TEMPLATE_FILE"
  elif [ -n "${AUTOMATION_PAT:-}" ]; then
    curl -sS --fail-with-body \
      -H "Authorization: -Bearer $AUTOMATION_PAT" -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/d1Mano/d1mano-automation/contents/scripts/generate_post_prompt.md?ref=main" \
      | jq -r '.content' | base64 -d > "$TEMPLATE_FILE" \
      || fail "No pude descargar el template del generador"
  else
    fail "Falta el template ($TEMPLATE_FILE), el fallback commiteado y AUTOMATION_PAT"
  fi
  echo "::endgroup::"
fi

# ── 3. LLM ──
echo "::group::3. LLM"
MODEL="${POST_MODEL:-openai/gpt-4o-mini}"
if [ -n "${OPENROUTER_API_KEY:-}" ]; then
  LLM_URL="https://openrouter.ai/api/v1/chat/completions"
  LLM_AUTH=( -H "Authorization: Bearer $OPENROUTER_API_KEY" )
elif [ -n "${GROQ_API_KEY:-}" ]; then
  LLM_URL="https://api.groq.com/openai/v1/chat/completions"
  LLM_AUTH=( -H "Authorization: Bearer $GROQ_API_KEY" )
  MODEL="${POST_MODEL:-llama-3.3-70b-versatile}"
else
  fail "Sin OPENROUTER_API_KEY ni GROQ_API_KEY"
fi
echo "model=$MODEL"

SYSTEM=$(cat "$TEMPLATE_FILE")
USER_MSG=$(printf 'POSTER_TYPE: %s\nDRAFT_ID: %s\nBRIEF_JSON: %s' "$POSTER_TYPE" "$DRAFT_ID" "$BRIEF")

REQUEST=$(python3 - "$SYSTEM" "$USER_MSG" "$MODEL" <<'PYEOF'
import json, sys
system, user, model = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({
  "model": model,
  "messages": [
    {"role": "system", "content": system},
    {"role": "user", "content": user}
  ],
  "temperature": 0.8,
  "response_format": {"type": "json_object"}
}))
PYEOF
)

RESP=$(curl -sS --fail-with-body "$LLM_URL" "${LLM_AUTH[@]}" \
  -H "Content-Type: application/json" -d "$REQUEST") \
  || fail "LLM rechazó la petición"
echo "::endgroup::"

# ── 4. Parseo del JSON del LLM ──
echo "::group::4. Parseo y escritura"
CONTENT=$(echo "$RESP" | jq -r '.choices[0].message.content // empty')
[ -n "$CONTENT" ] || fail "El LLM devolvió una respuesta vacía"

echo "$CONTENT" | jq -e '.title and .body and .cta and .hashtags' >/dev/null 2>&1 \
  || fail "El JSON del LLM no trae title/body/cta/hashtags"

PAYLOAD=$(echo "$CONTENT" | jq -c '{
  title: (.title | tostring),
  blocks: [
    { type: "title",    text: (.title    | tostring) },
    { type: "body",     text: (.body     | tostring) },
    { type: "cta",      text: (.cta      | tostring) },
    { type: "hashtags", text: (.hashtags | tostring) }
  ],
  caption: [(.title|tostring), (.body|tostring), (.cta|tostring), (.hashtags|tostring)] | join("\n\n")
}')
META=$(echo "$RESP" | jq -c '{model: (.model // null), usage: (.usage // null)}')

# caption/blocks al payload; meta con modelo y poster_type.
FULL=$(echo "$PAYLOAD" | jq -c --argjson meta "$META" --arg pt "$POSTER_TYPE" \
  '. + {meta: ($meta + {poster_type: $pt, generated_by: "gh-actions generate-post"})}')

echo "$FULL" | jq -c '{status, caption, blocks}' 
curl -sS --fail-with-body -X PATCH "$API/boceto_posts?id=eq.$POST_ID" \
  "${AUTH[@]}" -H "Content-Type: application/json" -H "Prefer: return=minimal" \
  -d "{\"status\":\"READY\",\"caption\":$(echo "$FULL" | jq '.caption'),\"blocks\":$(echo "$FULL" | jq '.blocks'),\"meta\":$(echo "$FULL" | jq '.meta'),\"error\":null}" >/dev/null \
  || fail "No pude escribir el resultado en boceto_posts"
echo "::notice::post $POST_ID READY"
echo "::endgroup::"
