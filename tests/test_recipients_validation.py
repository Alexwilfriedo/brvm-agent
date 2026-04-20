"""Tests de validation d'adresses pour les recipients (pas de DB)."""
import pytest

from src.api.recipients import _validate_address


@pytest.mark.unit
class TestValidateAddress:
    @pytest.mark.parametrize("email", [
        "alex@karitech.ci",
        "brief+notif@example.com",
        "a.b@c.co",
    ])
    def test_emails_valides(self, email):
        assert _validate_address("email", email) == email

    @pytest.mark.parametrize("email", [
        "pas-un-email",
        "@example.com",
        "alex@",
        "alex@example",
        "alex example@x.com",
    ])
    def test_emails_invalides(self, email):
        with pytest.raises(ValueError, match="email invalide"):
            _validate_address("email", email)

    @pytest.mark.parametrize("phone", [
        "+2250700000000",
        "+33612345678",
        "+14155552671",
    ])
    def test_whatsapp_e164_valides(self, phone):
        assert _validate_address("whatsapp", phone) == phone

    @pytest.mark.parametrize("phone", [
        "0700000000",          # pas de +
        "+",                   # trop court
        "+0700000000",         # commence par 0 après +
        "+225 07 00 00 00 00", # espaces
        "00225700000000",      # format international mais sans +
    ])
    def test_whatsapp_invalides(self, phone):
        with pytest.raises(ValueError, match="E.164"):
            _validate_address("whatsapp", phone)

    def test_trim_whitespace(self):
        assert _validate_address("email", "  alex@x.ci  ") == "alex@x.ci"
