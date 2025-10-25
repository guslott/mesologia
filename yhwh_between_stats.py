"""Compute adjacency statistics for Hebrew lexical pairs in the BHSA corpus."""

from __future__ import annotations

import argparse
import math
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from tf.fabric import Fabric

ROOT = Path(__file__).resolve().parent
BHSA_TF_DIR = ROOT / "bhsa" / "tf"
DEFAULT_MODULE = os.environ.get("BHSA_MODULE", "2021")
# DEFAULT_TARGET_WORD = os.environ.get("BHSA_TARGET_WORD", "יהוה")
DEFAULT_TARGET_WORD = os.environ.get("BHSA_TARGET_WORD", "שלומ")
DEFAULT_SUMMARY_BOOK = os.environ.get("BHSA_SUMMARY_BOOK")

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
    """Strip vowel marks, cantillation, and letter-final variants for comparisons."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    normalized = unicodedata.normalize("NFC", stripped)
    return normalized.translate(FINAL_FORM_TRANSLATION)


def strip_diacritics(text: str) -> str:
    """Remove Hebrew vowel and cantillation marks, along with maqaf."""
    text = text.replace("\u05be", "")  # maqaf
    return normalize_for_match(text)


@dataclass(frozen=True)
class WordInfo:
    text: str
    book: str


@dataclass(frozen=True)
class PatternStats:
    scope: str
    total_words: int
    suffix_count: int
    prefix_count: int
    expected_adjacent: float
    observed_adjacent: int
    suffix_percent: float
    prefix_percent: float
    ratio: float
    p_value: float


def load_words(module: str = DEFAULT_MODULE) -> tuple[Any, list[WordInfo]]:
    """Load every word in the BHSA corpus along with its book metadata."""
    if not BHSA_TF_DIR.exists():
        raise SystemExit(
            f"BHSA Text-Fabric data not found at {BHSA_TF_DIR}. "
            "Clone https://github.com/ETCBC/bhsa into this directory."
        )

    TF = Fabric(locations=str(BHSA_TF_DIR), modules=module)
    api = TF.load("otype g_word_utf8 book chapter verse", silent=True)
    if not api:
        raise SystemExit("Failed to load BHSA Text-Fabric data.")

    F = api.F
    T = api.T
    word_nodes = F.otype.s("word")
    words: list[WordInfo] = []
    for node in word_nodes:
        raw = F.g_word_utf8.v(node) or ""
        section = T.sectionFromNode(node)
        book = section[0] if section else "Unknown"
        words.append(WordInfo(text=strip_diacritics(raw), book=book))
    return api, words


def _suffix_flags(words: Sequence[WordInfo], suffix_target: str) -> list[bool]:
    if not suffix_target:
        return [True] * len(words)
    return [word.text.endswith(suffix_target) for word in words]


def _prefix_flags(words: Sequence[WordInfo], prefix_target: str) -> list[bool]:
    total = len(words)
    flags = [False] * total
    if not prefix_target:
        return flags

    for start in range(total):
        remaining = prefix_target
        idx = start
        matched = False

        while idx < total and remaining:
            segment = words[idx].text
            if not segment:
                break

            if segment.startswith(remaining):
                matched = True
                remaining = ""
                break

            if remaining.startswith(segment):
                matched = True
                remaining = remaining[len(segment) :]
                idx += 1
                continue

            break

        if not remaining and matched:
            flags[start] = True

    return flags


def _poisson_tail(observed: int, lam: float) -> float:
    if lam <= 0:
        return 0.0 if observed > 0 else 1.0
    if observed <= 0:
        return 1.0

    term = math.exp(observed * math.log(lam) - lam - math.lgamma(observed + 1))
    tail = term
    k = observed
    while True:
        k += 1
        term *= lam / k
        tail += term
        if term < 1e-12:
            break
    return min(1.0, tail)


def compute_pattern_stats(
    scope: str,
    words: Sequence[WordInfo],
    suffix_target: str,
    prefix_target: str,
) -> PatternStats:
    """Summarize how frequently the suffix/prefix pattern appears."""
    total_words = len(words)
    if total_words == 0:
        return PatternStats(scope, 0, 0, 0, 0.0, 0, 0.0, 0.0, math.inf, 1.0)

    suffix_flags = _suffix_flags(words, suffix_target)
    prefix_flags = _prefix_flags(words, prefix_target)

    suffix_count = sum(suffix_flags)
    prefix_count = sum(prefix_flags)

    probability_suffix = suffix_count / total_words
    probability_prefix = prefix_count / total_words

    expected_adjacent = (total_words - 1) * probability_suffix * probability_prefix
    observed_adjacent = sum(
        1
        for idx in range(total_words - 1)
        if suffix_flags[idx] and prefix_flags[idx + 1]
    )

    ratio = observed_adjacent / expected_adjacent if expected_adjacent else math.inf
    p_value = _poisson_tail(observed_adjacent, expected_adjacent)

    return PatternStats(
        scope=scope,
        total_words=total_words,
        suffix_count=suffix_count,
        prefix_count=prefix_count,
        expected_adjacent=expected_adjacent,
        observed_adjacent=observed_adjacent,
        suffix_percent=probability_suffix * 100,
        prefix_percent=probability_prefix * 100,
        ratio=ratio,
        p_value=p_value,
    )


def display_stats(stats: PatternStats, suffix_target: str, prefix_target: str) -> None:
    """Pretty-print a PatternStats record."""
    print(f"==== {stats.scope} ====")
    print(f"Total words: {stats.total_words:,}")
    print(
        f"Words ending with {suffix_target}: {stats.suffix_count:,} "
        f"({stats.suffix_percent:.4f}%)"
    )
    print(
        f"Words beginning with {prefix_target}: {stats.prefix_count:,} "
        f"({stats.prefix_percent:.4f}%)"
    )
    print(f"Expected adjacent occurrences (random order): {stats.expected_adjacent:.4f}")
    print(f"Observed adjacent occurrences: {stats.observed_adjacent:,}")
    print(f"Observed/Expected ratio: {stats.ratio:.4f}")
    print(f"P-value (Poisson tail): {stats.p_value:.6f}")
    print()


def resolve_targets(
    word: str | None,
    suffix_target: str | None,
    prefix_target: str | None,
    split_index: int | None,
) -> tuple[str, str]:
    """Return normalized suffix/prefix targets based on CLI input."""
    normalized_word = normalize_for_match(word or "")

    suffix = normalize_for_match(suffix_target) if suffix_target else ""
    prefix = normalize_for_match(prefix_target) if prefix_target else ""

    if suffix and prefix:
        return suffix, prefix

    if not normalized_word:
        raise ValueError(
            "Provide --word (or BHSA_TARGET_WORD) unless both --suffix-target and "
            "--prefix-target are supplied."
        )

    if len(normalized_word) < 2:
        raise ValueError("The normalized target word must contain at least two letters.")

    length = len(normalized_word)
    if split_index is None:
        split_idx = max(1, min(length - 1, length // 2 or 1))
    else:
        if not 0 < split_index < length:
            raise ValueError(
                f"--split-index must fall between 1 and {length - 1} for '{word}'."
            )
        split_idx = split_index

    resolved_suffix = suffix or normalized_word[:split_idx]
    resolved_prefix = prefix or normalized_word[split_idx:]

    if not resolved_suffix or not resolved_prefix:
        raise ValueError("Both suffix and prefix must be non-empty after normalization.")

    return resolved_suffix, resolved_prefix


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Create an argument parser for CLI use."""
    parser = argparse.ArgumentParser(
        description="Quantify how often BHSA words form a particular split target."
    )
    parser.add_argument(
        "--module",
        default=DEFAULT_MODULE,
        help="Text-Fabric BHSA module to load (default: %(default)s).",
    )
    parser.add_argument(
        "--word",
        default=DEFAULT_TARGET_WORD,
        help="Target Hebrew word used to infer suffix/prefix (default: %(default)s).",
    )
    parser.add_argument(
        "--split-index",
        type=int,
        default=None,
        help="Index at which to split --word into suffix/prefix (1-based from left). "
        "By default the word midpoint is used.",
    )
    parser.add_argument(
        "--suffix-target",
        dest="suffix",
        help="Explicit suffix override for the first word (after normalization).",
    )
    parser.add_argument(
        "--prefix-target",
        dest="prefix",
        help="Explicit prefix override for the following word(s).",
    )
    parser.add_argument(
        "--book",
        default=DEFAULT_SUMMARY_BOOK,
        help="Optional BHSA book name for a focused summary.",
    )
    parser.add_argument(
        "--skip-book",
        action="store_true",
        help="Skip the book-level summary even if --book is provided.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        suffix_target, prefix_target = resolve_targets(
            word=args.word,
            suffix_target=args.suffix,
            prefix_target=args.prefix,
            split_index=args.split_index,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))

    _, words = load_words(args.module)
    overall_stats = compute_pattern_stats(
        "BHSA (all books)", words, suffix_target, prefix_target
    )

    book_stats: PatternStats | None = None
    book_scope = None
    if not args.skip_book and args.book:
        book_scope = args.book
        book_words = [word for word in words if word.book == book_scope]
        if not book_words:
            print(f"Warning: book '{book_scope}' not found in the dataset; skipping.")
        else:
            book_stats = compute_pattern_stats(
                book_scope, book_words, suffix_target, prefix_target
            )

    print(f"Pattern: {suffix_target} … {prefix_target} (source word: {args.word})\n")
    display_stats(overall_stats, suffix_target, prefix_target)
    if book_stats:
        display_stats(book_stats, suffix_target, prefix_target)

    print("Summary:")
    print(
        f"In {overall_stats.scope}, randomness predicts ~{overall_stats.expected_adjacent:.4f} "
        f"transitions; we observe {overall_stats.observed_adjacent} (p={overall_stats.p_value:.6f})."
    )
    if book_stats:
        print(
            f"In {book_stats.scope}, the expected count is ~{book_stats.expected_adjacent:.4f}, "
            f"with {book_stats.observed_adjacent} observed (p={book_stats.p_value:.6f})."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
