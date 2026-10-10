# Scheduler externo LIVE de MBox TV

Cloudflare es el scheduler primario previsto; GitHub conserva un fallback horario. La activación requiere publicar primero este fallback, configurar autorización/secret y después desplegar. La publicación del código no implica que el Worker esté activo.

## Qué hace

Cloudflare Cron → Worker → GitHub `workflow_dispatch` → `update-sports-live.yml` en `main` → generador existente → feed/commit si cambia.

El Worker solo habla con GitHub. No obtiene fixtures, no resuelve canales y no conoce credenciales deportivas/IPTV. La lógica deportiva y su presupuesto permanecen en GitHub Actions.

`wrangler.toml` configura `*/10 * * * *`. Cloudflare ejecuta sus cron en UTC, pero el Worker determina la hora **actual** con `Intl.DateTimeFormat` y `America/Argentina/Buenos_Aires`: admite 15:00:00–00:59:59, incluidas las horas 15–23 y 00. No calcula la ventana sumando/restando tres horas. Fuera de la ventana no llama a GitHub. Un tick retrasado no usa una hora programada antigua para saltarse esa condición.

## Cómo evita duplicaciones

1. Consulta el historial LIVE de `main`, de cualquier evento (`schedule` y `workflow_dispatch` incluidos).
2. Una ejecución `queued` o `in_progress` bloquea un nuevo disparo. También se consideran activos `waiting`, `pending` y `requested`.
3. Una ejecución creada **o iniciada** en los últimos 8 minutos, incluido el límite exacto, bloquea el disparo aunque esté completada o haya fallado. No se confunde ausencia de commit con ausencia de run.
4. Si no hay ninguno de esos casos, envía solo `{"ref":"main"}`. No activa descubrimiento de broadcasters ni otros inputs.
5. Una protección adicional dentro de la misma instancia evita ticks simultáneos y repeticiones durante los 8 minutos posteriores a un dispatch exitoso, mientras GitHub incorpora el run al historial.

API usada:

- `GET https://api.github.com/repos/MBoxTVOficial/mbox-tv-downloads/actions/workflows/update-sports-live.yml/runs`
  - `branch=main`, `per_page=100`, historial móvil de 24 horas, sin filtrar por evento.
- `POST https://api.github.com/repos/MBoxTVOficial/mbox-tv-downloads/actions/workflows/update-sports-live.yml/dispatches`
  - Body: `{"ref":"main"}`.

Se utiliza la versión REST `2026-03-10`. `204` significa dispatch aceptado. Se admite también `200`, compatible con respuestas que incluyen detalles de la ejecución. **Aceptado no significa que el workflow haya finalizado correctamente.**

### Límites de la deduplicación

El GET devuelve como máximo 100 runs de las últimas 24 horas. Si el historial está incompleto/paginado o es inválido, no se hace POST: no se agrega una tercera llamada. Una ejecución anormalmente retenida por más de 24 horas queda fuera de esa consulta y requiere diagnóstico en Actions.

La comprobación GET + POST no es una transacción atómica ni una garantía de ejecución exactamente una vez. Dos instancias distintas o la demora de visibilidad del historial pueden producir una carrera. La protección en memoria no es un lock distribuido ni sobrevive a un reinicio. Un lock persistente requeriría otra intervención y recursos adicionales.

GitHub mantiene un **fallback una vez por hora**: `37 18-23 * * *` y `37 0-3 * * *`, equivalentes a 15:37, 16:37, …, 23:37 y 00:37 ART (10 slots/día). Cloudflare es el scheduler primario con ticks */10. Publicar/verificar estos cron antes de desplegar. Un dispatch Cloudflare a las 22:30 no impide necesariamente un schedule GitHub a las 22:37. La concurrencia existente `sports-today-main` ordena ejecuciones, pero no elimina todos los duplicados; el Worker consulta el historial en sus ticks posteriores.

## Errores y tiempo máximo

