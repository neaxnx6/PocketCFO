import json
from pydantic import BaseModel, Field
from typing import Optional, List
from litellm import acompletion
from app.config import settings

class TransactionParsed(BaseModel):
    action: str = Field(description="'expense' or 'income'")
    amount: float = Field(description="The numeric amount of money")
    category: str = Field(description="Short category name, e.g. 'Еда', 'Такси', 'Зарплата'")
    description: str = Field(description="The user's original phrasing or intent, e.g. 'Купил хлеб и молоко'")

class ParsedDebt(BaseModel):
    name: str = Field(description="Название кредита, банка или человека, кому должен (например: 'Сбербанк', 'Халва', 'Долг другу')")
    amount: float = Field(description="Сумма оставшегося долга")
    min_payment: Optional[float] = Field(default=None, description="Минимальный обязательный ежемесячный платеж. Если нет, то 0.0 или null")

class ParsedDebtsList(BaseModel):
    debts: List[ParsedDebt] = Field(default_factory=list)

class ParsedExpense(BaseModel):
    name: str = Field(description="Название расхода (например: 'Аренда', 'Продукты', 'Машина', 'Интернет')")
    amount: float = Field(description="Месячный лимит или планируемая сумма расхода")

class ParsedExpensesList(BaseModel):
    expenses: List[ParsedExpense] = Field(default_factory=list)


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


async def parse_onboarding_debts(user_text: str) -> ParsedDebtsList:
    system_prompt = (
        "Ты — финансовый помощник. Извлеки список долгов и кредитов из сообщения пользователя.\n"
        "Для каждого долга найди:\n"
        "- Название кредита/долга (например: 'Сбербанк', 'Халва', 'Долг другу').\n"
        "- Общую сумму долга (amount).\n"
        "- Минимальный обязательный ежемесячный платеж (min_payment) — если он упомянут, иначе поставь null.\n\n"
        "Ответ верни строго в виде JSON-объекта со списком 'debts'.\n"
        "Пример формата:\n"
        '{"debts": [{"name": "Кредитка Сбербанк", "amount": 80000.0, "min_payment": 4000.0}, {"name": "Долг другу", "amount": 15000.0, "min_payment": null}]}\n'
        "Если долгов нет или в тексте написан 0 или слово 'нет', верни пустой список:\n"
        '{"debts": []}\n'
        "Ответ должен содержать ТОЛЬКО валидный JSON."
    )
    
    response = await acompletion(
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
        return ParsedDebtsList(**data)
    except Exception as e:
        raise ValueError(f"Failed to parse debts LLM response: {content}") from e


async def parse_onboarding_expenses(user_text: str) -> ParsedExpensesList:
    system_prompt = (
        "Ты — финансовый помощник. Извлеки список регулярных месячных трат из сообщения пользователя.\n"
        "Для каждого расхода найди:\n"
        "- Название расхода (например: 'Аренда', 'Продукты', 'Машина', 'Интернет').\n"
        "- Месячный лимит или планируемую сумму (amount).\n\n"
        "Ответ верни строго в виде JSON-объекта со списком 'expenses'.\n"
        "Пример формата:\n"
        '{"expenses": [{"name": "Аренда", "amount": 35000.0}, {"name": "Продукты", "amount": 25000.0}, {"name": "Интернет", "amount": 500.0}]}\n'
        "Если расходов нет, верни пустой список:\n"
        '{"expenses": []}\n'
        "Ответ должен содержать ТОЛЬКО валидный JSON."
    )
    
    response = await acompletion(
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
        return ParsedExpensesList(**data)
    except Exception as e:
        raise ValueError(f"Failed to parse expenses LLM response: {content}") from e

