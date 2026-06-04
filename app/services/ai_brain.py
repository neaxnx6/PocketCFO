import json
from pydantic import BaseModel, Field
from typing import Optional, List
from litellm import acompletion
from app.config import settings


class PydanticEnvelope(BaseModel):
    name: str
    target_amount: Optional[float] = None
    current_amount: float = 0.0
    is_debt: bool = False
    is_goal: bool = False
    min_payment: Optional[float] = None


class PlanItem(BaseModel):
    name: str
    amount: float


class IncomeAllocation(BaseModel):
    envelope_name: str
    amount: float


class TransactionItem(BaseModel):
    action: str = Field(description="'expense'|'income'")
    amount: float
    target_envelope_name: Optional[str] = None
    category: Optional[str] = None


class BrainResponse(BaseModel):
    thoughts: str = Field(default="", description="Черновик. Считай пошагово. Проверяй: Сумма плана <= free_cash?")
    intent: str = Field(description="'transaction'|'profile_update'|'chat'")
    transactions: Optional[List[TransactionItem]] = Field(default=None, description="Массив транзакций (может быть несколько трат или доходов одновременно)")
    envelopes_to_create: Optional[List[PydanticEnvelope]] = Field(default=None)
    monthly_income: Optional[float] = Field(default=None)
    free_cash: Optional[float] = Field(default=None)
    plan_items: Optional[List[PlanItem]] = Field(default=None)
    income_allocations: Optional[List[IncomeAllocation]] = Field(default=None)
    show_dashboard: bool = Field(default=False)
    coach_reply: str = Field(description="Ответ. Теги <b>,<i>. Переносы \\n")


SYSTEM_PROMPT_BODY = """\
ЗАКОНЫ:
1. ДИАГНОЗ: По ФИН. ЗДОРОВЬЮ. Долги >6 мес -> 🚨 Критично. 3-6 мес -> ⚠️ Напряжённо. <3 мес -> ✅ Управляемо. 0 -> 🟢 Рост.
2. МАТЕМАТИКА: thoughts: считай пошагово. Округляй предлагаемые суммы до ТЫСЯЧ (например 11к, а не 11.4к и не 2.1к). free_cash = доходы-расходы. ВАЖНО: plan_items — это распределение ТОЛЬКО free_cash! Сумма plan_items ДОЛЖНА СТРОГО РАВНЯТЬСЯ free_cash. Распределяй кэш ПОД НОЛЬ. В тексте ответа НЕ пытайся считать финальные остатки.
3. КОНВЕРТЫ (profile_update):
 ВАЖНО: Режим profile_update используй ТОЛЬКО ОДИН РАЗ для настройки бюджета! Если юзер оплатил счета или получил премию — это СТРОГО intent: transaction!
 Ты ОБЯЗАТЕЛЬНО должен передать ВСЕ фонды в массиве envelopes_to_create!
 а) Расходы: is_debt=F, is_goal=F. ВАЖНО: current_amount — это фактические ДЕНЬГИ В КОНВЕРТЕ ПРЯМО СЕЙЧАС (а не потраченные). Если конверт пуст, current_amount = 0.0.
 б) Цели: is_debt=F, is_goal=T, current_amount=0.
 в) Долги: is_debt=T, is_goal=F, target_amount = ВЕСЬ ДОЛГ, current_amount = 0 (погашено). ОБЯЗАТЕЛЬНО: если пользователь упомянул минимальный обязательный ежемесячный платеж по долгу (например, 'минималка 10к'), запиши это число в min_payment.
 г) Буфер: Назови строго "Буфер". is_debt=F, is_goal=T, current_amount=0, target_amount=0.
 🛑 КРИТИЧЕСКОЕ ПРАВИЛО БАЛАНСА: Сумма всех current_amount созданных конвертов + free_cash должна СТРОГО равняться сумме денег, которая СЕЙЧАС есть у пользователя на руках.
 Имена фондов в plan_items должны ТОЧНО СОВПАДАТЬ с envelopes_to_create!
4. СТРАТЕГИЯ АВТОПИЛОТА (РАСПРЕДЕЛЕНИЕ СВОБОДНЫХ ДЕНЕГ ИЛИ ПРИХОДОВ):
  - 🛑 БАЗОВЫЕ ПОТРЕБНОСТИ И ЕДА (ПРИОРИТЕТ 0 - САМЫЙ ВЫСОКИЙ): Если в бюджете есть РАСХОДЫ (например, Продукты, Аренда), где current_amount < target_amount (конверт пуст или недофинансирован), ты ОБЯЗАН в ПЕРВУЮ ОЧЕРЕДЬ направить деньги ТУДА! Без еды и жилья человек не выживет. НИКОГДА не предлагай отдавать деньги на долги (даже на минимальные платежи по кредиткам!), если у пользователя 0 рублей отложено на еду (Продукты) и Аренду! Сначала базовое выживание, потом кредиты.
  - МИНИМАЛЬНЫЕ ПЛАТЕЖИ (ПРИОРИТЕТ 1): После того как закрыта база (еда/жилье), направь деньги на минимальные обязательные платежи по долгам.
  - БУФЕР: ВСЕГДА ~10% от свободных денег.
  - ДОЛГИ (ДОСРОЧНОЕ ГАШЕНИЕ - ПРИОРИТЕТ 3): Только после базы, минималок и буфера — направляй остаток на досрочное гашение долгов.
    🛑 КРИТИЧЕСКОЕ ПРАВИЛО: НИКОГДА не предлагай перевести на долг сумму, превышающую его остаток!
  - ЦЕЛИ (НАКОПЛЕНИЯ): Если есть долги, выделяй на цели копейки.
  - 🛑 ОБЯЗАТЕЛЬНОЕ ПРАВИЛО ДЛЯ ПРИХОДОВ: При любой транзакции на приход (action='income') ты ОБЯЗАН заполнить income_allocations для распределения всей суммы прихода.
  - 🛑 ВОЗВРАТ ДОЛГА ПОЛЬЗОВАТЕЛЮ: Если пользователю вернули долг - это СТРОГО ДОХОД (action='income').
5. РЕЖИМ profile_update: Будь КРАТОК. Скажи: "Бюджет сформирован. Можешь проверять Дашборд!"
6. ДАШБОРД: НИКОГДА не печатай списки фондов вручную в тексте! Просто ставь show_dashboard=true.
7. КРАТКОСТЬ: Сокращай рассуждения, пиши емко и только по делу.
8. ФОРМАТ: Строго <b>, <i>, \\n, •.
"""