- Timeout de 8 segundos para cada request, incluido el cuerpo JSON del GET.
- Máximo 2 requests por tick; sin reintentos inmediatos y sin seguir redirecciones con el token.
- GET con 401/403/404/422/429/5xx, timeout, fallo de red o JSON/historial inválido → `GITHUB_CHECK_FAILED`, sin POST.
- POST fallido → `DISPATCH_FAILED`; no se reintenta inmediatamente. Un timeout POST puede significar que GitHub recibió el disparo, por eso se vuelve a comprobar el historial en un tick posterior.
- No se solicitan reintentos automáticos de Cloudflare para el mismo evento (`controller.noRetry()`).
- Se vuelve a comprobar la ventana antes del POST por si el GET atravesó las 01:00.
- El endpoint HTTP, para cualquier método/ruta, devuelve únicamente `{"service":"mbox-live-scheduler","status":"ok"}`. No permite disparar nada. `workers_dev` y las preview URLs públicas están deshabilitados en la configuración.

Los logs usan `MBOX_SCHEDULER`: `TICK`, `LOCAL_TIME`, `SKIP_OUTSIDE_WINDOW`, `CHECK_RUNS`, `SKIP_ACTIVE_RUN`, `SKIP_RECENT_RUN`, `GITHUB_CHECK_FAILED`, `DISPATCH_START`, `DISPATCH_SUCCESS`, `DISPATCH_FAILED`. `status=0` identifica errores sin respuesta HTTP. No se imprimen token, headers, cookies, cuerpos remotos ni mensajes de excepciones.

## Presupuesto diario

| Concepto | Máximo previsto |
|---|---:|
| Cron cada 10 minutos, todo el día | 144 ticks |
| Ticks dentro de 15:00–00:59 ART | 60 |
| Ticks fuera de la ventana | 84, sin llamadas GitHub |
| GET GitHub elegibles | 60 |
| POST GitHub | 60 |
| Requests GitHub totales | 120 |
| Tiempo de espera de red por tick | Aproximadamente 16 segundos, más procesamiento |

La deduplicación, un secret ausente o un error reducen esos valores. Es un cálculo para una entrega normal por slot; no promete que un proveedor jamás entregue un evento duplicado.

**Presupuesto automático tras publicar el fallback:** hasta 60 dispatch Cloudflare + 10 schedules LIVE de respaldo + 5 ejecuciones GENERAL (00:05, 08:00, 12:00, 17:00 y 20:00 ART) = **75 ejecuciones/llamadas deportivas potenciales por día**, por debajo de `MAX_DAILY_API_CALLS=90`, que permanece intacto. Cada primera ejecución puede efectuar como máximo una consulta de fixtures. La deduplicación y ausencia de candidatos pueden reducir el consumo. El generador trata `workflow_dispatch` como refresh manual: puede consultar la API aun sin candidatos LIVE. Runs manuales adicionales, reintentos, carreras/entregas duplicadas o atrasos entre días pueden consumir el margen; la guardia existente cuenta runs/intentos conservadoramente por día UTC y falla de forma segura al alcanzar el límite. No se promete una reserva de 15 llamadas manuales. El Worker no consume directamente cuota deportiva. **No desplegar con los antiguos 60 schedules LIVE/día todavía activos.**

## Probar localmente

Requisitos: Node.js 22 o posterior y npm. Wrangler queda fijado en `package-lock.json`; no se instala globalmente.

Desde esta carpeta:

```powershell
npm ci
npm test
npm run lint
npm run simulate
```

Los tests y la simulación usan fetch mock; nunca leen un token real del entorno. Los valores de autenticación de prueba se generan solo en memoria. La simulación cubre 15:00, 15:10, …, 00:50: con situaciones variables espera 60 ticks, 60 GET, 20 POST y 40 skips por deduplicación. Sin runs recientes espera 60 GET + 60 POST = 120 requests simulados.

Para probar el runtime Worker local **sin credenciales**:

```powershell
$env:WRANGLER_SEND_METRICS = 'false'
$env:CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV = 'false'
$env:CLOUDFLARE_INCLUDE_PROCESS_ENV = 'false'
npm run dev
```

En otra terminal:

```powershell
Invoke-RestMethod http://localhost:8787/
Invoke-WebRequest 'http://localhost:8787/cdn-cgi/local/scheduled?format=json'
```

