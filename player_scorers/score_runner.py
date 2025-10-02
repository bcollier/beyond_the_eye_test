import argparse
import asyncio
import json
import os
import re
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import csv

from pydantic import BaseModel, Field, field_validator

from langchain_core.prompts import ChatPromptTemplate
def _setup_logging() -> None:
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', '%Y-%m-%d %H:%M:%S')
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    # Prevent duplicate handlers if re-imported
    if logger.handlers:
        return
    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    # File handler under project logs/
    logs_dir = Path(__file__).resolve().parent.parent / 'logs'
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(logs_dir / 'score_runner.log'))
        fh.setLevel(logging.INFO)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception as e:
        # If file logging fails, continue with console-only
        logger.warning(f"Failed to set up file logging: {e}")

_setup_logging()

from langchain_openai import ChatOpenAI


class ScoreOutput(BaseModel):
    model_name: str = Field(..., description="The model identifier that produced this rating")
    current_rating: int = Field(..., ge=1, le=9, description="Integer 1-9 for current performance")
    future_rating: int = Field(..., ge=1, le=9, description="Integer 1-9 for future potential")
    current_confidence: int = Field(..., ge=0, le=100, description="0-100 confidence for current rating")
    future_confidence: int = Field(..., ge=0, le=100, description="0-100 confidence for future rating")
    reasoning: List[str] = Field(
        ..., min_items=3, max_items=3, description="Exactly three concise bullet points explaining the ratings"
    )
    timestamp: str = Field(..., description="ISO-8601 UTC timestamp of when the score was produced")
    version: str = Field(default="1.0", description="Schema version")

    @field_validator("reasoning")
    @classmethod
    def ensure_three_bullets(cls, v: List[str]) -> List[str]:
        if len(v) != 3:
            raise ValueError("reasoning must have exactly three bullets")
        return v


def _load_system_prompt() -> str:
    # Load system prompt from scorers/score_runner_prompt.md; fallback to default text
    prompt_path = Path(__file__).resolve().parent / "score_runner_prompt.md"
    if prompt_path.exists():
        try:
            prompt_text = prompt_path.read_text(encoding="utf-8").strip()
            logging.info(f"Loaded system prompt from {prompt_path}")
            return prompt_text
        except Exception as e:
            logging.warning(f"Failed reading system prompt at {prompt_path}, using default. Error: {e}")
    else:
        logging.warning(f"System prompt file not found at {prompt_path}, using default prompt text")
    return (
        "You are a neutral hockey player scoring analyst. "
        "Read only the provided MARKDOWN summary about a player (including its tables and any front matter). "
        "Output ONLY valid JSON matching the required schema. "
        "Use integers 1-9 for ratings, confidence 0-100. Provide EXACTLY three concise reasoning bullets. "
        "If data is missing or uncertain, reduce confidence and state the gap succinctly."
    )

SYSTEM_PROMPT = _load_system_prompt()

USER_PROMPT = (
    "Player summary MARKDOWN follows. Use it as your only evidence, do not fabricate.\n\n"
    "{player_summary_markdown}\n\n"
    "Return only the JSON for the following schema fields: model_name, current_rating, future_rating, "
    "current_confidence, future_confidence, reasoning (3 bullets), timestamp (UTC ISO-8601), version."
)


# Note: We read the full markdown and let the model parse tables/front matter as needed.


def make_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("user", USER_PROMPT),
    ])


def make_openai(
    model: str,
    api_key_env: str = "OPENAI_API_KEY",
    temperature: float = 0.2,
    model_kwargs: Optional[Dict[str, Any]] = None,
    reasoning: Optional[Dict[str, Any]] = None,
) -> ChatOpenAI:
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise RuntimeError(f"Missing API key in env var {api_key_env}")
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        temperature=temperature,
        model_kwargs=model_kwargs or {},
        reasoning=reasoning,
    )


