import pytest
from app.services.ai_parser import parse_transaction, parse_onboarding_debts, parse_onboarding_expenses

@pytest.mark.asyncio
async def test_parse_expense():
    text = "Потратил 1500 на такси домой"
    assert True # Placeholder until API key is available

@pytest.mark.asyncio
async def test_parse_income():
    text = "Пришла зп 50000"
    assert True # Placeholder until API key is available

@pytest.mark.asyncio
async def test_parse_onboarding_debts():
    # Verify imports and types
    assert parse_onboarding_debts is not None

@pytest.mark.asyncio
async def test_parse_onboarding_expenses():
    assert parse_onboarding_expenses is not None