Sin secret, el tick omite GitHub: `missing_secret` dentro de la ventana, o `SKIP_OUTSIDE_WINDOW` fuera. No pongas el token de producción en `.dev.vars` para esta revisión. La ruta especial de prueba existe solo en Wrangler local con `--test-scheduled`; no es un trigger HTTP público de producción. Cerrá el servidor local con Ctrl+C.

Estas pruebas locales no ejecutan dispatch real ni necesitan secrets. El despliegue pertenece a la etapa autorizada de activación, después del push y de configurar el secret.

## Activación segura, paso a paso

Seguir este orden; si hace falta login o introducir un token, el usuario debe completar esa acción. No registrar el valor del token.

1. **Cuenta Cloudflare.** Entrá a [Cloudflare](https://dash.cloudflare.com/sign-up), creá tu cuenta y verificá el correo.
2. **Herramientas.** Instalá Node.js 22 o posterior. Abrí esta carpeta en una terminal, ejecutá `npm ci` y luego `npx wrangler login`. Se abre el navegador para autorizar tu cuenta Cloudflare.
3. **Worker.** En Workers & Pages, creá un Worker llamado `mbox-live-scheduler`, sin agregar todavía un cron ni una ruta pública. El nombre debe coincidir con `wrangler.toml`.
4. **Token GitHub.** En GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens, generá uno con vencimiento y nombre descriptivo. Elegí Resource owner **MBoxTVOficial** y Only select repositories → **mbox-tv-downloads**. En Repository permissions asigná **Actions: Read and write**. Metadata: Read-only queda obligatorio/automático. No necesita Contents: Write, Administration ni acceso a otros repositorios. La lectura del historial requiere Actions: Read; el dispatch necesita Actions: Write, que incluye esa lectura. Si la organización exige aprobación, el administrador debe aprobarlo antes de activar.
5. **Guardar el secret.** Ejecutá `npx wrangler secret put GITHUB_TOKEN`. Pegá el token únicamente en el prompt de Wrangler, nunca como argumento, archivo o mensaje. Este comando es para la futura activación: puede crear/actualizar recursos remotos.
6. **Desplegar.** Verificá primero en GitHub que LIVE tenga solo los dos cron horarios indicados y que `workflow_dispatch` siga intacto. Con autorización Cloudflare y el secret ya configurados, ejecutá `npx wrangler deploy`. Publicará este código y su cron. No modifica los workflows GitHub.
7. **Verificar cron.** En el Worker → Settings → Triggers/Cron Triggers, confirmá `*/10 * * * *`. Los cambios de cron pueden tardar en propagarse. El horario argentino se aplica en el código, no en el cron UTC. Esperá un slot natural dentro de la ventana.
8. **Revisar.** Usá los logs del Worker o `npx wrangler tail`. Buscá `DISPATCH_SUCCESS`/skips y revisá Actions en GitHub para comprobar el resultado real del run, el feed y el presupuesto. El JSON HTTP informativo no certifica el estado de GitHub.
9. **Desactivar si hace falta.** Eliminá el Cron Trigger desde el panel del Worker; los cron nativos GitHub seguirán disponibles. Para cortar el acceso inmediatamente, revocá el PAT en GitHub. Después se puede borrar el secret/Worker desde el panel si ya no se usará. No desactives el fallback GitHub durante la primera validación.

## Seguridad y fuentes oficiales

La carpeta tiene su propio `.gitignore`: `node_modules`, `.wrangler`, `.env*`, `.dev.vars*`, bundles y logs quedan fuera de Git. No hay token real ni archivos locales de secrets. El único cambio fuera de esta carpeta es el schedule LIVE horario y su comentario. No se modifican feeds, lógica/reglas deportivas o Android.

- [Cloudflare Cron Triggers: UTC y pruebas locales](https://developers.cloudflare.com/workers/configuration/cron-triggers/)
- [Cloudflare scheduled handler](https://developers.cloudflare.com/workers/runtime-apis/handlers/scheduled/)
- [Cloudflare secrets y archivos locales ignorados](https://developers.cloudflare.com/workers/configuration/secrets/)
- [GitHub: listar runs y permiso Actions Read](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow)
- [GitHub: workflow_dispatch y permiso Actions Write](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
- [GitHub: compatibilidad de respuestas 204/200](https://github.blog/changelog/2026-02-19-workflow-dispatch-api-now-returns-run-ids/)
