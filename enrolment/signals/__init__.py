from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal

from enrolment.confirmation import (
    on_task_complete_enrolment,
)


def bind_service_signals():
    bind_service_signal(
        "task_service.complete_task",
        on_task_complete_enrolment,
        bind_type=ServiceSignalBindType.AFTER,
    )
