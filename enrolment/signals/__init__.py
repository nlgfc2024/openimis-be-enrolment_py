from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal

from enrolment.confirmation import (
    on_confirm_enrolment_of_group,
    on_confirm_enrolment_of_individual,
    on_task_complete_enrolment,
)


def bind_service_signals():
    bind_service_signal(
        "enrolment_service.select_individuals_to_benefit_plan",
        on_confirm_enrolment_of_individual,
        bind_type=ServiceSignalBindType.AFTER,
    )
    bind_service_signal(
        "enrolment_service.select_groups_to_benefit_plan",
        on_confirm_enrolment_of_group,
        bind_type=ServiceSignalBindType.AFTER,
    )
    bind_service_signal(
        "task_service.complete_task",
        on_task_complete_enrolment,
        bind_type=ServiceSignalBindType.AFTER,
    )
