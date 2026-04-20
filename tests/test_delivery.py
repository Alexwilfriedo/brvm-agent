"""Tests du rendu email — pas d'envoi SMTP réel."""
import pytest

from src.delivery.email_brevo import render_email_html
from src.delivery.sample_brief import sample_brief, sample_snapshot
from src.delivery.whatsapp import format_brief_short


@pytest.mark.unit
class TestRenderEmail:
    def test_brief_vide_signale_absence_dans_sujet(self):
        subject, html = render_email_html({}, "Lundi 01 janvier 2026")
        assert "aucun signal fort" in subject
        assert "Brief BRVM" in html
        assert "Brief BRVM · Lundi 01 janvier 2026" in subject

    def test_brief_avec_opportunite_met_le_ticker_en_sujet(self):
        brief = sample_brief()
        subject, html = render_email_html(
            brief, "Mardi 22 avril 2026", market_snapshot=sample_snapshot()
        )
        # Le top ticker doit apparaître dans le sujet
        assert "BOAC" in subject
        assert "achat" in subject.lower()
        # Et dans le HTML
        assert "Bank of Africa" in html
        assert "Sonatel" in html  # 2e opportunité
        assert "Invalidation" in html
        assert "Catalyseurs" in html
        assert "Tendance" in html or "Range" in html  # badge régime

    def test_variante_empty_affiche_skip_reasons(self):
        brief = {
            "market_summary": "Marché calme.",
            "opportunities": [],
            "skip_reasons": "Pas de catalyseur aujourd'hui — mieux vaut attendre.",
        }
        _, html = render_email_html(brief, "Mer. 23 avril 2026")
        assert "Note de l'analyste" in html
        assert "Pas de catalyseur" in html

    def test_variante_error_affiche_banniere_degradee(self):
        brief = {
            "opportunities": [],
            "_error": True,
            "skip_reasons": "anthropic: 529 Overloaded",
        }
        subject, html = render_email_html(brief, "Jeu. 24 avril 2026")
        assert subject.startswith("[DEGRADÉ]")
        assert "synthèse automatique a échoué" in html

    def test_snapshot_affiche_gainers_et_losers(self):
        brief = sample_brief()
        _, html = render_email_html(
            brief, "Ven. 25 avril 2026", market_snapshot=sample_snapshot()
        )
        assert "Hausses" in html
        assert "Baisses" in html
        # Var_pct formatée avec signe
        assert "+2.10%" in html or "+2,10%" in html

    def test_xss_echappe_dans_thesis(self):
        """Autoescape Jinja2 doit neutraliser les scripts injectés."""
        brief = {
            "market_summary": "<script>alert('xss')</script>",
            "opportunities": [{
                "ticker": "XXX",
                "name": "<img src=x onerror=alert(1)>",
                "direction": "buy",
                "thesis": "<script>bad()</script>",
                "conviction": 3,
            }],
        }
        _, html = render_email_html(brief, "Sam. 26 avril 2026")
        # Les balises dangereuses doivent être échappées (inertes comme texte)
        assert "<script>" not in html
        assert "<img src=x onerror" not in html
        assert "&lt;script&gt;" in html
        assert "&lt;img src=x onerror" in html


@pytest.mark.unit
class TestFormatWhatsApp:
    def test_conserve_taille_raisonnable(self):
        brief = {
            "market_summary": "x" * 500,
            "opportunities": [
                {"ticker": f"T{i}", "direction": "buy", "conviction": 3, "thesis": "y" * 200}
                for i in range(10)
            ],
            "alerts": ["a" * 100 for _ in range(5)],
        }
        out = format_brief_short(brief)
        assert len(out) < 2000
        assert "Brief BRVM" in out
