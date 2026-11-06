import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from vaultgen import make_small_vault  # noqa: E402

from mdvault_mcp.index import Vault  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def cache_dir(tmp_path):
    d = tmp_path / "cache"
    d.mkdir()
    return str(d)


@pytest.fixture
def vault_dir(tmp_path):
    d = tmp_path / "vault"
    d.mkdir()
    make_small_vault(str(d))
    return str(d)


@pytest.fixture
def vault(vault_dir, cache_dir):
    v = Vault(vault_dir, cache_dir)
    v.refresh()
    return v
