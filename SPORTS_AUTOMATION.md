# Agenda deportiva automática de MBox TV

`scripts/generate_sports_today.py` consulta una vez los fixtures del día en Argentina,
selecciona competiciones relevantes y genera el `sports_today.json` de la raíz.
Usa Python 3.10+ y exclusivamente su biblioteca estándar; no hay que instalar paquetes.

Endpoint: `GET https://v3.football.api-sports.io/fixtures` con
`date=YYYY-MM-DD` y `timezone=America/Argentina/Buenos_Aires`.
La autenticación usa el header `x-apisports-key`, leído **solo** de la variable de
entorno `API_FOOTBALL_KEY`. La app Android no contiene esta clave ni consulta esta API.

## Configuración de competiciones

Editar `ARGENTINA_LEAGUES`, `CONMEBOL_LEAGUES`, `CHAMPIONS_LEAGUES` e
`INTERNATIONAL_LEAGUES` en el script. Cada `LeagueRule` permite IDs, alias y países.
Los IDs conocidos tienen prioridad; el fallback compara nombres completos normalizados
(sin acentos, diferencias de mayúsculas o puntuación) y exige el país configurado.
No se incluyen todas las competiciones del país ni coincidencias parciales.

- Argentina: Liga Profesional/Primera División, Primera Nacional, Copa Argentina,
  Supercopa, Copa de la Liga y Trofeo de Campeones. El fallback exige Argentina.
- CONMEBOL: Libertadores, Sudamericana y Recopa.
- Champions: UEFA Champions League.
- Internacional: Premier League de Inglaterra, La Liga, Serie A italiana,
  Bundesliga, Ligue 1, MLS, Europa/Conference League, Mundial, Copa América,
  Eurocopa, eliminatorias y UEFA Nations League.

