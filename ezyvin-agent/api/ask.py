"""
Ezyvin API-assistent – Vercel serverless function
Håller Anthropic API-nyckeln säkert på servern via miljövariabeln ANTHROPIC_API_KEY.
"""

from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.error
import os
import re
import time

DOCS_URL = "https://ezyvin.com/openapi/v1.json"

# Tak för dokumentationens längd i tecken. Kapa aldrig tyst — se fetch_docs.
# Ezyvin-speccen är idag liten (~15 KB) så taket lär aldrig nås, men skyddet
# finns kvar om speccen växer.
DOCS_MAX_CHARS = 400_000
DOCS_TTL_SECONDS = 600

# Module-level cache – lever under warm starts på samma instans
_docs_cache: str | None = None
_docs_cache_ts: float = 0.0


def _openapi_to_text(data: dict) -> str:
    """Konverterar en OpenAPI JSON-spec till läsbar text för modellen."""
    lines = []

    info = data.get("info", {})
    lines.append(f"# {info.get('title', 'Ezyvin API')}")
    if info.get("description"):
        lines.append(info["description"])
    lines.append(f"Version: {info.get('version', '')}\n")

    servers = data.get("servers", [])
    if servers:
        lines.append("## Base URLs")
        for s in servers:
            lines.append(f"- {s.get('url', '')}  {s.get('description', '')}")
        lines.append("")

    components = data.get("components", {})
    security_schemes = components.get("securitySchemes", {})
    if security_schemes:
        lines.append("## Autentisering")
        for name, scheme in security_schemes.items():
            lines.append(f"**{name}:** typ={scheme.get('type','')}  "
                         f"in={scheme.get('in','')}  namn={scheme.get('name','')}")
            if scheme.get("description"):
                lines.append(scheme["description"])
        lines.append("")

    paths = data.get("paths", {})
    if paths:
        lines.append("## Endpoints")
        for path, methods in paths.items():
            for method, details in methods.items():
                if method not in ("get", "post", "put", "delete", "patch"):
                    continue
                lines.append(f"\n### {method.upper()} {path}")
                if details.get("summary"):
                    lines.append(f"**Sammanfattning:** {details['summary']}")
                if details.get("description"):
                    lines.append(f"**Beskrivning:** {details['description']}")

                params = details.get("parameters", [])
                if params:
                    lines.append("**Parametrar:**")
                    for p in params:
                        req_flag = " *(obligatorisk)*" if p.get("required") else ""
                        ptype = p.get("schema", {}).get("type", "")
                        desc = p.get("description", "")
                        lines.append(f"- `{p.get('name')}` ({p.get('in')}, {ptype}{req_flag}): {desc}")

                responses = details.get("responses", {})
                if responses:
                    lines.append("**Svar:**")
                    for code, resp in responses.items():
                        lines.append(f"- {code}: {resp.get('description', '')}")

    schemas = components.get("schemas", {})
    if schemas:
        lines.append("\n## Datamodeller")
        for name, schema in schemas.items():
            lines.append(f"\n### {name}")
            if schema.get("description"):
                lines.append(schema["description"])
            props = schema.get("properties", {})
            if props:
                lines.append("**Fält:**")
                for prop_name, prop in props.items():
                    ptype = prop.get("type", "")
                    pdesc = prop.get("description", "")
                    lines.append(f"- `{prop_name}` ({ptype}): {pdesc}")

    return "\n".join(lines)


def _html_to_text(html: str) -> str:
    """Fallback om URL:en någon gång returnerar HTML i stället för JSON."""
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def fetch_docs() -> str:
    """Hämtar Ezyvins OpenAPI-spec och konverterar till läsbar text."""
    global _docs_cache, _docs_cache_ts
    if _docs_cache and (time.time() - _docs_cache_ts) < DOCS_TTL_SECONDS:
        return _docs_cache

    req = urllib.request.Request(
        DOCS_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; EzyvinBot/1.0)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception:
        if _docs_cache:  # hellre gammal cache än inget alls
            return _docs_cache
        raise

    # URL:en returnerar OpenAPI-JSON — konvertera strukturerat. Tidigare
    # HTML-strippades innehållet, vilket gav modellen svårläst rå-JSON.
    try:
        result = _openapi_to_text(json.loads(raw))
    except (json.JSONDecodeError, AttributeError, TypeError):
        result = _html_to_text(raw)

    if len(result) > DOCS_MAX_CHARS:
        # Kapa aldrig tyst — tala om för modellen att slutet saknas så den
        # kan säga "vet ej" i stället för att gissa om det som föll bort.
        result = (
            result[:DOCS_MAX_CHARS]
            + "\n\n[OBS: Dokumentationen är TRUNKERAD här. Innehåll efter denna "
            "punkt är INTE tillgängligt — svara \"Den informationen finns inte i "
            "den tillgängliga dokumentationen\" om frågan gäller något du inte "
            "ser ovan.]"
        )
    _docs_cache = result
    _docs_cache_ts = time.time()
    return _docs_cache


