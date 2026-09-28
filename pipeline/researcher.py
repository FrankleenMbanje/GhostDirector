import sys
import json
import asyncio
from pathlib import Path
from typing import Dict, Any, List

sys.path.insert(0, str(Path(__file__).parent.parent))
from models import ResearchResult
import config
from utils.logger import get_logger
from utils.retry import retry, pace_llm_call

from google import genai
from google.genai import types
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

logger = get_logger(__name__)


def _format_web_context(results: list[dict], max_results: int = 8) -> str:
    """Format DDG text results into a grounding block for the research prompt."""
    lines = []
    for r in results:
        title = (r.get("title") or "").strip()
        body = (r.get("body") or "").strip()
        href = (r.get("href") or r.get("url") or "").strip()
        if title and body:
            lines.append(f"- {title} — {body[:400]} (source: {href})")
    if not lines:
        return ""
    return "\n".join(lines[:max_results])


def min_facts_for(target_minutes: float) -> int:
    """FIX-064: research depth scales with the format.

    Found live: an 8-minute documentary target was researched to FOUR key
    facts. Four facts cannot carry eight minutes — the script pads, repeats
    itself, and the finished video reads as thin. Depth is a requirement, not
    a hope, so the format declares how much substance it needs.
    """
    if target_minutes >= 5:
        return 24
    if target_minutes >= 3:
        return 14
    return 6


def _merge_facts(existing: list, extra: list) -> list:
    """Merge fact lists, dropping exact and near-duplicate entries.

    A gap-fill pass must only ADD information; if the model rewords facts
    already on file, those are rejected rather than inflating the count.
    """
    def norm(s) -> str:
        return " ".join(str(s).lower().split()).strip(" .,:;")

    seen = {norm(f) for f in existing if norm(f)}
    token_sets = {frozenset(s.split()) for s in seen}
    out = list(existing)
    for f in extra:
        n = norm(f)
        if not n or n in seen:
            continue
        toks = frozenset(n.split())
        if toks in token_sets:      # same words, different order
            continue
        seen.add(n)
        token_sets.add(toks)
        out.append(str(f).strip())
    return out


