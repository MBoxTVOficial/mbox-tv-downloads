# Edición manual de Fútbol

Editar **sports_manual_agenda.json** en GitHub, rama **main**. El bot sigue consultando sus páginas y publicando su agenda normal. El archivo manual se aplica después y no se sobrescribe durante las actualizaciones.

Al guardar el archivo en GitHub se ejecuta **Apply manual football agenda**. Usa la agenda automática ya guardada: no consulta páginas deportivas, APIs ni IPTV. GitHub puede demorar en ejecutar la acción. No hace falta generar ni instalar otra APK.

No editar `sports_today.json`, `sports_agenda_window.json`, `sports_agenda_automatic.json` ni `sports_fixture_identity.json`: son salidas generadas. El horario siempre es de Argentina, `America/Argentina/Buenos_Aires`.

## Añadir un partido que falta

Agregar un objeto a `events`. Este ejemplo es el partido histórico del **19 de noviembre de 2026**, no un partido del día actual:

```json
{
  "id": "argentina-australia-sub17",
  "date": "2026-11-19",
  "sectionId": "international",
  "competition": "Mundial Sub-17",
  "homeTeam": "Argentina Sub-17",
  "awayTeam": "Australia Sub-17",
  "startTime": "13:00",
  "channelIds": [165660, 165659]
}
```

Los dos IDs corresponden a las opciones de TyC Sports de nuestra grilla. DGO es una plataforma y no se convierte en otro canal. El ejemplo está documentado aquí y no está activado en el archivo de edición.

- `id`: una etiqueta propia, única para ese día. Mantenerla al corregir el mismo partido para conservar su identidad.
- `date`: fecha real del partido, `YYYY-MM-DD`.
- `startTime`: hora ART real en formato `HH:MM`.
- `sectionId`: usar `international` para la sección Fútbol - Internacional, o `argentina` para Partidos de Argentina. También se admiten los IDs de secciones que ya aparecen en la agenda publicada.
- `competition`, `homeTeam`, `awayTeam`: nombres completos. Indicar Sub-17, Reserva u otra categoría cuando corresponda para distinguir equipos mayores.
- `channelIds`: IDs de canales confirmados para ese partido, en el orden del selector. `[]` deja la tarjeta informativa.
- `homeLogo`, `awayLogo`: opcionales; URLs HTTPS públicas de las imágenes. Si se omiten, quedan vacíos. No usar enlaces de reproducción.

La tarjeta aparece únicamente en su fecha y en la ventana de agenda disponible. Una entrada de ayer no se publica como partido de hoy. No se crean fechas sin una agenda automática válida. Se mantienen los límites de acceso y finalización que ya tiene la app.

## Asociar canales a una tarjeta que ya existe

Buscar el partido en `sports_today.json` y copiar el número de su `id`. Si dice `fixture-1234567890`, el `fixtureId` es `1234567890`.

Agregar en `channelOverrides`, sustituyendo el número de ejemplo por el ID real del partido y la fecha por la fecha real:

```json
{
  "date": "2026-11-20",
  "fixtureId": 1234567890,
  "channelIds": [165660, 165659]
}
```

Esto sustituye solamente los canales de esa tarjeta y ese día. No cambia equipos, horario, resultados, posición del foco ni reproducción. Usar los IDs de la señal que realmente transmite ese encuentro; los dos IDs de TyC son solo un ejemplo.

Si son varias señales, poner sus IDs en la lista en el orden deseado. No repetir un mismo ID. Para quitar temporalmente todos los canales de esa tarjeta usar `channelIds: []`.

## Consultar y actualizar los IDs

`sports_manual_channel_catalog.json` contiene nombres, categorías e IDs verificados en los archivos locales, sin URLs IPTV ni credenciales. El catálogo inicial contiene 724 canales deportivos del 8/10/2026 más dos Telefe identificados posteriormente; **no es una exportación completa ni actual de toda la grilla**. Su presencia no prueba qué partido está transmitiendo ahora.

Si un ID no figura en ese catálogo, el editor lo rechazará. Primero verificarlo en la grilla del servidor que usa la app y agregar su registro al catálogo:

```json
{"streamId": 123456, "name": "Nombre exacto del canal", "category": "EVENTOS DIARIOS", "enabled": true}
```

El ID anterior es un ejemplo, no un canal confirmado. No usar números de posición de la grilla como si fueran `streamId`. No mezclar IDs de servidores distintos. Este esquema de agenda no incluye un campo de servidor.

## Quitar una edición

- Eliminar el objeto de `events` para retirar una tarjeta manual.
- Eliminar el objeto de `channelOverrides` para recuperar los canales automáticos originales.
- Dejar ambas listas vacías para volver a la agenda automática.

El bot guarda la base automática por separado. Por eso una edición manual no borra la información original y puede deshacerse sin nuevas consultas.

## Errores y comprobación

El JSON no admite comentarios ni comas finales. No agregar `score`, `status`, URLs de streams u otros campos. Un ID desconocido, deshabilitado o repetido se rechaza. Si el partido ya existe con la misma competición, equipos y horario, usar `channelOverrides` en lugar de duplicarlo.

La acción manual conserva la agenda publicada si el archivo es inválido. Durante una actualización automática se intenta mantener la última configuración manual válida y se registra un aviso; las consultas automáticas siguen funcionando.

Después de que la acción termine correctamente, entrar nuevamente a Fútbol para refrescar la agenda. Los escudos manuales son opcionales y no se descargan ni se inventan durante la edición.
