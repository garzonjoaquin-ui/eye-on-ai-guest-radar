"""Guest Radar weekly agent.

1. Reads what Craig already did (past guests, earlier suggestions, his 👍/👎 and notes).
2. Researches the week's AI news with Claude + web search and picks 6–8 guests.
3. Turns the research into structured cards + a spoken briefing script.
4. Checks each YouTube link, voices the briefing (Gemini TTS -> mp3) and publishes to Supabase.

Usage:
    python agent/run.py               # full run, publishes
    python agent/run.py --dry-run     # no Supabase writes; saves out/<week>.json (+ mp3)
    python agent/run.py --force       # replace this week's run even if it exists
"""

import argparse
import base64
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.parse
import wave

from common import ROOT, Supabase, http, load_env

CLAUDE_MODEL = os.environ.get("RADAR_CLAUDE_MODEL", "claude-opus-5-5")
GEMINI_TEXT_MODEL = os.environ.get("RADAR_GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_TTS_MODEL = os.environ.get("RADAR_TTS_MODEL", "gemini-3.8-flash-tts")
TTS_VOICE = os.environ.get("RADAR_TTS_VOICE", "Charon")

CATEGORIES = [
    "Chips & Hardware",
    "Models & Algorithms",
    "Consciousness & the Brain",
    "Defense & Ukraine",
    "AI for Science",
    "AI Policy & Society",
    "Robotics & Physical AI",
]

SHOW_BRIEF = """\
Eye on AI is a long-form interview podcast hosted by Craig S. Smith, a former New York Times
correspondent who spent much of his career in China and has covered AI since before ChatGPT.
The show ranges from chip design to consciousness theory. Craig wants to rebalance it away from
sponsored vendor episodes toward serious, high-profile guests: researchers, founders and
public figures who are newsworthy *right now* and who can sustain a 45-60 minute conversation.
Evidence from the channel: Ilya Sutskever (16k views) and neuroscientist Christof Koch
(11.7k views, best retention) vastly outperform typical sponsored episodes (500-1,000 views).
Ongoing interests: consciousness and the brain, chips and inference hardware, new model
architectures (world models, open weights), AI in the Ukraine war and defense, AI for science,
China's AI labs. Craig does the outreach himself, so suggestions must be people a serious
journalist can plausibly reach and who would say yes to a respected niche podcast."""

GUEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "briefing_script", "guests"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "briefing_script": {"type": "string"},
        "guests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "role", "org", "category", "why_now", "angle",
                             "previous_note", "video_url", "video_title", "sources"],
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "org": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "why_now": {"type": "string"},
                    "angle": {"type": "string"},
                    "previous_note": {"type": ["string", "null"]},
                    "video_url": {"type": ["string", "null"]},
                    "video_title": {"type": ["string", "null"]},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["title", "url"],
                            "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                        },
                    },
                },
            },
        },
    },
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def week_monday(today=None):
    today = today or dt.datetime.now(dt.timezone.utc).date()
    return today - dt.timedelta(days=today.weekday())


# ---------------------------------------------------------------- memory

def gather_memory(sb):
    """What the agent must know before searching: who is taken, what Craig liked and disliked."""
    if sb is None:
        pilot = json.loads((ROOT / "site" / "data" / "pilot.json").read_text())
        return {"past_guests": ["Yann LeCun (Eye on AI #17, #150)"],
                "already_suggested": [g["name"] for g in pilot["guests"]],
                "liked": [], "disliked": [], "known": []}
    past = [f"{r['name']}" + (f" ({r['detail']})" if r.get("detail") else "")
            for r in sb.select("past_guests", "select=name,detail&order=name")]
    guests = sb.select("guests", "select=id,name,org,category,angle,created_at&order=created_at.desc&limit=400")
    by_id = {g["id"]: g for g in guests}
    fb = sb.select("feedback", "select=guest_id,verdict,note,created_at&order=created_at.asc")
    latest = {}
    for f in fb:
        latest[f["guest_id"]] = f
    liked, disliked, known = [], [], []
    for gid, f in latest.items():
        g = by_id.get(gid)
        if not g:
            continue
        line = f"{g['name']} ({g['org']}, {g['category']})" + (f" — Craig's note: {f['note']}" if f.get("note") else "")
        {"interested": liked, "no": disliked, "known": known}[f["verdict"]].append(line)
    return {
        "past_guests": past,
        "already_suggested": sorted({g["name"] for g in guests}),
        "liked": liked, "disliked": disliked, "known": known,
    }


