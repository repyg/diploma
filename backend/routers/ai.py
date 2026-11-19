"""Dobro.ru event passport generation and runtime AI API key management."""

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException

from ..schemas import AISettingsRequest, AISettingsResponse, DobroRequest, DobroResponse
from ..services.ai_ocr import (
    call_openai_compatible_llm,
    clear_ai_config,
    get_ai_config_status,
    set_ai_config,
)
from .auth import get_current_user

router = APIRouter(prefix="/ai", tags=["ai"])


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _build_local_description(event_name: str, roles: list[str]) -> str:
    roles_block = (
        f" Задействованы волонтерские роли: {', '.join(roles)}."
        if roles
        else ""
    )
    return (
        f"Мероприятие «{event_name}» направлено на развитие добровольного донорства крови и компонентов среди студентов РХТУ.{roles_block} "
        "Участники помогают организовать информирование студентов, регистрацию добровольцев и сопровождение группы до медицинского учреждения. "
        "Перед выездом координаторы напоминают участникам о необходимости соблюдать рекомендации медицинского центра и подготовить документы. "
        "Волонтеры встречают участников, помогают с навигацией, поддерживают порядок и передают организационные вопросы координатору. "
        "Мероприятие способствует популяризации регулярного донорства, взаимопомощи и ответственного отношения к здоровью. "
        "По итогам акции координаторы фиксируют количество участников и собирают обратную связь для улучшения будущих мероприятий."
    )


@router.get("/settings", response_model=AISettingsResponse)
def read_ai_settings(_: str = Depends(get_current_user)) -> AISettingsResponse:
    """Проверить статус подключенного пользовательского API-ключа."""
    return AISettingsResponse(**get_ai_config_status())


@router.put("/settings", response_model=AISettingsResponse)
def update_ai_settings(
    payload: AISettingsRequest,
    _: str = Depends(get_current_user),
) -> AISettingsResponse:
    """Установить пользовательский API-ключ и модель для OCR и генерации текстов."""
    status = set_ai_config(
        api_key=payload.api_key,
        base_url=payload.base_url,
        model=payload.model,
    )
    return AISettingsResponse(**status)


@router.delete("/settings", response_model=AISettingsResponse)
def delete_ai_settings(_: str = Depends(get_current_user)) -> AISettingsResponse:
    """Очистить пользовательский API-ключ и переключиться в локальный режим."""
    return AISettingsResponse(**clear_ai_config())


@router.post("/dobro-rf/generate", response_model=DobroResponse)
def generate_dobro_passport(
    payload: DobroRequest,
    x_llm_api_key: str | None = Header(default=None),
    _: str = Depends(get_current_user),
) -> DobroResponse:
    start, end = _minutes(payload.start_time), _minutes(payload.end_time)
    duration_minutes = end - start
    if duration_minutes <= 0 or duration_minutes > 8 * 60:
        raise HTTPException(status_code=422, detail="Shift must be positive and no longer than 8 hours")

    description = payload.description
    generated_locally = True

    if description is None:
        effective_key = payload.api_key or x_llm_api_key
        roles_str = ", ".join(payload.volunteer_roles) if payload.volunteer_roles else "координатор, волонтер-информатор, донор"
        try:
            llm_text = call_openai_compatible_llm(
                system_prompt=(
                    "Ты — помощник координатора донорского движения РХТУ им. Д.И. Менделеева. "
                    "Сгенерируй официальное описание волонтерского мероприятия для платформы Добро.рф "
                    "длиной СТРОГО от 450 до 1500 символов (без разметки Markdown). "
                    "Опиши цели акции, задачи волонтеров и социальную значимость."
                ),
                user_prompt=(
                    f"Название акции: {payload.event_name}\n"
                    f"Время смены: {payload.start_time} – {payload.end_time}\n"
                    f"Роли волонтеров: {roles_str}"
                ),
                api_key=effective_key,
            ).strip()
            if 300 <= len(llm_text) <= 3000:
                description = llm_text
                generated_locally = False
        except RuntimeError:
            pass

    if description is None:
        description = _build_local_description(payload.event_name, payload.volunteer_roles)
        generated_locally = True

    if not 300 <= len(description) <= 3000:
        raise HTTPException(status_code=422, detail="Description must contain from 300 to 3000 characters")

    return DobroResponse(
        event_name=payload.event_name,
        description=description,
        start_time=payload.start_time,
        end_time=payload.end_time,
        duration_hours=duration_minutes / 60,
        generated_locally=generated_locally,
    )