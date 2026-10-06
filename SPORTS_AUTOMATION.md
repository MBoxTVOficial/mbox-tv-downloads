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

Una clave ausente, HTTP distinto de 200, JSON inválido, `errors`, resultados
inconsistentes o paginación incompleta hacen fallar el proceso y conservan la salida.
Un fixture relevante malformado también detiene la publicación. Una respuesta válida
con cero resultados genera el día con las cuatro secciones vacías.

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
