# SPDX-License-Identifier: Apache-2.0
"""Unrestricted ICU analysis with initial English and Chinese validation profiles."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import cast

from meridian_storage.semantics import JsonValue, sha256_fingerprint

_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def normalize_language(value: str) -> str:
    if not isinstance(value, str) or _LANGUAGE.fullmatch(value) is None:
        raise ValueError("language metadata must be a bounded BCP 47 language tag")
    parts = value.split("-")
    return "-".join((parts[0].lower(), *(part.lower() for part in parts[1:])))


def analyzer_for_language(language: str | None) -> str:
    """Select an analyzer without restricting future language metadata."""

    if language is None:
        return "meridian_icu"
    normalized = normalize_language(language)
    if normalized == "en" or normalized.startswith("en-"):
        return "meridian_en"
    if normalized == "zh" or normalized.startswith("zh-"):
        return "meridian_zh"
    return "meridian_icu"


@dataclass(frozen=True, slots=True)
class AnalysisDefinition:
    languages: tuple[str, ...]
    settings: Mapping[str, JsonValue]

    @property
    def fingerprint(self) -> str:
        return sha256_fingerprint(
            cast(JsonValue, {"languages": list(self.languages), "settings": self.settings})
        )


def build_analysis(languages: Iterable[str]) -> AnalysisDefinition:
    normalized = tuple(sorted({normalize_language(value) for value in languages}))
    if not normalized:
        raise ValueError("at least one language hint is required")
    # Every language uses ICU. English adds exact, language-specific stemming;
    # Chinese and all other languages deliberately keep ICU's language-neutral path.
    settings: dict[str, JsonValue] = {
        "analyzer": {
            "meridian_en": {
                "type": "custom",
                "char_filter": [],
                "tokenizer": "icu_tokenizer",
                "filter": [
                    "meridian_en_possessive",
                    "lowercase",
                    "meridian_en_stop",
                    "meridian_en_stemmer",
                    "meridian_icu_folding",
                ],
            },
            "meridian_icu": {
                "type": "custom",
                "char_filter": [],
                "tokenizer": "icu_tokenizer",
                "filter": ["lowercase", "meridian_icu_folding"],
            },
            "meridian_zh": {
                "type": "custom",
                "char_filter": [],
                "tokenizer": "icu_tokenizer",
                "filter": ["lowercase", "meridian_icu_folding"],
            },
        },
        "filter": {
            "meridian_en_possessive": {"type": "stemmer", "language": "possessive_english"},
            "meridian_en_stemmer": {"type": "stemmer", "language": "english"},
            "meridian_en_stop": {"type": "stop", "stopwords": "_english_"},
            "meridian_icu_folding": {"type": "icu_folding"},
        },
        "normalizer": {
            "meridian_keyword": {
                "type": "custom",
                "char_filter": [],
                "filter": ["icu_normalizer", "lowercase"],
            }
        },
    }
    return AnalysisDefinition(normalized, settings)


__all__ = [
    "AnalysisDefinition",
    "analyzer_for_language",
    "build_analysis",
    "normalize_language",
]
