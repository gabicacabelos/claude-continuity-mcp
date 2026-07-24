# claude-continuity-mcp — Resumen (español)

> Versión en inglés y documentación completa: ver [`README.md`](README.md).

## Qué es

Un servidor MCP (Model Context Protocol) que se conecta a cualquier cliente de Claude — Code, Desktop, Cowork — y les da algo que ninguno tiene de fábrica: memoria persistente en disco, compartida entre todos ellos. No es un modelo, no es un agente, no reemplaza a Claude: es infraestructura local que se sienta entre Claude y tus archivos para decidir qué entra realmente a la ventana de contexto.

Corre en tu máquina. Todo es 100% local, determinístico y sin API keys.

## El problema que resuelve

Claude es amnésico entre sesiones y ciego entre clientes. Si ayer le pediste a Claude Code que lea `auth.py`, hoy en una sesión nueva —o en Claude Desktop, o en Cowork— vuelve a leerlo entero. Mismo archivo, mismos tokens, otra vez. En un proyecto activo eso significa releer miles de tokens por día en contenido que no cambió una sola línea.

El segundo problema es la fragmentación: cada cliente de Claude trabaja aislado. Lo que se decidió en una sesión de Code no existe para la sesión de Cowork del mismo proyecto. No hay forma de dejarle una tarea a otro cliente y que la retome con contexto — cada handoff implica re-explicar todo desde cero.

## Qué ofrece — seis herramientas

**`router_smart_read`** — lectura quirúrgica con memoria. Un archivo grande no entra entero al contexto: se lee con ranking local (BM25, o embeddings si instalás `fastembed`) y devuelve solo los fragmentos exactos relevantes, con número de línea. La parte distintiva es la memoria cross-sesión: cada lectura queda registrada con su hash. La próxima vez que cualquier cliente pida ese mismo archivo, si no cambió devuelve un outline de ~90 tokens en vez de los miles originales; si cambió, devuelve solo el diff. Fidelidad 100%, porque no resume con un modelo — es determinista.

**`router_checkpoint`** — guarda el estado de una tarea larga (resumen, decisiones tomadas, pendientes, archivos involucrados) como JSON legible en disco. Cualquier sesión futura, en cualquier cliente, lo retoma con `action="resume"` en ~300 tokens — y de paso informa qué archivos cambiaron en disco desde entonces, sin releerlos. Sin checkpoint guardado, reconstruye un digest determinista de la actividad reciente.

**`router_inbox`** — el buzón asíncrono entre clientes. Los chats de Claude no pueden comandarse en tiempo real entre sí, pero comparten este disco: un cliente deja una orden (opcionalmente con un checkpoint vinculado y con `assets` — rutas o URLs de material de apoyo), otro la consume al arrancar, la ejecuta y reporta el resultado. Registra `completed_by` para saber quién ejecutó cada orden, distinto de para quién era.

**`router_project_search`** — búsqueda BM25 en TODO el proyecto cuando no sabés en qué archivo está lo que buscás. Índice incremental (solo re-indexa lo que cambió) que persiste cross-sesión — devuelve los archivos más relevantes con fragmentos exactos.

**`router_rules`** — reglas permanentes del proyecto ("nunca usar Redux") con procedencia: quién la decidió, cuándo, y de qué checkpoint nació. Viven en un JSON git-friendly en la raíz del proyecto y se inyectan solas en `smart_read`/`resume`.

**`router_status`** — métricas honestas de cuántos tokens de las fuentes originales nunca entraron al contexto, más lecturas, hits de memoria y estado del inbox.

## La novedad para la comunidad

Hay muchas herramientas de "ahorro de contexto" que en el fondo son un LLM barato resumiendo tu archivo antes de que Claude lo vea — lo cual introduce pérdida y, en código, termina costando más en reintentos. Este proyecto evita esa trampa a propósito: donde importa la fidelidad, `smart_read` es puramente determinista (hashing, diff, ranking léxico o por embeddings), nunca un resumen generado.

El punto que no existe en ningún otro MCP público es la combinación: **memoria que persiste entre sesiones**, **memoria que persiste entre clientes distintos de Claude**, y **un canal para que esos clientes se coordinen entre sí sin que el usuario oficie de mensajero**. Un checkpoint guardado en Code aparece disponible en Cowork; una orden dejada en Cowork la ve Code al arrancar. (Claude Design no puede cargar el MCP, así que participa del handoff por un puente semi-manual vía Google Drive — ver el README.)

Es open source (MIT), sin costo, y pensado para instalarse una vez y quedar activo en todas las sesiones de Claude — no es una herramienta que se invoca a mano, sino infraestructura que Claude aprende a usar sola gracias a las `instructions` que el propio servidor inyecta.

## Números medidos

| Escenario | Ahorro | Fidelidad |
|---|---|---|
| Releer archivo sin cambios | 98–99% | 100% |
| Releer archivo modificado | 90–99% | 100% (diff exacto) |
| Retomar tarea en cliente nuevo (checkpoint resume) | ~300 tokens vs re-explorar todo | 100% |
| Búsqueda puntual en archivo de 20k tokens | 80–95% | 100% |
| Chat conversacional normal | ~0% | — |

Repo: `github.com/gabicacabelos/claude-continuity-mcp`
