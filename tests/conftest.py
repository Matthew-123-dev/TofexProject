import sys
import os
import pytest
from fastapi.testclient import TestClient

# Add the app directory to the Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.main import app, get_db

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as client:
        yield client

@pytest.fixture(scope="module")
async def async_client():
    async with TestClient(app) as client:
        yield client