IDs inicialmente verificados: **2** (Champions), **39** (Premier League) y **140**
(La Liga), publicados en la [guía oficial de API-Football](https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide).
Para otras competiciones se usan alias y país; no se inventaron IDs. Se pueden agregar
IDs confirmados con `/leagues` o el [dashboard de IDs](https://dashboard.api-football.com/soccer/ids/leagues).

## Salida y errores

La salida contiene `schemaVersion: 1`, fecha argentina, timezone, `updatedAt` ISO-8601
con offset `-03:00`, `demo: false` y las cuatro secciones, aunque estén vacías:
`argentina`, `conmebol`, `champions`, `international`, con prioridades 1–4.

Cada evento incluye `fixture-<id>`, sport `football`, competencia, equipos, logos,
hora `HH:mm` y estado. No genera URLs IPTV ni asociaciones `channels`.
Los eventos se ordenan por hora; `TBD` o una hora ausente quedan al final.
Las horas con offset se convierten a Argentina y se excluyen fixtures de otro día local.

Estados: NS/TBD → SCHEDULED; PST → POSTPONED; CANC/ABD → CANCELLED;
1H/HT/2H/ET/BT/P/SUSP/INT/LIVE → LIVE; FT/AET/PEN → FINISHED.
Estados desconocidos se conservan; si faltan, se usa UNKNOWN.

### Marcadores opcionales

El generador usa exclusivamente los goles de API-Football:

- `goals.home` → `homeScore`.
- `goals.away` → `awayScore`.

Si ambos valores son enteros no negativos, incluye ambos campos en el evento.
En `LIVE` representan el marcador actual y en `FINISHED` el resultado final.
`0-0` y `5-0` son marcadores válidos cuando la API proporciona ambos enteros.
En `SCHEDULED` con `null/null`, no se publica un `0-0` ficticio.

Si falta un valor, es `null` o es inválido (negativo, boolean, string o float),
se omiten **ambos** campos. Un `goals` ausente o malformado tampoco aporta marcador.
Esta excepción conserva el fixture y el resto de la agenda si sus campos obligatorios
son válidos: un marcador opcional incoherente no debe impedir publicar la programación.
No se convierte `null` a cero ni se usan `score.halftime`, `score.fulltime`,
`score.extratime` o `score.penalty`.

La validación de salida exige ambos scores presentes o ambos ausentes; si están,
deben ser enteros no negativos, nunca boolean, string, float ni `null` explícito.
Los feeds anteriores sin scores siguen siendo válidos y `schemaVersion` permanece en 1.
Los cambios de marcador participan en la comparación estructural existente: un score
idéntico conserva los bytes y `updatedAt`; un score diferente actualiza el feed.

Una clave ausente, HTTP distinto de 200, JSON inválido, `errors`, resultados
inconsistentes o paginación incompleta hacen fallar el proceso y conservan la salida.
Un fixture relevante malformado también detiene la publicación. Una respuesta válida
con cero resultados genera el día con las cuatro secciones vacías.
Si ya existe una agenda válida no vacía para la misma fecha y la nueva respuesta
vacía **todas** las secciones relevantes, se conserva la agenda anterior y falla
la actualización. No se mezclan marcadores viejos con la respuesta nueva. Un día
nuevo o una primera carga válida sí pueden tener cero eventos; secciones individuales
pueden vaciarse mientras siga habiendo otros eventos relevantes.

Se valida el modelo, se escribe `sports_today.json.tmp`, se valida ese archivo y
se reemplaza el destino atómicamente. Si el contenido es idéntico, se conservan
los bytes y `updatedAt`: este campo indica el último cambio de contenido, no la
última consulta. Una fecha nueva o cualquier cambio de agenda lo actualiza.

El script usa la zona IANA cuando está disponible. En Windows sin base IANA usa
UTC−3, el offset argentino actual, sin requerir dependencias adicionales.

## Ejecución local

Desde la raíz del repositorio, en PowerShell 7, ingresar la clave sin guardarla
en archivos ni dejarla escrita en el historial:

```powershell
$env:API_FOOTBALL_KEY = Read-Host 'API_FOOTBALL_KEY' -MaskInput
python scripts/generate_sports_today.py
Remove-Item Env:API_FOOTBALL_KEY
```

La clave es necesaria para una consulta real. Los logs solo muestran fecha,
cantidades y ruta de salida; nunca muestran la clave, headers o respuesta remota.

Para transformar una respuesta API guardada sin requests ni clave:

```powershell
python scripts/generate_sports_today.py --input fixtures_sample.json
```

Este modo escribe `sports_today.preview.json`, y rechaza una salida que apunte al
`sports_today.json` real. Una prueba reproducible con el ejemplo incluido:

```powershell
python scripts/generate_sports_today.py --input examples/fixtures_sample.json --date 2026-10-06 --output examples/sports_today.generated.json
python -m unittest discover -s tests -p "test_*.py"
```

Los equipos, fixture IDs y logos del ejemplo son sintéticos. El JSON generado de
ejemplo **no es programación real** y no debe sustituir el feed de producción.
`--date` permite probar una fecha concreta; sin esa opción se usa el día actual argentino.

## GitHub Actions

Agregar el secreto en GitHub → Settings → Secrets and variables → Actions →
New repository secret. **Name: `API_FOOTBALL_KEY`**. Pegar la clave en el valor del
secreto, nunca en el script, workflow o archivos del repositorio.

El workflow `.github/workflows/update-sports-today.yml` usa el secreto solamente
en el paso de generación. Corre tests offline antes de consultar la API.

| Cron UTC | Hora aproximada en Argentina |
| --- | --- |
| `0 11 * * *` | 08:00 |
| `0 15 * * *` | 12:00 |
| `0 20 * * *` | 17:00 |
| `0 23 * * *` | 20:00 |

Son cuatro consultas diarias, sin polling frecuente. GitHub puede demorar una
ejecución programada; estos horarios no son una garantía de puntualidad.
Ver [documentación de schedules](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule).

### Actualización LIVE

`.github/workflows/update-sports-live.yml` usa **el mismo generador**, con `--live`,
la misma API key desde `secrets.API_FOOTBALL_KEY` y el mismo grupo de concurrencia
`sports-today-main`. Ambos workflows se serializan; usan `main` y nunca cargan examples.
El workflow LIVE mantiene `workflow_dispatch` para consultas manuales reales.
Una ejecución manual permite consultar aunque el feed no tenga candidatos activos;
mantiene la misma comprobación del presupuesto y nunca usa datos de ejemplo.

| Cron UTC LIVE | Horario ART |
| --- | --- |
| `7,17,27,37,47,57 18-23 * * *` | 15:07–20:57 |
| `7,17,27,37,47,57 0-3 * * *` | 21:07–00:57 |

Son **60 oportunidades LIVE al día**, cada diez minutos entre aproximadamente
15:00 y 01:00 ART. El minuto 7 evita el comienzo de la hora, que suele tener mayor
carga en [GitHub Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
GitHub puede retrasar u omitir ejecuciones: no se promete una latencia exacta.

Antes de usar API-Football, `--live` revisa el feed ya cargado desde disco:

- Si hay un evento `LIVE`, consulta para actualizarlo y detectar su finalización.
- Si hay un `SCHEDULED` con hora conocida, consulta desde diez minutos antes del
  inicio hasta cuatro horas después. Este margen permite descubrir la transición
  a LIVE sin quedarse indefinidamente consultando un NS atrasado.
- Si todos terminaron, fueron cancelados/aplazados o no tienen hora conocida y no
  hay otro candidato, omite la consulta. Las cuatro cargas generales siguen vigentes.
- Si falta un feed válido o cambió el día argentino, permite una primera carga real.
  Se consulta exclusivamente la fecha argentina actual, incluyendo después de medianoche.
  Partidos de la fecha anterior requieren una futura estrategia específica; no se
  añaden consultas a fechas anteriores en este cambio.

Cada ejecución elegible hace **un solo GET `/fixtures` para todo el día**, sin llamadas
por equipo, liga o partido, sin retries HTTP ni fallback sintético. Los estados LIVE
usan los goles recibidos en cada respuesta; FT/AET/PEN pasan a FINISHED con esos mismos
goles. Los marcadores no se calculan ni se obtienen de los desgloses `score.*`.
La APK verá los nuevos datos en su próxima lectura del feed remoto; este repositorio
no modifica la frecuencia de refresh de Android.

El commit del workflow LIVE es `Update live sports scores` y añade exclusivamente
`sports_today.json`. Si no cambia el contenido, conserva bytes/`updatedAt` y no hace
commit ni push. El workflow general conserva su mensaje y sus cuatro crons.

### Presupuesto compartido y persistente

`MAX_DAILY_API_CALLS = 90` deja diez solicitudes de margen respecto de las 100 del
plan gratuito. Las cuotas del dashboard directo se reinician a las 00:00 UTC,
según [API-Football](https://www.api-football.com/terms); la agenda sigue usando ART.

Máximo automático: **60 LIVE + 4 generales = 64 solicitudes por día UTC**. El filtro
de actividad normalmente reduce esa cifra. Las ejecuciones manuales nuevas comparten
el tope de 90; con los 64 slots programados quedan hasta 26 slots adicionales.

Para no depender de la memoria efímera del runner, el generador consulta el historial
persistente de ambos workflows mediante la
[API de workflow runs](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow).
Cada run creado ese día UTC reserva conservadoramente una posible consulta; sus
intentos históricos también cuentan. Runs fallidos, omitidos o todavía en cola
consumen slots aunque finalmente no hayan usado API-Football. No se requiere otro
archivo, caché, rama de contabilidad ni secreto nuevo. El historial del día debe
conservarse para mantener esta cota.

La ejecución actual debe aparecer completa en el historial, ser de `main`, ser
`schedule`/`workflow_dispatch` y tener `run_attempt = 1`. Los re-runs se omiten: para
reintentar se usa **Run workflow**, que crea otro run contabilizado. Runs creados en
otro día UTC no pueden usar la cuota nueva. Si el contador supera 90, se omite sin
consultar ni cambiar el feed. Si GitHub falla o devuelve metadata incompleta, se
falla conservando el feed **sin llamar a API-Football**. No se usa un contador local
que pueda reiniciarse silenciosamente entre runners.

Ambos workflows conservan `contents: write` y añaden solo `actions: read` para leer
ese historial mediante `${{ github.token }}` (`GITHUB_TOKEN` en el paso de generación).
La API key existente no cambia. Las consultas al historial GitHub no consumen cuota
de API-Football. La cota cubre estos workflows; usos de la misma key fuera de ellos
comparten la cuota del proveedor y deben contabilizarse por separado.

Logs seguros con prefijo `SPORTS_REALTIME`: `REALTIME_REQUEST`, `REALTIME_RESPONSE`,
`LIVE_FIXTURES`, `SCORE_CHANGED`, `STATUS_CHANGED`, `API_BUDGET` y `REFRESH_SKIPPED`.
El modo `--input` se identifica como `OFFLINE_RESPONSE`/`source=offline`, nunca como
consulta real. No se imprimen API key, token GitHub, headers ni respuestas completas.

Para una ejecución manual: Actions → **Update sports schedule** → Run workflow →
seleccionar `main` → Run workflow. El job solo publica desde `main`.
Las ejecuciones del workflow se serializan para evitar escritores simultáneos.

Con `contents: write`, el bot **MBox Sports Bot** hace commit `Update sports schedule`
y push de **solo `sports_today.json`** si cambió. Si no cambió, termina sin commit.
No usa force push. Si la rama avanzó mientras corría o sus reglas bloquean al bot,
el push falla; hay que revisar el permiso y volver a ejecutar el workflow.

Para activar la automatización, estos nuevos archivos deben incorporarse a `main`
y el secreto debe existir. La preparación local no hizo commit ni push manual
y no creó el secreto. No cambió APKs, Android, RemoteConfig, `update.json`,
GitHub Pages, releases, versionado ni canales IPTV.
