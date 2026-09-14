from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from ha_nuc9_ec.cli import _run
from ha_nuc9_ec.mqtt import prepare_mqtt


@pytest.mark.asyncio
async def test_invalid_ca_fails_78_before_linux_backend_open(example_config, tmp_path, monkeypatch):
    bad_ca = tmp_path / 'bad-ca.pem'
    bad_ca.write_text('not a certificate')
    raw = example_config.model_dump(mode='python')
    raw['mqtt'].update(enabled=True, broker='ssl://localhost:8883', username=None,
                       password_file=None, tls={'ca_file': str(bad_ca)})
    config = type(example_config).model_validate(raw, context={'normalized_duration': True})
    opened = False
    def forbidden(*args, **kwargs):
        nonlocal opened
        opened = True
        raise AssertionError('hardware opened')
    monkeypatch.setattr('ha_nuc9_ec.hardware.linux.LinuxBackend.open', forbidden)
    args = SimpleNamespace(backend='linux', sys_root=Path('/sys'), lock_path=None)
    assert await _run(args, config, tmp_path / 'config.yaml') == 78
    assert not opened


@pytest.mark.asyncio
async def test_mismatched_client_key_is_rejected_before_backend_without_prompt(example_config, tmp_path, monkeypatch):
    cert = tmp_path / 'client.crt'
    key = tmp_path / 'client.key'
    other_key = tmp_path / 'other.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=test', '-keyout', key, '-out', cert], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['openssl', 'genrsa', '-out', other_key, '2048'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    raw = example_config.mqtt.model_dump(mode='python')
    raw.update(enabled=True, broker='ssl://localhost:8883', username=None, password_file=None,
               tls={'ca_file': str(cert), 'certificate_file': str(cert), 'key_file': str(other_key)})
    mqtt_config = type(example_config.mqtt).model_validate(raw)
    with pytest.raises(ValueError, match='invalid MQTT TLS certificate configuration'):
        prepare_mqtt(mqtt_config)
    app_raw = example_config.model_dump(mode='python')
    app_raw['mqtt'] = mqtt_config.model_dump(mode='python')
    config = type(example_config).model_validate(app_raw, context={'normalized_duration': True})
    opened = False
    def forbidden(*args, **kwargs):
        nonlocal opened
        opened = True
        raise AssertionError('hardware opened')
    monkeypatch.setattr('ha_nuc9_ec.hardware.linux.LinuxBackend.open', forbidden)
    args = SimpleNamespace(backend='linux', sys_root=Path('/sys'), lock_path=None)
    assert await _run(args, config, tmp_path / 'config.yaml') == 78
    assert not opened
