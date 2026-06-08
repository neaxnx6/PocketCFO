# -*- coding: utf-8 -*-
import asyncio
import json
from app.services.ai_brain import process_user_message
from test_llm import user_text, chat_history

async def debug_llm():
    res = await process_user_message(
        user_text=user_text,
        user_vibe="Заботливый друг",
        envelopes_context="Статьи расходов:\n- [РАСХОД] 'Нераспределённые': 0 руб",
        financial_health="УРОВЕНЬ: неизвестен",
        chat_history=chat_history
    )
    print("REPLY:", res.coach_reply)
    print("FREE CASH:", res.free_cash)
    if res.envelopes_to_create:
        print("\nCreated Envelopes:")
        for env in res.envelopes_to_create:
            print(f"- {env.name}: current={env.current_amount}, target={env.target_amount}, is_debt={env.is_debt}, is_goal={env.is_goal}, min_payment={env.min_payment}")

if __name__ == "__main__":
    asyncio.run(debug_llm())
