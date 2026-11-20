"""Module-level ``CDS_SIP2_*`` defaults and ``Settings.from_env``."""

import pytest

from cds_sip2 import config
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
        {"CDS_SIP2_TIMEOUT_PERIOD": "100"},
        {"CDS_SIP2_REQUEST_DEADLINE": "15"},  # not below the advertised 15 s
        {"CDS_SIP2_ENCODING": "klingon"},
        {"CDS_SIP2_DEFAULT_MEDIA_TYPE": "book"},  # a SIP2 code is expected
        {"CDS_SIP2_SECURITY_MARKER": "9"},
        {"CDS_SIP2_TERMINALS": "not json"},
        {"CDS_SIP2_TERMINALS": '["kiosk1"]'},
        {"CDS_SIP2_TERMINALS": '{"kiosk1": {"password": "p"}}'},
        {"CDS_SIP2_TERMINALS": '{"kiosk1": {"password": "", "user_pid": "1"}}'},
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


def test_every_module_variable_is_a_setting():
    names = {name for name in vars(config) if name.startswith("CDS_SIP2_")}
    assert {f"CDS_SIP2_{name.upper()}" for name in config.defaults()} == names
    assert Settings().ils_url == config.CDS_SIP2_ILS_URL


def test_terminals_come_from_json_and_do_not_leak_into_errors():
    settings = Settings.from_env(
        {"CDS_SIP2_TERMINALS": '{"kiosk1": {"password": "s3cr3t", "user_pid": 42}}'}
    )
    assert settings.terminals["kiosk1"]["user_pid"] == 42
    with pytest.raises(ConfigError) as info:
        Settings.from_env({"CDS_SIP2_TERMINALS": '{"kiosk1": {"password": "s3cr3t"'})
    assert "s3cr3t" not in str(info.value)


def test_messages_override_merges_over_defaults():
    settings = Settings.from_env({"CDS_SIP2_MESSAGES": '{"unknown_item": "Inconnu."}'})
    assert settings.message("unknown_item") == "Inconnu."
    assert settings.message("unavailable").startswith("Service temporarily unavailable")


def test_timeout_period_is_advertised_in_tenths():
    assert Settings().advertised_timeout == "150"
    assert Settings(timeout_period=2.5, request_deadline=2).advertised_timeout == "025"
