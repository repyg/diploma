"""Dobro.ru event passport draft generation."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from ..schemas import DobroRequest, DobroResponse
from .auth import get_current_user

router = APIRouter(prefix="/ai", tags=["ai"])


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


@router.post("/dobro-rf/generate", response_model=DobroResponse)
def generate_dobro_passport(payload: DobroRequest, _: str = Depends(get_current_user)) -> DobroResponse:
    start, end = _minutes(payload.start_time), _minutes(payload.end_time)
    duration_minutes = end - start
    if duration_minutes <= 0 or duration_minutes > 8 * 60:
        raise HTTPException(status_code=422, detail="Shift must be positive and no longer than 8 hours")
    description = payload.description or (
        f"Мероприятие «{payload.event_name}» направлено на развитие добровольного донорства крови и компонентов среди студентов РХТУ. "
        "Участники помогают организовать информирование студентов, регистрацию добровольцев и сопровождение группы до медицинского учреждения. "
        "Перед выездом координаторы напоминают участникам о необходимости соблюдать рекомендации медицинского центра и подготовить документы. "
        "Волонтеры встречают участников, помогают с навигацией, поддерживают порядок и передают организационные вопросы координатору. "
        "Мероприятие способствует популяризации регулярного донорства, взаимопомощи и ответственного отношения к здоровью. "
        "По итогам акции координаторы фиксируют количество участников и собирают обратную связь для улучшения будущих мероприятий."
    )
    if not 300 <= len(description) <= 3000:
        raise HTTPException(status_code=422, detail="Description must contain from 300 to 3000 characters")
    return DobroResponse(event_name=payload.event_name, description=description, start_time=payload.start_time, end_time=payload.end_time, duration_hours=duration_minutes / 60)
