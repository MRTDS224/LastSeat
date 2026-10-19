import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.models import AgentRule, BookingTransaction
from app.schemas import (
    AgentRuleCreate,
    AgentRuleResponse,
    AgentRuleUpdate,
    BookingTransactionResponse,
)

router = APIRouter(prefix="/api/v1/rules", tags=["Autonomous Agent Rules & Bookings"])


@router.post(
    "",
    response_model=AgentRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new autonomous buying rule for a user",
)
async def create_agent_rule(
    payload: AgentRuleCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AgentRule:
    rule = AgentRule(
        id=uuid.uuid4(),
        user_id=payload.user_id,
        origin=payload.origin,
        destination=payload.destination,
        max_price=payload.max_price,
        currency=payload.currency,
        preferred_cabin=payload.preferred_cabin.value,
        earliest_departure=payload.earliest_departure,
        latest_departure=payload.latest_departure,
        auto_purchase_enabled=payload.auto_purchase_enabled,
        passenger_info=payload.passenger_info.model_dump(),
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return rule


@router.get(
    "",
    response_model=list[AgentRuleResponse],
    summary="List configured autonomous buying rules",
)
async def list_agent_rules(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    user_id: Annotated[str | None, Query()] = None,
    origin: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    active_only: Annotated[bool, Query()] = False,
) -> list[AgentRule]:
    stmt = select(AgentRule).order_by(AgentRule.created_at.desc())
    if user_id:
        stmt = stmt.where(AgentRule.user_id == user_id)
    if origin:
        stmt = stmt.where(AgentRule.origin == origin.upper())
    if active_only:
        stmt = stmt.where(AgentRule.auto_purchase_enabled.is_(True))

    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get(
    "/bookings/transactions",
    response_model=list[BookingTransactionResponse],
    summary="List autonomous booking transactions and Saga states (CONFIRMED / COMPENSATED_FAILED)",
)
async def list_booking_transactions(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    user_id: Annotated[str | None, Query()] = None,
    deal_id: Annotated[str | None, Query()] = None,
    tx_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[BookingTransaction]:
    stmt = (
        select(BookingTransaction)
        .order_by(BookingTransaction.created_at.desc())
        .limit(limit)
    )
    if user_id:
        stmt = stmt.where(BookingTransaction.user_id == user_id)
    if deal_id:
        stmt = stmt.where(BookingTransaction.deal_id == deal_id)
    if tx_status:
        stmt = stmt.where(BookingTransaction.status == tx_status.upper())

    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get(
    "/{rule_id}",
    response_model=AgentRuleResponse,
    summary="Get a specific autonomous buying rule by ID",
)
async def get_agent_rule(
    rule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AgentRule:
    stmt = select(AgentRule).where(AgentRule.id == rule_id)
    rule = (await session.execute(stmt)).scalar_one_or_none()
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AgentRule '{rule_id}' not found.",
        )
    return rule


@router.patch(
    "/{rule_id}",
    response_model=AgentRuleResponse,
    summary="Update an existing autonomous buying rule",
)
async def update_agent_rule(
    rule_id: uuid.UUID,
    payload: AgentRuleUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AgentRule:
    stmt = select(AgentRule).where(AgentRule.id == rule_id)
    rule = (await session.execute(stmt)).scalar_one_or_none()
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AgentRule '{rule_id}' not found.",
        )

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "preferred_cabin" and value is not None:
            setattr(rule, field, value.value)
        else:
            setattr(rule, field, value)

    await session.commit()
    await session.refresh(rule)
    return rule


@router.delete(
    "/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an autonomous buying rule",
)
async def delete_agent_rule(
    rule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    stmt = select(AgentRule).where(AgentRule.id == rule_id)
    rule = (await session.execute(stmt)).scalar_one_or_none()
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AgentRule '{rule_id}' not found.",
        )
    await session.delete(rule)
    await session.commit()