SYSTEM_PROMPT_EXAMPLES = (
    '\nПРИМЕРЫ (сжато):\n'
    'profile_update: {"thoughts":"Посчитал...","intent":"profile_update","monthly_income":240000,"free_cash":64000,"plan_items":[{"name":"Отпуск","amount":50000}],"envelopes_to_create":[{"name":"Аренда","target_amount":35000,"current_amount":35000,"is_debt":false,"is_goal":false}],"show_dashboard":true,"coach_reply":"🚨 <b>Сводка</b>\\nСвободно: 64к\\n\\nПредлагаю план:\\n• Отпуск: +50к\\n• Долг Сбер: +14к"}\n'
    'expense: {"intent":"transaction","transactions":[{"action":"expense","amount":5000,"target_envelope_name":"Машина"}, {"action":"expense","amount":3000,"target_envelope_name":"Продукты"}],"show_dashboard":true,"coach_reply":"💸 Учтено:\\n<b>−5000</b> → Машина\\n<b>−3000</b> → Продукты"}\n'
    'income: {"intent":"transaction","transactions":[{"action":"income","amount":15000}],"income_allocations":[{"envelope_name":"Долг Сбер","amount":10000}],"show_dashboard":true,"coach_reply":"💰 <b>+15к</b>\\nПредлагаю: 10к → Долг Сбер\\n<i>Подтверди или предложи своё.</i>"}\n'
    'chat: {"intent":"chat","show_dashboard":true,"coach_reply":"Смотри ниже 👇"}\n'
    '━━━━━━━━━━━━━━━━━━━━━━━━━\n'
    'Ответ СТРОГО в формате JSON. Меняй формулировки.'
)


async def process_user_message(
    user_text: str,
    user_vibe: str,
    envelopes_context: str,
    financial_health: str,
    chat_history: List[dict]
) -> BrainResponse:
    system_prompt = (
        f"Ты — ИИ-помощник «На Балансе», твой стиль: {user_vibe}.\n"
        "Ты честный, прямой и конкретный. Не льстишь и не преуменьшаешь проблемы.\n\n"
        f"ТВОИ КОНВЕРТЫ:\n{envelopes_context}\n\n"
        f"ФИН. ЗДОРОВЬЕ:\n{financial_health}\n\n"
        + SYSTEM_PROMPT_BODY
        + SYSTEM_PROMPT_EXAMPLES
    )

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_history)
    messages.append({"role": "user", "content": user_text})

    response = await acompletion(
        model=settings.LLM_MODEL_NAME,
        api_key=settings.LLM_API_KEY,
        api_base=settings.LLM_API_BASE,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.3
    )

    content = response.choices[0].message.content
    try:
        data = json.loads(content)
        # Fallback: ensure required fields exist
        if "thoughts" not in data:
            data["thoughts"] = ""
        if "intent" not in data:
            data["intent"] = "chat"
        if "coach_reply" not in data:
            data["coach_reply"] = "Извини, не смог сформировать ответ. Попробуй ещё раз."
        return BrainResponse(**data)
    except Exception as e:
        raise ValueError(f"Failed to parse LLM json: {content}") from e
