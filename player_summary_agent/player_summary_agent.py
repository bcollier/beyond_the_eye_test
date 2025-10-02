# https://platform.openai.com/docs/guides/tools-web-search?api-mode=responses
import os
import random
import openai
import csv
import re
import unicodedata
import time
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler

client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
gpt_model = "gpt-5"

# Base directory of this script; use relative paths so it runs on any machine/VM
BASE_DIR = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = BASE_DIR / "player_summary_agent.md"
ENHANCER_PROMPT_PATH = BASE_DIR / "player_summary_agent_enhancer.md"
roster_path = BASE_DIR / "data" / "nhl_roster.csv"
sample_roster_path = BASE_DIR / "data" / "nhl_roster_sample.csv"

# Configure robust logging (file + console) with rotation
logs_dir = BASE_DIR / "logs"
os.makedirs(logs_dir, exist_ok=True)
log_file_path = logs_dir / "player_summary_agent.log"

logger = logging.getLogger("player_summary_agent")
logger.setLevel(logging.INFO)

if not logger.handlers:
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        filename=str(log_file_path), maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
    system_prompt = f.read()

# Load Sonar deep research enhancer prompt template (optional)
try:
    with open(ENHANCER_PROMPT_PATH, "r", encoding="utf-8") as f:
        enhancer_template = f.read()
except FileNotFoundError:
    enhancer_template = None
    logger.warning(f"Enhancer prompt file not found at {ENHANCER_PROMPT_PATH}. Using fallback prompt.")
except Exception:
    enhancer_template = None
    logger.exception("Failed to load enhancer prompt; using fallback prompt.")

def slugify_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "player"

def parse_usage(resp):
    # Token usage tracking removed
    return None, None, None