def memory_text(mem):
    def block(title, items, empty):
        body = "\n".join(f"- {x}" for x in items) if items else f"- ({empty})"
        return f"{title}:\n{body}"
    return "\n\n".join([
        block("Already interviewed on Eye on AI (do not suggest unless there is a strong new reason; then mark as return guest)",
              mem["past_guests"], "none recorded"),
        block("Already suggested in earlier weeks (do not repeat)", mem["already_suggested"], "none yet"),
        block("Craig marked INTERESTED (find more like these)", mem["liked"], "no reactions yet"),
        block("Craig marked NOT FOR ME (avoid this kind of guest; read his notes)", mem["disliked"], "no reactions yet"),
        block("Craig ALREADY KNOWS these people (fine, but prefer fresh names)", mem["known"], "none"),
    ])


# ---------------------------------------------------------------- research (Claude)

def research_prompt(mem, week):
    return f"""Today is {dt.date.today().isoformat()}. You are the guest scout for the Eye on AI podcast. Week of {week}.

{SHOW_BRIEF}

{memory_text(mem)}

Your job this week:
1. Search the web for what actually happened in AI over roughly the last 10 days: notable papers, model or
   chip launches, funding rounds, policy moves, defense developments, scientific results, public debates.
   Prefer primary or reputable sources (company announcements, arXiv/journals, Reuters, FT, TechCrunch,
   The Verge, Nature, Science, NPR, Janes) over aggregator blogs.
2. From that, pick the 6-8 best interview candidates for Craig. Each must be a specific named person
   (not just a company), tied to something that happened recently, and able to carry a serious long-form
   conversation. Spread them across at least four of these categories: {", ".join(CATEGORIES)}.
3. For each candidate, check whether they have already appeared on Eye on AI (search the site eye-on.ai
   for their name). If they have, either drop them or keep them only with a clear new reason, and say so.
4. If you come across a YouTube video of a candidate speaking, note its URL; a separate step will look
   for videos too, so do not spend searches on this.
5. Learn from Craig's reactions above: more of what he liked, less of what he rejected.

Write your findings as plain research notes, one section per candidate, with: name, role, organization,
category, why now (with dates and numbers), a sharp interview angle tailored to Craig, previous-appearance
status, the YouTube URL and its title, and 1-3 source URLs. Then add a 3-sentence overview of the week.
Do not invent facts, URLs or numbers: if a detail is uncertain, say so or leave it out."""


def claude_research(mem, week):
    import anthropic

    client = anthropic.Anthropic(timeout=900, max_retries=3)
    messages = [{"role": "user", "content": research_prompt(mem, week)}]
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 40},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10},
    ]
    resp = None
    for i in range(8):
        log(f"Claude research turn {i + 1}…")
        resp = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=32000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": resp.content}]
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined the research request")
    notes = "\n".join(b.text for b in resp.content if b.type == "text").strip()
    if not notes:
        raise RuntimeError(f"Empty research (stop_reason={resp.stop_reason})")
    return notes


def structure_prompt(notes, week):
    return f"""Below are this week's research notes for the Eye on AI guest radar (week of {week}).
Turn them into the structured format. Rules:
- Keep only candidates that are specific named people with a sourced, recent reason. 6-8 guests, best first.
- why_now: 1-2 sentences, concrete (dates, numbers). angle: 1-2 sentences written for Craig.
- previous_note: null unless they were on Eye on AI before; then e.g. "Return guest — on the show in 2023 (#150)".
- video_url: only a YouTube URL that appears in the notes; otherwise null. video_title likewise.
- sources: only URLs that appear in the notes.
- title: a short headline for the week (max 10 words). summary: 2-3 sentences on the week in AI and the picks.
- briefing_script: a spoken 3-4 minute audio briefing (about 450-550 words) addressed to Craig, warm and
  concise, like a smart producer talking to the host: open with the week's big picture, then one short
  paragraph per guest (who, why now, the angle), close with "Mark the ones you like in the hub."
  No URLs, no markdown, no bullet symbols, numbers written the way they are spoken.

RESEARCH NOTES:
{notes}"""


