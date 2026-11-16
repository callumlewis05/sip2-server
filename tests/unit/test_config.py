"""``Settings.from_env``."""

import pytest

from cds_sip2.config import ConfigError, Settings


def test_defaults():
    settings = Settings.from_env({})
    assert settings.bind_host == "127.0.0.1"
    assert settings.port == 3004
    assert settings.error_detection is True
    assert settings.message("unavailable").startswith("Service temporarily unavailable")


def test_reads_prefixed_variables_and_coerces_types():
    settings = Settings.from_env(
        {
            "CDS_SIP2_ILS_URL": "https://ils/api/sip2",
            "CDS_SIP2_PORT": "4000",
            "CDS_SIP2_ILS_READ_TIMEOUT": "2.5",
            "CDS_SIP2_ERROR_DETECTION": "false",
            "CDS_SIP2_LANGUAGE": "",  # empty means default
        }
    )
    assert settings.ils_url == "https://ils/api/sip2"
    assert settings.port == 4000
    assert settings.ils_read_timeout == 2.5
    assert settings.error_detection is False
    assert settings.language == "en"


@pytest.mark.parametrize(
    "env",
    [
        {"CDS_SIP2_PORT": "abc"},
        {"CDS_SIP2_ERROR_DETECTION": "maybe"},
        {"CDS_SIP2_MAX_CONNECTIONS": "0"},
        {"CDS_SIP2_TIMEOUT_PERIOD": "1000"},
        {"CDS_SIP2_ENCODING": "klingon"},
    ],
)
def test_invalid_values_are_rejected(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_settings_are_frozen():
    with pytest.raises(AttributeError):
        Settings().port = 1
    with pytest.raises(TypeError):
        Settings().messages["unavailable"] = "x"
