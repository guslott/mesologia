from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple
import subprocess
import unicodedata

from tf.fabric import Fabric

BHSA_DATA_DIR = Path(__file__).resolve().parent / "bhsa" / "tf"
BHSA_REPO_URL = "https://github.com/ETCBC/bhsa.git"
DEFAULT_MODULE = os.environ.get("BHSA_MODULE", "2021")
PROGRESS_INTERVAL = 50_000

#Potent words?
# WORD_TO_SEARCH = "יהוה"
WORD_TO_SEARCH = "שלומ"

#Control words?
# WORD_TO_SEARCH = "ציון" #Zion, 1x (Jer 2:7)
# WORD_TO_SEARCH = "תורה" #Torah, 0x
# WORD_TO_SEARCH = "ברית" #Berit/Covenant, 1x Psalm 65:14
# WORD_TO_SEARCH = "יעקב" #Yaakov/Jacob, 0x
# WORD_TO_SEARCH = "ארבע" #Arba/Four, 2x Psalm 119:135, Job 15:26
# WORD_TO_SEARCH = "משפט" #Mishpat/Judgment, 0x
# WORD_TO_SEARCH = "חכמה" #Chokhmah/Wisdom, 0x
# WORD_TO_SEARCH = "אדאמ"  # Adam/Human, 2x Num 14:7, Jer 14:17
# WORD_TO_SEARCH = "דודי"  # Dodi/My beloved, 2x Lev 5:7, Lev 25:28


FIRST_WORD_SUFFIX = WORD_TO_SEARCH[:2]  # "יה"
SECOND_WORD_PREFIX = WORD_TO_SEARCH[2:]  # "וה"

FINAL_FORM_TRANSLATION = str.maketrans(
    {
        "ך": "כ",
        "ם": "מ",
        "ן": "נ",
        "ף": "פ",
        "ץ": "צ",
    }
)


def normalize_for_match(text: str) -> str:
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    normalized = unicodedata.normalize("NFC", stripped)
    return normalized.translate(FINAL_FORM_TRANSLATION)


@dataclass(frozen=True)
class WordSpan:
    nodes: Tuple[int, ...]
    consonantal: str
    pointed: str
    reference: str


@dataclass(frozen=True)
class InterWordMatch:
    first: WordSpan
    second: WordSpan
    context: str