def claude_structure(notes, week):
    import anthropic

    client = anthropic.Anthropic(timeout=600, max_retries=3)
    resp = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": GUEST_SCHEMA}},
        messages=[{"role": "user", "content": structure_prompt(notes, week)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined the structuring request")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


# ---------------------------------------------------------------- fallback (Gemini)

def gemini(model, body, timeout=600):
    key = os.environ["GEMINI_API_KEY"]
    return http("POST", f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                {"x-goog-api-key": key}, body, timeout=timeout)


def gemini_text(resp):
    parts = resp["candidates"][0]["content"].get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def gemini_research(mem, week):
    log(f"Gemini research ({GEMINI_TEXT_MODEL})…")
    resp = gemini(GEMINI_TEXT_MODEL, {
        "contents": [{"role": "user", "parts": [{"text": research_prompt(mem, week)}]}],
        "tools": [{"google_search": {}}],
    })
    notes = gemini_text(resp)
    # Grounding sources come back as redirect links; list their titles for transparency.
    chunks = resp["candidates"][0].get("groundingMetadata", {}).get("groundingChunks", [])
    if chunks:
        notes += "\n\nSearch sources consulted:\n" + "\n".join(
            f"- {c['web'].get('title', '')}: {c['web'].get('uri', '')}" for c in chunks if "web" in c)
    return notes


def gemini_structure(notes, week):
    log("Gemini structuring…")
    schema = json.loads(json.dumps(GUEST_SCHEMA))
    resp = gemini(GEMINI_TEXT_MODEL, {
        "contents": [{"role": "user", "parts": [{"text": structure_prompt(notes, week)}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema},
    })
    return json.loads(gemini_text(resp))


# ---------------------------------------------------------------- checks

YT_RE = re.compile(r"(?:youtu\.be/|v=|embed/|shorts/)([\w-]{11})")


def check_video(url):
    """Return (canonical_url, title) if the YouTube video exists and is embeddable, else (None, None)."""
    if not url:
        return None, None
    m = YT_RE.search(url)
    if not m:
        return None, None
    canon = f"https://www.youtube.com/watch?v={m.group(1)}"
    try:
        info = http("GET", "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(canon, safe=""), timeout=20)
        return canon, info.get("title")
    except Exception:
        return None, None


def claude_find_videos(guests):
    """Second, cheap pass: one YouTube video per guest that still lacks one. Returns {name: url}."""
    import anthropic

    todo = [g for g in guests if not g.get("video_url")]
    if not todo:
        return {}
    people = "\n".join(f"- {g['name']} ({g['role']}, {g['org']})" for g in todo)
    prompt = f"""For each person below, find ONE YouTube video where that exact person is speaking
(a talk, interview, podcast or panel; recent if possible). Search YouTube for each name.
Only use URLs you actually saw in search results; never construct or guess a video ID.

{people}

Answer with one line per person, exactly in this format and nothing else:
NAME || https://www.youtube.com/watch?v=XXXXXXXXXXX
Use "NAME || none" if you found nothing that clearly shows that person."""
    client = anthropic.Anthropic(timeout=600, max_retries=3)
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search",
              "max_uses": 3 * len(todo), "allowed_domains": ["youtube.com"]}]
    resp = None
    for _ in range(5):
        resp = client.beta.messages.create(
            model=CLAUDE_MODEL, max_tokens=8000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "low"}, tools=tools, messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": resp.content}]
    text = "\n".join(b.text for b in resp.content if b.type == "text")
    found = {}
    for line in text.splitlines():
        if "||" in line:
            name, url = (x.strip(" -*") for x in line.split("||", 1))
            if url.startswith("http"):
                found[name.lower()] = url
    return found


def clean(result, mem):
    taken = {n.lower() for n in mem["already_suggested"]}
    out = []
    for g in result["guests"]:
        if g["name"].lower() in taken:
            log(f"  skip repeat: {g['name']}")
            continue
        url, title = check_video(g.get("video_url"))
        if g.get("video_url") and not url:
            log(f"  video not found for {g['name']}: {g['video_url']}")
        g["video_url"], g["video_title"] = url, (title or None)
        g["sources"] = [s for s in g.get("sources", []) if s.get("url", "").startswith("http")][:3]
        out.append(g)
    result["guests"] = out[:8]
    return result


# ---------------------------------------------------------------- audio

def pcm_from_wav_or_raw(data, mime):
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data)) as w:
            return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
    rate = int(re.search(r"rate=(\d+)", mime).group(1)) if "rate=" in mime else 24000
    return data, rate, 1


def tts_chunk(text):
    resp = gemini(GEMINI_TTS_MODEL, {
        # Send only the script: any style instruction in the text gets read aloud by the TTS model.
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": TTS_VOICE}}},
        },
    }, timeout=300)
    part = resp["candidates"][0]["content"]["parts"][0]["inlineData"]
    return pcm_from_wav_or_raw(base64.b64decode(part["data"]), part.get("mimeType", ""))