sample_size = 500
if sample_roster_path.exists():
    # Load precomputed sample for reproducibility
    with open(sample_roster_path, newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        rows = [
            r
            for r in reader
            if (r.get("team_name") or "").strip()
            and (((r.get("firstName") or "").strip()) or ((r.get("lastName") or "").strip()))
        ]
    total = len(rows)
    logger.info(
        f"Loaded roster sample with {total} rows from {sample_roster_path} (no resampling)"
    )
else:
    # Create a new sample from the full roster and persist it
    with open(roster_path, newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        full_rows = [
            r
            for r in reader
            if (r.get("team_name") or "").strip()
            and (((r.get("firstName") or "").strip()) or ((r.get("lastName") or "").strip()))
        ]
    if len(full_rows) > sample_size:
        seed_raw = os.getenv("PLAYER_SAMPLE_SEED")
        if seed_raw is not None and seed_raw.strip() != "":
            try:
                random.seed(int(seed_raw))
            except Exception:
                random.seed(seed_raw)
        rows = random.sample(full_rows, sample_size)
    else:
        rows = full_rows

    # Persist the sampled set for subsequent runs
    try:
        fieldnames = ["team_name", "firstName", "lastName"]
        os.makedirs(sample_roster_path.parent, exist_ok=True)
        with open(sample_roster_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: (r.get(k) or "").strip() for k in fieldnames})
        logger.info(
            f"Wrote roster sample with {len(rows)} rows to {sample_roster_path}"
        )
    except Exception:
        logger.exception(
            f"Failed to write roster sample to {sample_roster_path}; proceeding without persisted sample"
        )
    total = len(rows)
    logger.info(f"Loaded roster file with {total} rows to process from {roster_path}")

# Process all rows (applies whether we loaded a saved sample or just created one)
for idx, row in enumerate(rows, start=1):
    team_name = (row.get("team_name") or "").strip()
    first_name = (row.get("firstName") or "").strip()
    last_name = (row.get("lastName") or "").strip()
    player_name = (f"{first_name} {last_name}").strip() or first_name or last_name

    logger.info(f"[{idx}/{total}] Starting summary for {player_name} — {team_name}")

    # Determine output path and skip if file already exists
    # Prefix filename with team to disambiguate and follow requested format
    slug = slugify_name(f"{team_name} {player_name}")
    out_dir = BASE_DIR / "player_summaries"
    os.makedirs(out_dir, exist_ok=True)
    filename = out_dir / f"{slug}.md"
    enhanced_path = out_dir / f"{slug}_extended.md"
    if filename.exists() and enhanced_path.exists():
        logger.info(
            f"[{idx}/{total}] Skipping {player_name} — base and extended files exist"
        )
        continue

    try:
        user_message = f"Research and produce the full markdown summary for player: {player_name} ({team_name})."
        start_time = time.time()
        # Allow configuring reasoning effort and text verbosity via env vars
        reasoning_effort = "high"
        text_verbosity = "high"
        response = client.responses.create(
            model=gpt_model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            tools=[{"type": "web_search"}],
            reasoning={"effort": reasoning_effort},
            text={"verbosity": text_verbosity}
        )
        elapsed_s = time.time() - start_time
        research_output = getattr(response, "output_text", "") or ""
        with open(filename, "w", encoding="utf-8") as f:
            f.write(research_output)
        # Second-pass enhancement via Perplexity Sonar Deep Research (OpenRouter)
        try:
            base_url_or = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
            openrouter_key = os.getenv("OPENROUTER_API_KEY")
            if openrouter_key:
                logger.info(f"Enhancing report with Sonar Deep Research for {player_name}")
                # Include recommended headers for OpenRouter routing/attribution
                default_headers = {}
                referer = os.getenv("OPENROUTER_SITE_URL")
                if referer:
                    default_headers["HTTP-Referer"] = referer
                default_headers["X-Title"] = "player-summary-agent"
                or_client = openai.OpenAI(
                    base_url=base_url_or,
                    api_key=openrouter_key,
                    default_headers=default_headers,
                )
                stub = Path(filename).read_text(encoding="utf-8")
                # Build the sonar prompt from external template if available; otherwise fallback to legacy inline template
                if enhancer_template:
                    if "{stub}" in enhancer_template:
                        sonar_prompt = enhancer_template.replace("{stub}", stub)
                    elif "{{STUB}}" in enhancer_template:
                        sonar_prompt = enhancer_template.replace("{{STUB}}", stub)
                    else:
                        sonar_prompt = f"{enhancer_template}\n\n{stub}"
                else:
                    sonar_prompt = (
                        "You area deep research analyst for a professional hockey team. Below is the stub of research about a hockey player. Search the web for all available information about this player and fill in any missing data values."
                        "Extend this research to be as exhaustive as possible. Return the completed research of everything from the stub as well as any new research in markdown format. add any new sources to the Sources section \n\n" + stub
                    )
                # Execute with simple retry to handle transient gateway/JSON parse errors
                sonar_content = ""
                max_attempts = 2
                for attempt in range(1, max_attempts + 1):
                    try:
                        sonar = or_client.chat.completions.create(
                            model="perplexity/sonar-deep-research",
                            messages=[
                                {"role": "user", "content": sonar_prompt},
                            ],
                        )
                        sonar_content = (
                            getattr(sonar.choices[0].message, "content", None) or ""
                        ).strip()
                        break
                    except Exception:
                        logger.exception(
                            f"Sonar request failed (attempt {attempt}/{max_attempts}) for {player_name}"
                        )
                        if attempt < max_attempts:
                            time.sleep(10)
                        else:
                            break

                if sonar_content:
                    enhanced_path = out_dir / f"{slug}_extended.md"
                    enhanced_path.write_text(sonar_content, encoding="utf-8")
                    logger.info(f"Enhanced report saved to {enhanced_path}")
            else:
                logger.info("OPENROUTER_API_KEY not set; skipping Sonar deep research enhancement")
        except Exception:
            logger.exception(f"Sonar deep research enhancement failed for {player_name}")
        logger.info(
            f"[{idx}/{total}] Saved report to {filename} | time: {elapsed_s:.2f}s"
        )
    except Exception:
        logger.exception(f"[{idx}/{total}] Error while generating summary for {player_name} — {team_name}")
        # Brief backoff before next player
        time.sleep(5)
        continue

    logger.info("Sleeping for 65 seconds...")
    time.sleep(65)

logger.info(
    f"Completed {total} players."
)