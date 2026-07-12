import pytest
from unittest.mock import MagicMock, AsyncMock

@pytest.fixture
def mock_aiohttp_session():
    session = AsyncMock()
    session.post.return_value.__aenter__.return_value.status = 200
    session.post.return_value.__aenter__.return_value.json = AsyncMock(return_value={})
    
    session.get.return_value.__aenter__.return_value.status = 200
    session.get.return_value.__aenter__.return_value.json = AsyncMock(return_value={})
    
    session.patch.return_value.__aenter__.return_value.status = 200
    session.patch.return_value.__aenter__.return_value.json = AsyncMock(return_value={})
    return session