def make_jetstream(model: str, temperature: float = 0.2) -> ChatOpenAI:
    """Connect to a private vLLM/OpenAI-compatible endpoint (e.g., Jetstream)."""
    base_url = os.getenv("JETSTREAM_BASE_URL", "https://llm.jetstream-cloud.org/api")
    api_key = os.getenv("JETSTREAM_API_KEY")
    if not api_key:
        logging.error("Missing JETSTREAM_API_KEY for Jetstream/vLLM models")
        raise RuntimeError("Missing JETSTREAM_API_KEY for Jetstream/vLLM models")
    return ChatOpenAI(model=model, api_key=api_key, base_url=base_url, temperature=temperature)


def make_openrouter(model: str, temperature: float = 0.2) -> ChatOpenAI:
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logging.error("Missing OPENROUTER_API_KEY for OpenRouter models")
        raise RuntimeError("Missing OPENROUTER_API_KEY for OpenRouter models")
    return ChatOpenAI(model=model, api_key=api_key, base_url=base_url, temperature=temperature)


def build_chain(llm: ChatOpenAI, model_name: str):
    prompt = make_prompt()
    # Use structured output if supported; LangChain will coerce to Pydantic
    structured_llm = llm.with_structured_output(ScoreOutput)
    chain = prompt | structured_llm

    async def run(player_summary_markdown: str) -> Dict[str, Any]:
        result: ScoreOutput = await chain.ainvoke({
            "player_summary_markdown": player_summary_markdown
        })
        data = result.model_dump()
        # Enforce correct model name and timestamp from client side
        data["model_name"] = model_name
        data["timestamp"] = datetime.now(timezone.utc).isoformat()
        return data

    return run


def _strip_think_blocks(text: str) -> str:
    # Remove <think>...</think> blocks and lone tags (case-insensitive)
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?think>", "", cleaned, flags=re.IGNORECASE)
    return cleaned


def _extract_json_object(text: str) -> Dict[str, Any]:
    # Attempt direct parse
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fenced code block
    m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # First balanced object
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break
    raise ValueError("Could not extract JSON object from model output")


def _load_merged_markdown(primary_path: Path) -> str:
    """Load player summary markdown, merging base and extended reports when present.

    If the selected file is "<name>_extended.md", also load "<name>.md".
    If the selected file is "<name>.md", also load "<name>_extended.md".
    The two documents are concatenated with a horizontal rule separator.

    Also searches a sibling summaries directory if the companion is not
    present in the same directory (e.g., cross-merge from `player_summaries`).
    """
    try:
        primary_text = primary_path.read_text(encoding="utf-8")
    except Exception as e:
        logging.error(f"Failed reading primary summary {primary_path}: {e}")
        primary_text = ""

    stem = primary_path.stem
    suffix = primary_path.suffix
    is_primary_extended = stem.endswith("_extended")
    if is_primary_extended:
        base_stem = stem[: -len("_extended")]
        wanted_name = f"{base_stem}{suffix}"
    else:
        wanted_name = f"{stem}_extended{suffix}"

    # Prefer companion in same directory
    companion = primary_path.with_name(wanted_name)

    merged_text = primary_text
    companion_path: Optional[Path] = None
    if companion.exists() and companion.resolve() != primary_path.resolve():
        companion_path = companion
    else:
        # Search across common summary directories if not found locally
        root_dir = primary_path.parent.parent
        candidate_dirs = [root_dir / "player_summaries"]
        for d in candidate_dirs:
            candidate = d / wanted_name
            try:
                if candidate.exists() and candidate.resolve() != primary_path.resolve():
                    companion_path = candidate
                    break
            except Exception:
                # Ignore resolution errors and keep searching
                pass

    if companion_path is not None:
        try:
            companion_text = companion_path.read_text(encoding="utf-8")
            logging.info(
                f"Merging summaries: primary='{primary_path.name}', companion='{companion_path.name}'"
            )
            merged_text = f"{primary_text}\n\n---\n\n{companion_text}"
        except Exception as e:
            logging.warning(f"Found companion summary but failed to read {companion_path}: {e}")

    return merged_text