async def _fill_missing_facts(client, topic: str, known: list, need: int,
                              web_block: str) -> list:
    """Ask the model for ADDITIONAL distinct facts, same grounding.

    FIX-064: never invents — the prompt restates the no-repeat/no-speculation
    rules and is given only the live search results it already had.
    """
    schema = {
        "type": "object",
        "properties": {"key_facts": {"type": "array", "items": {"type": "string"}}},
        "required": ["key_facts"],
    }
    prompt = (
        f"Topic: {topic}\n\nWEB SEARCH RESULTS:\n{web_block}\n\n"
        "FACTS ALREADY ON FILE (do NOT repeat, reword or paraphrase these):\n"
        + ("\n".join(f"- {f}" for f in known[:80]) or "- (none)")
        + f"\n\nAdd {need} MORE distinct, documentary-grade facts about this story.\n"
        "Rules: every fact must be supported by the search results above; each must "
        "carry a concrete specific (date, figure, place, named person or outcome); "
        "no speculation, no invented quotes, no duplicates of the list above."
    )

    def _gen(model_id: str):
        pace_llm_call()
        return client.models.generate_content(
            model=model_id, contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=schema,
            ),
        )

    last_err: Exception | None = None
    for model_id in config.gemini_candidates():
        try:
            resp = await asyncio.to_thread(_gen, model_id)
            txt = (resp.text or "").strip()
            if txt.startswith("```"):
                lines = txt.splitlines()[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                txt = "\n".join(lines).strip()
            return list(json.loads(txt).get("key_facts") or [])
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    return []


async def _web_context(topic: str, max_results: int = 8) -> str:
    """Pull live DDG text snippets for the topic (A26-lite grounding).

    Gives the research model real, current material to work from instead of
    parametric memory — the #1 defense against hallucinated dates/claims on
    celebrity topics. Returns a formatted block or "" on any failure.
    """
    try:
        results = await _ddg_text(topic, max_results)
    except Exception as e:
        logger.warning(f"DDG grounding failed (continuing ungrounded): {e}")
        return ""
    return _format_web_context(results, max_results)


async def _ddg_text(topic: str, max_results: int) -> list[dict]:
    def _sync():
        with DDGS() as ddgs:
            return list(ddgs.text(topic, max_results=max_results))
    return await asyncio.get_running_loop().run_in_executor(None, _sync)


@retry(max_attempts=3, base_delay=2.0)
async def research_topic(topic: str, template: dict) -> ResearchResult:
    """
    Research a topic using the Gemini API grounded in live web snippets.
    Falls back to duckduckgo-search if Gemini fails.
    """
    logger.info(f"Starting research for topic: {topic}")
    try:
        # Using Gemini
        client = genai.Client(api_key=config.GEMINI_API_KEY)

        # Bound once, up front: the depth requirement below needs the format's
        # target duration, and reading it later raised UnboundLocalError —
        # which silently degraded EVERY research call to the DuckDuckGo
        # fallback (headlines as "facts", no people) before this fix.
        script_cfg = (template or {}).get("script", {})
        
        # Schema definition for Gemini structured output
        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "timeline": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "event": {"type": "string"}
                        },
                        "required": ["date", "event"]
                    }
                },
                "key_people": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "search_query": {"type": "string"}
                        },
                        "required": ["name", "role", "search_query"]
                    }
                },
                "key_facts": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "narrative_arc": {"type": "string"}
            },
            "required": ["summary", "timeline", "key_people", "key_facts", "narrative_arc"]
        }
        
        # FIX-064: depth target from the format (long-form docs need volume).
        try:
            _target_min = float(script_cfg.get("target_duration_minutes") or 0)
        except (TypeError, ValueError):
            _target_min = 0.0
        _deep = _target_min >= 5
        _min_facts = min_facts_for(_target_min)

        # Ground the research in live web snippets (A26): dates, figures and
        # claims should come from these sources, not from model memory.
        web_block = await _web_context(topic, max_results=16 if _deep else 8)
        grounding = (
            f"\nWEB SEARCH RESULTS (ground every date, number and claim in these; "
            f"do not add events you cannot support from them):\n{web_block}\n"
            if web_block
            else "\nNOTE: No live web results available — only include facts you are highly confident are publicly documented.\n"
        )

        # Style hints only — never dump the whole template JSON into the prompt.
        script_cfg = (template or {}).get("script", {})
        style_hint = f"Tone: {script_cfg.get('tone', 'documentary')}. Structure: {', '.join(script_cfg.get('structure', []))}."

        depth_hint = (
            f"\nDEPTH REQUIREMENT (this is a {_target_min:.0f}-minute documentary):\n"
            f"- Give AT LEAST {_min_facts} key_facts. Each fact must carry a concrete "
            "specific (a date, figure, place, named person or outcome) and be "
            "traceable to the web results.\n"
            "- NEVER restate the same fact in different words: every entry must add "
            "new information.\n"
            "- Give at least 12 timeline entries covering cause → escalation → "
            "current state, and name EVERY relevant person (lawyers, executives, "
            "officials, witnesses) with their role.\n"
            "- Cover the money and the mechanics: what is being claimed, by whom, "
            "under which law or rule, what it costs, and what happens next.\n"
            if _deep else ""
        )
        prompt = (
            f"Research the following topic for a YouTube documentary script: {topic}\n\n"
            f"{style_hint}\n{grounding}\n{depth_hint}"
            "Provide a comprehensive summary, a timeline of events (with real dates), "
            "key people involved (full names + roles), key facts (specific numbers, "
            "dates, dollar figures), and a narrative arc with a clear midpoint turn."
        )
        
        # Transient 503s from the model backends are common; retry the same
        # model once before degrading to the fallback chain. Free-tier RPM
        # (5/min on some keys) is paced cross-module via pace_llm_call.
        def _gen(model_id: str):
            pace_llm_call()
            last_err = None
            for attempt in range(2):
                try:
                    return client.models.generate_content(
                        model=model_id,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=schema,
                        ),
                    )
                except Exception as gen_err:
                    last_err = gen_err
                    if "503" in str(gen_err) or "UNAVAILABLE" in str(gen_err).upper():
                        import time as _t
                        _t.sleep(2.0 * (attempt + 1))
                        continue
                    raise
            raise last_err

        # Walk the full verified chain (FIX-036 evolution): any single id can
        # 404, exhaust its own daily bucket, or 503 under load — the chain
        # keeps research alive through all three.
        response = None
        last_err: Exception | None = None
        for model_id in config.gemini_candidates():
            try:
                response = await asyncio.to_thread(_gen, model_id)
                break
            except Exception as p_err:
                last_err = p_err
                # Self-heal (FIX-036): if Google retired the model for this
                # key, re-verify available models with live probes once.
                if "404" in str(p_err) and "no longer available" in str(p_err):
                    logger.warning(
                        "Research model retired for this API key — "
                        "re-verifying available models with live probes..."
                    )
                    try:
                        config.verify_gemini_models()
                    except Exception as cfg_err:
                        logger.warning(f"Model re-verification failed: {cfg_err}")
                logger.warning(
                    f"Research model {model_id} failed: {str(p_err)[:140]}. "
                    f"Trying next candidate in chain..."
                )
        if response is None:
            raise last_err if last_err else RuntimeError("No Gemini candidates available")
        
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()
        data = json.loads(raw_text)
        logger.info(f"Successfully researched topic with Gemini: {topic}")

        # FIX-064: one gap-fill pass when a documentary still came in thin.
        # Same grounding, same rules — this only ADDS distinct facts.
        facts = list(data.get("key_facts", []) or [])
        if _deep and len(facts) < _min_facts:
            logger.warning(
                f"FIX-064: research returned {len(facts)} facts for a "
                f"{_target_min:.0f}-min doc (want ≥{_min_facts}) — gap-fill pass")
            try:
                extra = await _fill_missing_facts(
                    client, topic, facts, _min_facts, web_block)
                if extra:
                    merged = _merge_facts(facts, extra)
                    logger.info(f"FIX-064: gap-fill added {len(merged) - len(facts)} facts "
                                f"({len(merged)} total)")
                    facts = merged
            except Exception as gap_err:
                logger.warning(f"FIX-064 gap-fill failed (non-fatal): {str(gap_err)[:140]}")

        return ResearchResult(
            topic=topic,
            summary=data.get("summary", ""),
            timeline=data.get("timeline", []),
            key_people=data.get("key_people", []),
            key_facts=facts,
            narrative_arc=data.get("narrative_arc", ""),
            source_queries=[topic]
        )
        
    except Exception as e:
        logger.warning(f"Gemini research failed: {e}. Falling back to DuckDuckGo search.")
        return await fallback_research(topic)

async def fallback_research(topic: str) -> ResearchResult:
    """
    Fallback research using duckduckgo-search.
    """
    try:
        ddgs = DDGS()
        # Perform a text search synchronously wrapped in to_thread since DDGS is sync
        results = await asyncio.to_thread(lambda: list(ddgs.text(topic, max_results=5)))
        
        summary_parts = []
        for r in results:
            summary_parts.append(r.get('body', ''))
            
        summary = " ".join(summary_parts)
        
        logger.info(f"Successfully researched topic with DuckDuckGo: {topic}")
        return ResearchResult(
            topic=topic,
            summary=summary,
            timeline=[],
            key_people=[],
            key_facts=[r.get('title', '') for r in results],
            narrative_arc="Fallback generated narrative based on web search.",
            source_queries=[topic]
        )
    except Exception as e:
        logger.error(f"Fallback research also failed: {e}")
        raise
