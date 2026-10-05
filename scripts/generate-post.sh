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
      -H "Authorization: Bearer $AUTOMATION_PAT" -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/d1Mano/d1mano-automation/contents/scripts/generate_post_prompt.md?ref=main" \
      | jq -r '.content' | base64 -d > "$TEMPLATE_FILE" \
      || fail "No pude descargar el template del generador"
  else
    fail "Falta el template ($TEMPLATE_FILE), el fallback commiteado y AUTOMATION_PAT"
  fi
  echo "::endgroup::"
fi

# ── 3. Generación con OpenCode (modelos free de Zen, sin API keys) ──
echo "::group::3. OpenCode"
MODEL="${POST_MODEL:-}"
if [ -z "$MODEL" ]; then
  # Igual que las tasks: el modelo vive en system_config (service_get_config).
  # Cadena: OC_MODEL_POSTS → OC_MODEL_TASKS (el free probado) → big-pickle.
  for K in OC_MODEL_POSTS OC_MODEL_TASKS; do
    [ -n "$MODEL" ] && break
    MODEL=$(curl -sS -X POST "$API/rpc/service_get_config" "${AUTH[@]}" \
        -H "Content-Type: application/json" -d "{\"p_key\":\"$K\"}" \
        | jq -r '. // empty' 2>/dev/null) || MODEL=""
  done
fi
MODEL="${MODEL:-big-pickle}"
echo "model=inhouse/$MODEL"

SYSTEM=$(cat "$TEMPLATE_FILE")
USER_MSG=$(printf 'POSTER_TYPE: %s\nDRAFT_ID: %s\nBRIEF_JSON: %s' "$POSTER_TYPE" "$DRAFT_ID" "$BRIEF")
FULL_PROMPT="${SYSTEM}

---

${USER_MSG}"

OUT="/tmp/opencode_post_${POST_ID}.log"
# El exit code del CLI NO es el gate (mismo criterio que las tasks): el JSON
# de la respuesta es la certificación.
set +e
opencode run --pure --model "inhouse/${MODEL}" --auto --title "post-${POST_ID}" "$FULL_PROMPT" 2>&1 | tee "$OUT"
CODE=$?
set -e
echo "::endgroup::"

# ── 4. Parseo del JSON de la respuesta ──
echo "::group::4. Parseo y escritura"
CONTENT=$(python3 - "$OUT" <<'PYEOF'
import json, sys
text = open(sys.argv[1], encoding='utf-8', errors='replace').read()
cands = []
i = 0
while i < len(text):
    if text[i] == '{':
        depth = 0; j = i
        while j < len(text):
            if text[j] == '{': depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    cands.append(text[i:j+1]); i = j; break
            j += 1
    i += 1
best = None
for blob in reversed(cands):
    try: d = json.loads(blob)
    except Exception: continue
    if isinstance(d, dict) and all(k in d for k in ('title','body','cta','hashtags')):
        best = d; break
if best is None:
    raise SystemExit('sin JSON title/body/cta/hashtags en la salida de opencode')
print(json.dumps(best, ensure_ascii=False))
PYEOF
) || fail "La salida de opencode no trae el JSON esperado"

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
META=$(printf '{"model":"inhouse/%s","usage":null}' "$MODEL")

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