def build_system_blocks(docs: str, tone: str, include_refs: bool) -> list:
    """Systemprompt som block-array: regler + docs separat, så docs-blocket
    kan prompt-cachas (90 % rabatt på cachade input-tokens)."""
    tone_instr = (
        "Svara tekniskt och precist. Använd korrekt terminologi, visa kodexempel i "
        "kodblock (curl, Python, JavaScript), och var detaljerad. Anta att läsaren är en utvecklare."
        if tone == "technical"
        else "Svara pedagogiskt och engagerande. Förklara med analogier och enkla termer "
        "som för någon helt ny till API:et. Undvik onödig jargong."
    )
    ref_instr = (
        "Inkludera källreferenser när du kunnat belägga svaret – ange vilken endpoint, "
        "parameter eller sektion i dokumentationen du hänvisar till. Utelämna "
        "referenser för sådant du inte kunnat belägga."
        if include_refs
        else "Inkludera INGA källreferenser eller hänvisningar till dokumentationssektioner. "
        "Ge bara det direkta svaret."
    )
    rules = (
        "Du är en hjälpsam AI-assistent specialiserad på Ezyvins API.\n\n"
        "REGLER (obligatoriska):\n"
        "1. Besvara ENBART frågor baserat på dokumentationen nedan.\n"
        "2. Om svaret inte framgår av dokumentationen, säg tydligt: "
        "\"Den informationen finns inte i den tillgängliga dokumentationen.\" "
        "Använd inte frasen när du redan kunnat besvara frågan.\n"
        "3. Hitta inte på endpoints, parametrar eller beteenden som inte framgår av dokumentationen.\n"
        "4. Beläggnings-regel: innan du bekräftar att en endpoint, parameter eller "
        "ett fält finns — lokalisera den exakta raden i dokumentationen. Kan du "
        "inte peka på var det står, behandla det som att det INTE finns. Att något "
        "\"borde\" finnas i ett fordons-API räknas inte som belägg.\n"
        "5. Svara på svenska om inte användaren skriver på ett annat språk.\n"
        f"6. {tone_instr}\n"
        f"7. {ref_instr}"
    )
    docs_block = (
        f"API-DOKUMENTATION (källa: {DOCS_URL}):\n"
        + "─" * 60 + "\n"
        + docs + "\n"
        + "─" * 60
    )
    return [
        {"type": "text", "text": rules},
        {"type": "text", "text": docs_block, "cache_control": {"type": "ephemeral"}},
    ]


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            self._respond(500, {"error": "API-nyckel saknas på servern. Kontakta administratören."})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._respond(400, {"error": "Ogiltig förfrågan."})
            return

        question = body.get("question", "").strip()
        tone = body.get("tone", "technical")
        include_refs = bool(body.get("includeRefs", True))

        if not question:
            self._respond(400, {"error": "Fråga saknas."})
            return

        try:
            docs = fetch_docs()
        except Exception as exc:
            self._respond(502, {"error": f"Kunde inte hämta API-dokumentationen: {exc}"})
            return

        system = build_system_blocks(docs, tone, include_refs)

        # Sonnet 5: bättre grundning i lång kontext än 4.6, kampanjpris t.o.m. 31 aug 2026.
        # OBS: skicka INTE temperature — parametern är utfasad för Sonnet 5.
        payload = json.dumps({
            "model": "claude-sonnet-5",
            "max_tokens": 2048,
            "system": system,
            "messages": [{"role": "user", "content": question}],
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            # Sonnet 5 kan returnera flera content-block (t.ex. thinking + text).
            # Plocka ut alla textblock i stället för att blint läsa [0]["text"].
            answer = "\n".join(
                block.get("text", "")
                for block in result.get("content", [])
                if block.get("type") == "text"
            ).strip()
            if not answer:
                self._respond(500, {"error": "Tomt svar från modellen."})
                return
            self._respond(200, {"answer": answer})

        except urllib.error.HTTPError as exc:
            try:
                err_body = json.loads(exc.read())
                msg = err_body.get("error", {}).get("message", f"HTTP {exc.code}")
            except Exception:
                msg = f"HTTP {exc.code}"
            self._respond(500, {"error": f"Anthropic-fel: {msg}"})

        except Exception as exc:
            self._respond(500, {"error": f"Oväntat fel: {exc}"})

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _respond(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass
