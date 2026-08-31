import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOY = ROOT / "domains" / "toy"


@pytest.fixture
def toy():
    from autoresearch.config import DomainConfig
    return DomainConfig.load(TOY)
