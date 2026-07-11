import pytest
from unittest.mock import AsyncMock, patch
from src.notion.client import NotionClient

@pytest.fixture
def mock_notion_client():
    with patch('src.notion.client.AsyncClient') as MockAsyncClient:
        # Create an instance of AsyncMock for the return_value
        mock_instance = AsyncMock()
        MockAsyncClient.return_value = mock_instance
        
        client = NotionClient()
        client.email_db_id = "test-db-id"
        yield client, mock_instance

@pytest.mark.asyncio
async def test_create_page(mock_notion_client):
    client, mock_async_client = mock_notion_client
    
    # 设置 mock 返回值
    mock_async_client.pages.create = AsyncMock(return_value={"id": "new-page-id", "object": "page"})
    # Mock data_sources as well since get_data_source_id uses it
    mock_async_client.databases.retrieve = AsyncMock(return_value={"data_sources": [{"id": "ds-id"}]})
    
    properties = {"Name": {"title": [{"text": {"content": "Test"}}]}}
    children = [{"object": "block", "type": "paragraph"}]
    
    result = await client.create_page(properties, children)
    
    assert result["id"] == "new-page-id"
    mock_async_client.pages.create.assert_called_once()
    args, kwargs = mock_async_client.pages.create.call_args
    assert kwargs["parent"]["data_source_id"] == "ds-id"
    assert kwargs["properties"] == properties
    assert kwargs["children"] == children

@pytest.mark.asyncio
async def test_query_database(mock_notion_client):
    client, mock_async_client = mock_notion_client
    
    mock_async_client.data_sources.query = AsyncMock(return_value={
        "results": [{"id": "page-1"}, {"id": "page-2"}],
        "has_more": False,
        "next_cursor": None
    })
    # Mock databases.retrieve for get_data_source_id
    mock_async_client.databases.retrieve = AsyncMock(return_value={"data_sources": [{"id": "ds-id"}]})
    
    filter_conditions = {"property": "Status", "select": {"equals": "Done"}}
    results = await client.query_database(filter_conditions)
    
    assert len(results) == 2
    assert results[0]["id"] == "page-1"
    mock_async_client.data_sources.query.assert_called_once()
    args, kwargs = mock_async_client.data_sources.query.call_args
    assert kwargs["data_source_id"] == "ds-id"
    assert kwargs["filter"] == filter_conditions