def build_chain_plain_json(llm: ChatOpenAI, model_name: str):
    prompt = make_prompt()
    chain = prompt | llm

    async def run(player_summary_markdown: str) -> Dict[str, Any]:
        msg = await chain.ainvoke({
            "player_summary_markdown": player_summary_markdown
        })
        content = getattr(msg, "content", "") or ""
        data_raw = _extract_json_object(content)
        result = ScoreOutput.model_validate(data_raw).model_dump()
        result["model_name"] = model_name
        result["timestamp"] = datetime.now(timezone.utc).isoformat()
        return result

    return run


def build_chain_plain_json_for_deepseek(llm: ChatOpenAI, model_name: str):
    prompt = make_prompt()
    chain = prompt | llm

    async def run(player_summary_markdown: str) -> Dict[str, Any]:
        msg = await chain.ainvoke({
            "player_summary_markdown": player_summary_markdown
        })
        content = getattr(msg, "content", "") or ""
        content = _strip_think_blocks(content)
        data_raw = _extract_json_object(content)
        result = ScoreOutput.model_validate(data_raw).model_dump()
        result["model_name"] = model_name
        result["timestamp"] = datetime.now(timezone.utc).isoformat()
        return result

    return run


async def score_all(
    player_summary_path: Path,
    out_path: Path,
    llama_model_name: str,
    deepseek_model_name: str,
    oss_model_name: str,
    skip_llama: bool,
    skip_deepseek: bool,
    skip_oss: bool,
) -> None:
    markdown = _load_merged_markdown(player_summary_path)

    # Instantiate models
    # Enable OpenAI Reasoning Effort: High for GPT-5
    gpt5 = make_openai("gpt-5", reasoning={"effort": "high"})
    gpt5_mini = make_openai("gpt-5-mini")

    # Configure DeepSeek, Llama, and GPT-OSS via Jetstream
    deepseek_model = deepseek_model_name or os.getenv("DEEPSEEK_MODEL") or "DeepSeek-R1"
    # Normalize common variants to exact casing required by server
    if deepseek_model.lower().replace("_", "-") == "deepseek-r1":
        deepseek_model = "DeepSeek-R1"
    llama_model = llama_model_name or os.getenv("LLAMA_MODEL", "llama-4-scout")
    oss_model = oss_model_name or os.getenv("OSS_MODEL") or "gpt-oss-120b"
    deepseek = make_jetstream(deepseek_model)
    llama = make_jetstream(llama_model)
    oss = make_jetstream(oss_model)
    logging.info(
        f"Configured models: GPT-5, GPT-5-mini, DeepSeek='{deepseek_model}', Llama='{llama_model}', GPT-OSS='{oss_model}' via Jetstream"
    )

    # Build chains
    chains = [
        ("gpt-5", build_chain(gpt5, "gpt-5")),
        ("gpt-5-mini", build_chain(gpt5_mini, "gpt-5-mini")),
    ]
    if not skip_deepseek:
        # DeepSeek often emits <think> blocks; use plain JSON parsing with cleanup
        chains.append((deepseek_model, build_chain_plain_json_for_deepseek(deepseek, deepseek_model)))
    if not skip_llama:
        chains.append((llama_model, build_chain(llama, llama_model)))
    if not skip_oss:
        # Use plain JSON parsing for broad compatibility
        chains.append((oss_model, build_chain_plain_json(oss, oss_model)))

    # Optional: OpenRouter models (Gemini, Opus, Maverick)
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    gemini_model_env = os.getenv("GEMINI_MODEL")
    opus_model_env = os.getenv("OPUS_MODEL")
    if openrouter_key:
        # Gemini 2.5 Pro
        gemini_model = os.getenv("GEMINI_MODEL")
        if gemini_model is None:
            gemini_model = None  # require explicit configuration to avoid 400s
        if gemini_model:
            try:
                gemini_llm = make_openrouter(gemini_model)
                chains.append((gemini_model, build_chain_plain_json(gemini_llm, gemini_model)))
                logging.info(f"Enabled Gemini scorer via OpenRouter: model='{gemini_model}'")
            except Exception as e:
                logging.error(f"Skipping Gemini due to configuration error: {e}")
        else:
            logging.info("OpenRouter detected but GEMINI_MODEL not set; skipping Gemini scorer.")
        # Opus 4.1
        opus_model = os.getenv("OPUS_MODEL")
        if opus_model is None:
            opus_model = None  # require explicit configuration to avoid 400s
        if opus_model:
            try:
                opus_llm = make_openrouter(opus_model)
                chains.append((opus_model, build_chain_plain_json(opus_llm, opus_model)))
                logging.info(f"Enabled Opus scorer via OpenRouter: model='{opus_model}'")
            except Exception as e:
                logging.error(f"Skipping Opus due to configuration error: {e}")
        else:
            logging.info("OpenRouter detected but OPUS_MODEL not set; skipping Opus scorer.")
        # Llama 4 Maverick (enable by default to free tier unless overridden)
        try:
            maverick_model = os.getenv("MAVERICK_MODEL") or "meta-llama/llama-4-maverick:free"
            maverick_llm = make_openrouter(maverick_model)
            chains.append((maverick_model, build_chain_plain_json(maverick_llm, maverick_model)))
            logging.info(f"Enabled Llama Maverick scorer via OpenRouter: model='{maverick_model}'")
        except Exception as e:
            logging.error(f"Skipping Maverick due to configuration error: {e}")
    else:
        logging.info("OPENROUTER_API_KEY not set; skipping OpenRouter scorers (Gemini/Opus/Maverick)")

    # Run in parallel
    model_names = [name for name, _ in chains]
    logging.info(f"Invoking models: {', '.join(model_names)}")
    coros = [fn(markdown) for _, fn in chains]
    gathered = await asyncio.gather(*coros, return_exceptions=True)

    results = []
    errors = []
    for (model_name, _), res in zip(chains, gathered):
        if isinstance(res, Exception):
            err_msg = f"{model_name} failed: {res}"
            logging.error(err_msg)
            errors.append({"model_name": model_name, "error": str(res)})
            continue
        logging.info(f"{model_name} completed successfully")
        results.append(res)

    # Derive a display name from the filename as a fallback (e.g., "sidney_crosby" -> "Sidney Crosby")
    slug = player_summary_path.stem
    player_name = re.sub(r"[_-]+", " ", slug).title() if slug else "unknown"

    aggregate = {
        "player": player_name,
        "ratings": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": "1.0",
    }
    if errors:
        aggregate["errors"] = errors

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info(f"Saved scores to {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Run scorer models over player summaries")
    # Single-file mode
    parser.add_argument("--input", required=False, help="Path to markdown summary file containing JSON front matter")
    parser.add_argument("--output", required=False, help="Path to write aggregate ratings JSON (default: ratings/<slug>.json)")
    # Roster (batch) mode
    parser.add_argument("--from-roster", action="store_true", help="Iterate players from data/nhl_roster.csv and score each if summary exists")
    parser.add_argument("--roster", required=False, help="Path to roster CSV (default: data/nhl_roster.csv)")
    parser.add_argument("--summaries-dir", required=False, help="Directory containing player markdown summaries (default: ./player_summaries_v2)")
    parser.add_argument("--pattern", required=False, default="*.md", help="Glob pattern for summaries (default: *.md)")
    parser.add_argument("--llama-model", required=False, help="Model name for Llama on Jetstream (default env LLAMA_MODEL or 'llama-4-scout')")
    parser.add_argument("--deepseek-model", required=False, help="Model name for DeepSeek on Jetstream (default env DEEPSEEK_MODEL or 'DeepSeek-R1')")
    parser.add_argument("--oss-model", required=False, help="Model name for GPT-OSS on Jetstream (default env OSS_MODEL or 'gpt-oss-120b')")
    parser.add_argument("--gemini-model", required=False, help="OpenRouter model id for Gemini (e.g., google/gemini-2.5-pro)")
    parser.add_argument("--opus-model", required=False, help="OpenRouter model id for Opus (e.g., anthropic/claude-3-opus-20240229)")
    parser.add_argument("--maverick-model", required=False, help="OpenRouter model id for Llama Maverick (default meta-llama/llama-4-maverick:free)")
    parser.add_argument("--skip-llama", action="store_true", help="Skip the Llama scorer")
    parser.add_argument("--skip-deepseek", action="store_true", help="Skip the DeepSeek scorer")
    parser.add_argument("--skip-oss", action="store_true", help="Skip the GPT-OSS scorer")
    args = parser.parse_args()

    # Allow CLI to override env for OpenRouter models
    if args.gemini_model:
        os.environ["GEMINI_MODEL"] = args.gemini_model
    if args.opus_model:
        os.environ["OPUS_MODEL"] = args.opus_model
    if args.maverick_model:
        os.environ["MAVERICK_MODEL"] = args.maverick_model

    # Helper: slugify team/player names
    def _slugify(text: str) -> str:
        return text.strip().lower().replace(" ", "_").replace("-", "_")

    # Batch mode from roster
    if args.from_roster:
        roster_csv = Path(args.roster).expanduser().resolve() if args.roster else Path(__file__).resolve().parent.parent / "data" / "nhl_roster.csv"
        summaries_dir = Path(args.summaries_dir).expanduser().resolve() if args.summaries_dir else Path(__file__).resolve().parent.parent / "player_summaries_v2"
        if not roster_csv.exists():
            raise FileNotFoundError(f"Roster CSV not found: {roster_csv}")
        if not summaries_dir.exists():
            raise FileNotFoundError(f"Summaries directory not found: {summaries_dir}")

        # Index available markdowns
        md_files = sorted(summaries_dir.rglob(args.pattern))
        stems_to_paths = {p.stem.lower(): p for p in md_files}
        available_stems = list(stems_to_paths.keys())

        # Iterate roster
        with roster_csv.open(newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = [r for r in reader if (r.get("team_name") or r.get("Team") or "").strip()]
        total = len(rows)
        logging.info(f"Loaded {total} roster rows from {roster_csv}")
        successes = 0
        failures = 0
        for idx, row in enumerate(rows, start=1):
            team_name = (row.get("team_name") or row.get("Team") or "").strip()
            first = (row.get("firstName") or row.get("First") or "").strip()
            last = (row.get("lastName") or row.get("Last") or "").strip()
            if not team_name or not first or not last:
                logging.info(f"[{idx}/{total}] Skipping row with missing names: {row}")
                continue
            team_slug = _slugify(team_name)
            player_slug = _slugify(f"{first}_{last}")
            player_name = f"{first} {last}"

            candidate_stems = [
                f"{team_slug}_{player_slug}",
                player_slug,
            ]
            md = None
            for stem in candidate_stems:
                md = stems_to_paths.get(stem)
                if md:
                    break
            if md is None:
                # Fallback: any stem that ends with player_slug
                for stem in available_stems:
                    if stem.endswith(player_slug):
                        md = stems_to_paths[stem]
                        break
            if md is None:
                logging.info(f"[{idx}/{total}] Missing summary for {player_name}; expected stems like {candidate_stems}. Skipping.")
                continue

            out_dir = summaries_dir.parent / "ratings"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{team_slug}_{player_slug}.json"
            if out_path.exists():
                logging.info(f"[{idx}/{total}] Skipping {player_name} — exists: {out_path}")
                continue
            logging.info(f"[{idx}/{total}] Scoring {player_name} ({md.name}) — merging base/extended if available")
            try:
                asyncio.run(
                    score_all(
                        md,
                        out_path,
                        args.llama_model,
                        args.deepseek_model,
                        args.oss_model,
                        args.skip_llama,
                        args.skip_deepseek,
                        args.skip_oss,
                    )
                )
                successes += 1
            except Exception as e:
                logging.error(f"[{idx}/{total}] Failed {player_name}: {e}")
                failures += 1
        logging.info(f"Completed. Success: {successes}, Failures: {failures}")
        return

    # Single-file mode (default)
    if not args.input:
        raise RuntimeError("--input is required unless --from-roster is specified")
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    slug = input_path.stem
    default_out = input_path.parent.parent / "ratings" / f"{slug}.json"
    out_path = Path(args.output).expanduser().resolve() if args.output else default_out

    asyncio.run(
        score_all(
            input_path,
            out_path,
            args.llama_model,
            args.deepseek_model,
            args.oss_model,
            args.skip_llama,
            args.skip_deepseek,
            args.skip_oss,
        )
    )


if __name__ == "__main__":
    main()


