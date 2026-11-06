# Guest Radar · Eye on AI

Agente que **cada lunes** investiga las novedades de IA y le sugiere a Craig invitados para Eye on AI.
Publica todo en un **hub web** donde Craig escucha un briefing de audio, ve un video de cada candidato
y marca 👍 / 👎 / "ya lo conozco". El agente lee esas reacciones y la semana siguiente ajusta sus sugerencias.

## Cómo funciona

```
Lunes 08:00 (Buenos Aires)
  GitHub Actions  ──►  agent/run.py
                         1. Lee la memoria en Supabase: invitados que ya fueron, sugerencias anteriores,
                            los 👍/👎 y notas de Craig
                         2. Claude (claude-opus-5-5) + búsqueda web: noticias de los últimos ~10 días
                            → 6 a 8 candidatos, cada uno con fuentes y un video de YouTube
                         3. Chequea que cada video exista y descarta los nombres repetidos
                         4. Gemini TTS genera el briefing en audio (mp3, unos 3-4 min)
                         5. Guarda la semana en Supabase
  Hub (GitHub Pages) ◄── lee Supabase en vivo. No hace falta volver a publicar la web.
```

| Pieza | Dónde vive |
|---|---|
| Web (hub) | `site/`, publicada en GitHub Pages |
| Agente | `agent/run.py`, corre en GitHub Actions (`.github/workflows/weekly-radar.yml`) |
| Datos y audio | Supabase: tablas `runs`, `guests`, `feedback`, `past_guests` y el bucket `briefings` |
| Claves | `.env` en esta carpeta (nunca se sube) y los Secrets del repo de GitHub |

## Puesta en marcha (una sola vez)

1. Completá las claves en `.env` (cada línea explica de dónde sacarla).
2. Corré:

```bash
cd "/Users/joaquingarzon/Desktop/CLAUDE CODE/guest-radar"
.venv/bin/python agent/setup_supabase.py   # crea la base de datos, el storage y la semana piloto
.venv/bin/python agent/setup_github.py     # crea el repo, guarda los secrets, publica la web
```

Al terminar, el script imprime la URL del hub (también queda guardada en `.env` como `RADAR_HUB_URL`).

## Uso semanal

- **No hay que hacer nada.** El lunes a las 08:00 aparece la semana nueva en el hub.
- Mandale a Craig por WhatsApp algo así:
  > 🎧 Your weekly Guest Radar is ready (4 min): <link>
  > Mark the ones you like, and next week's picks will follow your lead.
- Para correrlo a mano: en GitHub, pestaña **Actions** → *Weekly Guest Radar* → **Run workflow**.
  Si la semana ya existe, tildá *force* para reemplazarla. Ojo: reemplazarla borra los 👍/👎 de esa semana.
- Para probarlo sin publicar nada:

```bash
.venv/bin/python agent/run.py --dry-run
```

  El resultado queda en `out/` (notas de la investigación, JSON y mp3).

## Agregar invitados que ya estuvieron en el show

Agregá filas a `agent/past_guests_seed.csv` y volvé a correr `setup_supabase.py`, o cargalas directo
en la tabla `past_guests` de Supabase. El agente además busca en eye-on.ai antes de sugerir a alguien.

## Costos estimados

- Claude: alrededor de USD 1-3 por corrida (investigación con búsqueda web y armado de las tarjetas). Unos USD 5-12 por mes.
- Gemini TTS, Supabase, GitHub Actions y Pages: entran en el plan gratis.

## Ajustes rápidos

- **Día y hora:** el `cron` en `.github/workflows/weekly-radar.yml` (está en UTC).
- **Modelo o voz:** las variables `RADAR_CLAUDE_MODEL`, `RADAR_TTS_VOICE` y `RADAR_TTS_MODEL` (ver `agent/run.py`).
- **Qué le interesa a Craig:** el texto `SHOW_BRIEF` y la lista `CATEGORIES` en `agent/run.py`.

## Si algo falla

- En GitHub → Actions → la corrida fallida → logs. Las notas de la investigación quedan como *artifact*.
- Si Claude falla, el agente intenta con Gemini. Si el audio falla, publica la semana igual, sin audio.
- Si salen menos de 3 invitados que sirvan, no publica nada (para no mostrarle algo pobre a Craig).
