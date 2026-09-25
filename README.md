# d1Mano-actions

Orquestador público de GitHub Actions para el sistema de agentes remotos de d1Mano.

> Referencia de arquitectura: roadmap interno de d1Mano (repo privado).

## Qué contiene

- **workflows** de orquestación (`.github/workflows/`)
- **scripts** genéricos y no sensibles (`scripts/`)
- **docs** operativos no sensibles (`docs/`)

Este repo **no contiene nunca**: credenciales, tokens, secretos de proveedores,
datos reales de clientes, prompts/skills privados ni configuración privada del
agente. Los secretos llegan en runtime vía GitHub Secrets / Supabase.

## Workflows

| Workflow | Fase | Rol |
|---|---|---|
| `health.yml` | 2 | Prueba del canal: `workflow_dispatch` → runner → resultado, con medición de tiempos |
| `manage-workspace.yml` | 3 | Ciclo de vida del Codespace (start/stop/status/list) + estado en Supabase `agent_control` |
| `agent-runtime.yml` | 5/7 | Control del runtime del agente DENTRO del Codespace (start/stop/status/selftest vía SSH) |

## Uso rápido

```bash
gh workflow run health.yml -f payload=ping
gh run watch
```