def make_audio(script):
    """Voice the script paragraph-group by paragraph-group and encode one mp3. Returns (bytes, seconds)."""
    import lameenc

    paras = [p.strip() for p in script.split("\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) > 1400:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)

    pcm, rate, ch = b"", 24000, 1
    silence = None
    for i, c in enumerate(chunks):
        log(f"  voicing part {i + 1}/{len(chunks)}")
        for attempt in range(7):
            try:
                data, rate, ch = tts_chunk(c)
                break
            except Exception as e:
                if attempt == 6:
                    raise
                wait = min(10 * 2 ** attempt, 120)  # 10s, 20s, 40s, 80s, 120s, 120s
                log(f"  TTS retry in {wait}s ({str(e)[:120]})")
                time.sleep(wait)
        silence = silence or b"\x00\x00" * int(rate * 0.6) * ch
        pcm += data + silence

    enc = lameenc.Encoder()
    enc.set_bit_rate(64)
    enc.set_in_sample_rate(rate)
    enc.set_channels(ch)
    enc.set_quality(2)
    mp3 = enc.encode(pcm) + enc.flush()
    seconds = int(len(pcm) / (2 * ch * rate))
    return bytes(mp3), seconds


# ---------------------------------------------------------------- publish

def publish(sb, week, result, audio_url, seconds, model, force, published=True):
    existing = sb.select("runs", f"select=id&week_of=eq.{week}")
    if existing:
        rid = existing[0]["id"]
        has_fb = sb.select("feedback", f"select=id,guests!inner(run_id)&guests.run_id=eq.{rid}&limit=1")
        if has_fb and not force:
            raise SystemExit(f"Week {week} already published and Craig has reacted to it; use --force to replace.")
        if not force:
            raise SystemExit(f"Week {week} already published; use --force to replace.")
        sb.delete("runs", f"id=eq.{rid}")
        log(f"Replaced existing run {rid}")
    run = sb.insert("runs", [{
        "week_of": week, "title": result["title"], "summary": result["summary"],
        "briefing_script": result["briefing_script"], "audio_url": audio_url,
        "audio_seconds": seconds, "model": model, "published": published,
    }])[0]
    rows = []
    for i, g in enumerate(result["guests"]):
        rows.append({**{k: g.get(k) for k in ("name", "role", "org", "category", "why_now", "angle",
                                                "previous_note", "video_url", "video_title", "sources")},
                     "run_id": run["id"], "position": i})
    sb.insert("guests", rows)
    return run["id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--engine", choices=["auto", "claude", "gemini"], default="auto")
    ap.add_argument("--week", help="Monday to file the run under (YYYY-MM-DD); default: this week")
    ap.add_argument("--hidden", action="store_true", help="save the run unpublished (test runs Craig won't see)")
    args = ap.parse_args()

    load_env()
    week = args.week or week_monday().isoformat()
    sb = None if args.dry_run else Supabase()
    mem = gather_memory(sb)
    log(f"Week {week}: {len(mem['past_guests'])} past guests, {len(mem['already_suggested'])} earlier suggestions, "
        f"{len(mem['liked'])} liked / {len(mem['disliked'])} rejected")

    engine = args.engine
    if engine == "auto":
        engine = "claude" if os.environ.get("ANTHROPIC_API_KEY") else "gemini"
    model = CLAUDE_MODEL if engine == "claude" else GEMINI_TEXT_MODEL
    try:
        if engine == "claude":
            notes = claude_research(mem, week)
            result = claude_structure(notes, week)
        else:
            notes = gemini_research(mem, week)
            result = gemini_structure(notes, week)
    except Exception as e:
        if engine == "claude" and os.environ.get("GEMINI_API_KEY"):
            log(f"Claude failed ({e}); falling back to Gemini")
            model = GEMINI_TEXT_MODEL
            notes = gemini_research(mem, week)
            result = gemini_structure(notes, week)
        else:
            raise

    if engine == "claude" or os.environ.get("ANTHROPIC_API_KEY"):
        try:
            log("Looking for YouTube videos…")
            found = claude_find_videos(result["guests"])
            for g in result["guests"]:
                if not g.get("video_url") and g["name"].lower() in found:
                    g["video_url"] = found[g["name"].lower()]
        except Exception as e:
            log(f"Video search failed, continuing without: {e}")
    result = clean(result, mem)
    log(f"{len(result['guests'])} guests: " + ", ".join(g["name"] for g in result["guests"]))
    if len(result["guests"]) < 3:
        raise SystemExit("Too few usable guests this run; not publishing.")

    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{week}-notes.md").write_text(notes)
    (out_dir / f"{week}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))

    audio_url, seconds = None, None
    if not args.no_audio and os.environ.get("GEMINI_API_KEY"):
        try:
            log("Voicing briefing…")
            mp3, seconds = make_audio(result["briefing_script"])
            (out_dir / f"{week}.mp3").write_bytes(mp3)
            log(f"Audio: {seconds}s, {len(mp3) // 1024} KB")
            if sb:
                audio_url = sb.upload("briefings", f"{week}-{int(time.time())}.mp3", mp3, "audio/mpeg")
        except Exception as e:
            log(f"Audio failed, publishing without it: {e}")

    if sb:
        rid = publish(sb, week, result, audio_url, seconds, model, args.force, published=not args.hidden)
        log(f"Published run {rid} for week {week}")
    else:
        log(f"Dry run saved to {out_dir}")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "agent"))
    main()
