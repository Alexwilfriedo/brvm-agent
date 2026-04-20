"""Tests unitaires du parsing JSON de l'enrichment.

On ne mocke pas Anthropic ici — on teste uniquement la robustesse du parsing
défensif (fences, contenu foireux, JSON valide).
"""
import pytest

from src.analysis.enrichment import _strip_fence


@pytest.mark.unit
class TestStripFence:
    def test_pure_json_passes_through(self):
        raw = '{"ticker": "SGBC"}'
        assert _strip_fence(raw) == '{"ticker": "SGBC"}'

    def test_strips_json_fence(self):
        raw = '```json\n{"ticker": "SGBC"}\n```'
        assert _strip_fence(raw) == '{"ticker": "SGBC"}'

    def test_strips_bare_fence(self):
        raw = '```\n{"ticker": "SGBC"}\n```'
        assert _strip_fence(raw) == '{"ticker": "SGBC"}'

    def test_trims_whitespace(self):
        raw = '   \n  {"ticker": "SGBC"}  \n  '
        assert _strip_fence(raw) == '{"ticker": "SGBC"}'

    def test_empty_string(self):
        assert _strip_fence("") == ""

    def test_unterminated_fence(self):
        raw = '```json\n{"ticker": "SGBC"}'
        assert _strip_fence(raw) == '{"ticker": "SGBC"}'