def ensure_bhsa_repo() -> None:
    """Clone the BHSA repository locally if it is missing."""
    tf_dir = BHSA_DATA_DIR
    repo_dir = tf_dir.parent

    if tf_dir.exists():
        return

    if repo_dir.exists() and not any(repo_dir.iterdir()):
        # Directory exists but is empty; remove it so git clone succeeds.
        repo_dir.rmdir()

    if repo_dir.exists():
        # Something exists but not the expected TF data; let the user resolve.
        raise SystemExit(
            f"BHSA directory exists at {repo_dir} but is missing the expected TF data "
            f"under {tf_dir}. Please fix or remove it and rerun."
        )

    print(f"BHSA data not found at {tf_dir}. Downloading from {BHSA_REPO_URL} ...")
    try:
        result = subprocess.run(
            ["git", "clone", "--depth=1", BHSA_REPO_URL, str(repo_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise SystemExit(
            "Error: git is required to download the BHSA repository automatically."
        ) from error
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            "Error: failed to download the BHSA repository automatically.\n"
            f"stdout:\n{error.stdout}\n\nstderr:\n{error.stderr}"
        ) from error

    if not tf_dir.exists():
        raise SystemExit(
            f"Downloaded BHSA repository but could not find TF data at {tf_dir}."
        )

    print(result.stdout.strip() or "✓ BHSA repository downloaded successfully.")
    print()


def available_modules() -> List[str]:
    ensure_bhsa_repo()
    if not BHSA_DATA_DIR.exists():
        raise SystemExit(
            f"Cannot find BHSA data directory at {BHSA_DATA_DIR}. "
            "Make sure the bhsa repository is present."
        )
    return sorted(
        p.name for p in BHSA_DATA_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


def choose_module(preferred: str | None) -> str:
    modules = available_modules()
    if not modules:
        raise SystemExit(f"No Text-Fabric modules found in {BHSA_DATA_DIR}.")

    if preferred and preferred in modules:
        return preferred

    if preferred and preferred not in modules:
        print(
            f"Requested module '{preferred}' not found. "
            f"Available modules: {', '.join(modules)}. "
            f"Falling back to '{modules[-1]}'."
        )

    return modules[-1]


def load_bhsa(module: str) -> Any:
    print(f"Loading BHSA module '{module}' from {BHSA_DATA_DIR} ...")
    TF = Fabric(locations=str(BHSA_DATA_DIR), modules=module)
    api = TF.load(
        """
        otype
        g_cons_utf8
        g_word_utf8
        book
        chapter
        verse
        """.strip()
    )

    if not api:
        raise SystemExit("Error: could not load BHSA Text-Fabric features.")

    print("✓ BHSA data loaded successfully.\n")
    return api


def format_reference(section: Iterable | None) -> str:
    if not section:
        return "Unknown reference"
    book, chapter, verse = section
    return f"{book} {chapter}:{verse}"


def collect_matches(api) -> List[InterWordMatch]:
    F = api.F
    T = api.T

    word_nodes = F.otype.s("word")
    total_words = len(word_nodes)

    print(f"Scanning {total_words:,} words for {FIRST_WORD_SUFFIX}···{SECOND_WORD_PREFIX} pairs...")

    suffix_target = normalize_for_match(FIRST_WORD_SUFFIX)
    prefix_target = normalize_for_match(SECOND_WORD_PREFIX)

    matches: List[InterWordMatch] = []

    def build_span(nodes: Sequence[int]) -> WordSpan:
        node_tuple = tuple(nodes)
        pointed = "".join(F.g_word_utf8.v(n) or "" for n in node_tuple)
        consonantal = "".join(F.g_cons_utf8.v(n) or "" for n in node_tuple)
        reference = format_reference(T.sectionFromNode(node_tuple[0]))
        return WordSpan(nodes=node_tuple, consonantal=consonantal, pointed=pointed, reference=reference)

    def second_span_nodes(start_index: int, prefix: str) -> Optional[Tuple[int, ...]]:
        if not prefix:
            return None

        nodes: List[int] = []
        remaining = prefix
        idx = start_index

        while idx < total_words and remaining:
            node = word_nodes[idx]
            cons = F.g_cons_utf8.v(node) or ""
            norm = normalize_for_match(cons)
            if not norm:
                return None

            if norm.startswith(remaining):
                nodes.append(node)
                remaining = ""
                break

            if remaining.startswith(norm):
                nodes.append(node)
                remaining = remaining[len(norm) :]
                idx += 1
                continue

            return None

        return tuple(nodes) if not remaining else None

    for idx, first_node in enumerate(word_nodes[:-1]):
        if idx and idx % PROGRESS_INTERVAL == 0:
            print(f"  Progress: {idx:,}/{total_words:,} words examined...")

        first_cons = F.g_cons_utf8.v(first_node) or ""
        first_norm = normalize_for_match(first_cons)
        if suffix_target and not first_norm.endswith(suffix_target):
            continue

        span_nodes = second_span_nodes(idx + 1, prefix_target)

        if not span_nodes:
            continue

        first_span = build_span([first_node])
        second_span = build_span(span_nodes)

        span_length = len(span_nodes)
        context_start = max(idx - 3, 0)
        context_end = min(idx + 1 + span_length + 3, total_words)
        context_slice = word_nodes[context_start:context_end]
        context_words = [F.g_word_utf8.v(node) or "" for node in context_slice]
        context = " ".join(word for word in context_words if word).strip()

        matches.append(InterWordMatch(first=first_span, second=second_span, context=context))

    print(f"\nCompleted scanning. Found {len(matches)} matching pairs.\n")
    return matches


def display_results(matches: Sequence[InterWordMatch]) -> None:
    print("=" * 60)
    print(f"INTER-WORD INSTANCES OF {FIRST_WORD_SUFFIX} + {SECOND_WORD_PREFIX}")
    print("=" * 60)
    print()

    for idx, match in enumerate(matches, start=1):
        same_ref = match.first.reference == match.second.reference
        reference = (
            match.first.reference
            if same_ref
            else f"{match.first.reference} → {match.second.reference}"
        )
        print(f"{idx}. {reference}")
        print(
            f"   {match.first.pointed} ({match.first.consonantal}) + "
            f"{match.second.pointed} ({match.second.consonantal})"
        )
        print(f"   Context: {match.context}")
        print()

    if not matches:
        print("No matches found.")

    print("=" * 60)
    print()


def summarize_by_book(matches: Sequence[InterWordMatch]) -> None:
    summary = defaultdict(int)
    for match in matches:
        book = match.first.reference.split()[0]
        summary[book] += 1

    if not summary:
        return

    print("=" * 60)
    print("SUMMARY BY BOOK")
    print("=" * 60)
    for book in sorted(summary):
        count = summary[book]
        print(f"{book:20s}: {count:3d} instance{'s' if count != 1 else ''}")
    print()


def spotlight_song_of_songs(matches: Sequence[InterWordMatch]) -> None:
    target_titles = {"Canticum", "Canticles", "Song", "SongOfSongs", "Songs"}
    filtered = [
        match
        for match in matches
        if any(match.first.reference.startswith(title) for title in target_titles)
    ]

    if not filtered:
        return

    print("=" * 60)
    print("SONG OF SONGS INSTANCES")
    print("=" * 60)
    for match in filtered:
        print(match.first.reference)
        print(
            f"  {match.first.pointed}{match.second.pointed} "
            f"({match.first.consonantal}{match.second.consonantal})"
        )
        print(f"  Context: {match.context}")
        print()


def main() -> None:
    module = choose_module(DEFAULT_MODULE)
    api = load_bhsa(module)
    matches = collect_matches(api)
    display_results(matches)
    summarize_by_book(matches)
    spotlight_song_of_songs(matches)
    print("=" * 60)
    print("SEARCH COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
