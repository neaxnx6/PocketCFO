from litellm import acompletion
from app.config import settings

async def generate_coach_response(user_vibe: str, transaction_desc: str, amount: float, category: str, action: str, current_balance: float = 0.0) -> str:
    action_ru = "потратили" if action == "expense" else "получили"
    
    system_prompt = (
        f"Ты финансовый коуч Pocket CFO. Твоя модель общения: '{user_vibe}'. "
        "К тебе пришел клиент со своей транзакцией. "
        "Твоя задача — подтвердить запись транзакции, и дать короткий микро-совет в соответствии с твоей личностью. "
        "Не будь слишком многословным. "
        "Обязательно включи WOW-эффект: покажи, что ты 'умный' и понимаешь контекст жизни пользователя."
    )
    
    user_prompt = (
        f"Данные о транзакции: {action_ru} {amount} руб. Категория: {category}. Описание: {transaction_desc}. "
        f"Баланс в конверте после этого: {current_balance} руб. "
        "Ответь клиенту."
    )

    response = await acompletion(
        # Используем мощную модель для генерации (например, gpt-4o или gpt-4o-mini)
        model="openai/gpt-4o-mini",
        api_key=settings.PROXY_API_KEY,
        api_base=settings.PROXY_API_BASE,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.7
    )
    
    return response.choices[0].message.content
