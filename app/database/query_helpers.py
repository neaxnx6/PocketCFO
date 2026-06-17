from datetime import datetime
from sqlalchemy import select, func
from app.database.models import Transaction, BudgetSyncAdjustment

async def get_monthly_payments(session, envelope_ids: list[int]) -> dict[int, float]:
    """
    Calculates the sum of positive transactions (payments/allocations) 
    for each envelope in the current calendar month.
    """
    if not envelope_ids:
        return {}
        
    start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    stmt = (
        select(Transaction.envelope_id, func.sum(Transaction.amount))
        .where(
            Transaction.envelope_id.in_(envelope_ids),
            Transaction.datetime_created >= start_of_month,
            Transaction.amount > 0
        )
        .group_by(Transaction.envelope_id)
    )
    result = await session.execute(stmt)
    return {env_id: amount for env_id, amount in result.all() if env_id is not None}


async def get_monthly_spending(session, envelope_ids: list[int]) -> dict[int, float]:
    """
    Calculates the sum of negative transactions (spending/expenses)
    for each envelope in the current calendar month. Returns positive values (absolute sums).
    """
    if not envelope_ids:
        return {}
        
    start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    stmt = (
        select(Transaction.envelope_id, func.sum(Transaction.amount))
        .where(
            Transaction.envelope_id.in_(envelope_ids),
            Transaction.datetime_created >= start_of_month,
            Transaction.amount < 0
        )
        .group_by(Transaction.envelope_id)
    )
    result = await session.execute(stmt)
    return {env_id: abs(amount) for env_id, amount in result.all() if env_id is not None}


async def get_monthly_adjustments(session, envelope_ids: list[int]) -> dict[int, float]:
    """
    Retrieves active BudgetSyncAdjustment amounts for each envelope for the current month.
    """
    if not envelope_ids:
        return {}
    current_month_str = datetime.utcnow().strftime("%Y-%m")
    stmt = (
        select(BudgetSyncAdjustment.envelope_id, BudgetSyncAdjustment.amount)
        .where(
            BudgetSyncAdjustment.envelope_id.in_(envelope_ids),
            BudgetSyncAdjustment.month == current_month_str
        )
    )
    result = await session.execute(stmt)
    return {env_id: amount for env_id, amount in result.all() if env_id is not None}

