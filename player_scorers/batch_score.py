import argparse
import subprocess
import sys
from pathlib import Path
from datetime import datetime
import csv


def _slugify(text: str) -> str:
    return (
        text.strip().lower().replace(" ", "_").replace("-", "_")
    )


def _build_roster_maps(roster_csv: Path):
    """Return mappings derived from the revised roster CSV columns.

    Expected columns (per data/nhl_roster.csv):
    - team_name, firstName, lastName (case as in file)
    """
    player_to_team = {}
    team_to_slug = {}
    with roster_csv.open(newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            team_name = (row.get("team_name") or row.get("Team") or "").strip()
            first = (row.get("firstName") or row.get("First") or "").strip()
            last = (row.get("lastName") or row.get("Last") or "").strip()
            if not team_name or not first or not last:
                continue
            team_slug = _slugify(team_name)
            team_to_slug[team_name] = team_slug
            player_slug = _slugify(f"{first}_{last}")
            player_to_team[player_slug] = team_slug
    return player_to_team, team_to_slug


def _iter_roster_players(roster_csv: Path):
    """Yield dicts with team_name, team_slug, first, last, player_slug for each roster row
    based on revised nhl_roster.csv."""
    with roster_csv.open(newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            team_name = (row.get("team_name") or row.get("Team") or "").strip()
            first = (row.get("firstName") or row.get("First") or "").strip()
            last = (row.get("lastName") or row.get("Last") or "").strip()
            if not team_name or not first or not last:
                continue
            team_slug = _slugify(team_name)
            player_slug = _slugify(f"{first}_{last}")
            yield {
                "team_name": team_name,
                "team_slug": team_slug,
                "first": first,
                "last": last,
                "player_slug": player_slug,
            }


def main():
    parser = argparse.ArgumentParser(description="Batch score all player summaries using score_runner.py")
    parser.add_argument(
        "--input-dir",
        default=str(Path(__file__).resolve().parent.parent / "player_summaries"),
        help="Directory containing player markdown summaries (default: ./player_summaries)",
    )
    parser.add_argument(
        "--pattern",
        default="*.md",
        help="Glob pattern to match player summary files (default: *.md)",
    )
    parser.add_argument(
        "--skip-llama",
        action="store_true",
        help="Skip the Llama scorer",
    )
    parser.add_argument(
        "--skip-deepseek",
        action="store_true",
        help="Skip the DeepSeek scorer",
    )
    parser.add_argument(
        "--llama-model",
        default=None,
        help="Override model name for Llama (otherwise uses env LLAMA_MODEL or defaults)",
    )
    parser.add_argument(
        "--deepseek-model",
        default=None,
        help="Override model name for DeepSeek (otherwise uses env DEEPSEEK_MODEL or defaults)",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir).expanduser().resolve()
    scorer = Path(__file__).resolve().parent / "score_runner.py"
    if not scorer.exists():
        raise FileNotFoundError(f"score_runner.py not found at {scorer}")
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    # Load roster and build mapping to normalize team/player names
    roster_csv = Path(__file__).resolve().parent.parent / "data" / "nhl_roster.csv"
    player_to_team, team_to_slug = _build_roster_maps(roster_csv)
    roster_players = list(_iter_roster_players(roster_csv))

    # Build an index of available markdowns by stem for quick lookup
    md_files = sorted(input_dir.glob(args.pattern))
    stems_to_paths = {p.stem.lower(): p for p in md_files}
    available_stems = sorted(stems_to_paths.keys())

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Found {len(roster_players)} roster rows. Scoring one per row...")
    successes = 0
    failures = 0
    for idx, pl in enumerate(roster_players, start=1):
        team_slug = pl["team_slug"]
        player_slug = pl["player_slug"]
        player_name = player_slug.replace("_", " ").title()

        # Determine candidate markdown stems for this player
        candidate_stems = [
            f"{team_slug}_{player_slug}",
            player_slug,
        ]
        md = None
        for stem in candidate_stems:
            path = stems_to_paths.get(stem)
            if path:
                md = path
                break
        # If not found by exact stem, try a relaxed search: any stem ending with player_slug
        if md is None:
            for stem in available_stems:
                if stem.endswith(player_slug):
                    md = stems_to_paths[stem]
                    break
        if md is None:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] [{idx}/{len(roster_players)}] Missing summary for {player_name}; expected one of {candidate_stems}. Skipping.")
            continue

        out_filename = f"{team_slug}_{player_slug}.json"
        out_path = input_dir.parent / "ratings" / out_filename
        if out_path.exists():
            print(f"[{datetime.now().isoformat(timespec='seconds')}] [{idx}/{len(roster_players)}] Skipping {player_name} — destination exists: {out_path}")
            continue
        cmd = [
            sys.executable,
            str(scorer),
            "--input",
            str(md),
        ]
        # Ensure score_runner writes to the normalized output path
        cmd.extend(["--output", str(out_path)])
        if args.skip_llama:
            cmd.append("--skip-llama")
        if args.skip_deepseek:
            cmd.append("--skip-deepseek")
        if args.llama_model:
            cmd.extend(["--llama-model", args.llama_model])
        if args.deepseek_model:
            cmd.extend(["--deepseek-model", args.deepseek_model])
        print(f"[{datetime.now().isoformat(timespec='seconds')}] [{idx}/{len(roster_players)}] Scoring {player_name} ({md.name})...")
        try:
            subprocess.run(cmd, check=True)
            print(f"[{datetime.now().isoformat(timespec='seconds')}] [{idx}/{len(roster_players)}] Completed {player_name}")
            successes += 1
        except subprocess.CalledProcessError as e:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] [{idx}/{len(roster_players)}] Failed {player_name} ({md.name}): {e}")
            failures += 1

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Completed. Success: {successes}, Failures: {failures}")


if __name__ == "__main__":
    main()


