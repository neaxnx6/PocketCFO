import json
from pydantic import BaseModel, Field
from litellm import acompletion
from app.config import settings

class TransactionParsed(BaseModel):
    action: str = Field(description="'expense' or 'income'")
    amount: float = Field(description="The numeric amount of money")
    category: str = Field(description="Short category name, e.g. 'Еда', 'Такси', 'Зарплата'")
    description: str = Field(description="The user's original phrasing or intent, e.g. 'Купил хлеб и молоко'")

async def parse_transaction(user_text: str) -> TransactionParsed:
    system_prompt = (
        "Ты — финансовый распознаватель. Пользователь пришлет тебе строку текста "
        "(возможно с опечатками). Извлеки из нее информацию о транзакции и верни СТРОГО валидный JSON объект."
        "Пойми, потратил человек деньги или получил ('expense' или 'income').\n"
        'Пример правильного ответа: {"action": "expense", "amount": 350.0, "category": "Еда", "description": "Купил хлеб"}\n'
        "В твоем ответе не должно быть никакого текста вне фигурных скобок JSON!"
    )
    
    response = await acompletion(
        # Используем gpt-4o-mini через прокси, так как это дешево и быстро для парсинга
        model="openai/gpt-4o-mini",
        api_key=settings.PROXY_API_KEY,
        api_base=settings.PROXY_API_BASE,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        response_format={ "type": "json_object" },
        temperature=0.0
    )
    
    content = response.choices[0].message.content
    try:
        data = json.loads(content)
        return TransactionParsed(**data)
    except Exception as e:
        # Fallback or simple extraction logic could be added here
        raise ValueError(f"Failed to parse LLM response: {content}") from e
