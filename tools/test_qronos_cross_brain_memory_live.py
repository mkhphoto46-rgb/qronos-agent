from __future__ import annotations

import sys

from core.brain_runtime import (
    BrainMessage,
    BrainMessageRole,
)
from core.orchestrator import Orchestrator
from core.task_plan import TaskPlan
from core.task_router import TaskType


SECRET_PHRASE = "Silver Harbor"


def run_turn(
    orchestrator: Orchestrator,
    task_type: TaskType,
    text: str,
    history: list[BrainMessage],
) -> str:
    plan = TaskPlan(
        goal=text
    )

    plan.add_step(
        task_type=task_type,
        description=text,
    )

    results = orchestrator.execute_plan(
        plan,
        conversation_messages=history,
    )

    if not results:
        raise RuntimeError(
            "Orchestrator returned no results."
        )

    result = results[-1]

    if not result.success:
        raise RuntimeError(
            result.error
            or "Qronos task failed."
        )

    return result.output


def add_turn_to_history(
    history: list[BrainMessage],
    user_text: str,
    assistant_text: str,
) -> None:
    history.append(
        BrainMessage(
            role=BrainMessageRole.USER,
            content=user_text,
        )
    )

    history.append(
        BrainMessage(
            role=BrainMessageRole.ASSISTANT,
            content=assistant_text,
        )
    )


def main() -> int:
    print()
    print("=" * 60)
    print("QRONOS FAST -> HEAVY SHARED MEMORY TEST")
    print("=" * 60)
    print()

    orchestrator = Orchestrator()

    history: list[BrainMessage] = []

    turn_1 = (
        "For this conversation, remember the exact phrase "
        f"'{SECRET_PHRASE}'. "
        "Reply briefly that you understood."
    )

    print("TURN 1")
    print("Brain: FAST")
    print()
    print("USER:")
    print(turn_1)
    print()

    try:
        response_1 = run_turn(
            orchestrator=orchestrator,
            task_type=TaskType.FAST,
            text=turn_1,
            history=history,
        )

    except Exception as exc:
        print("FAST TURN FAILED:")
        print(exc)
        return 1

    print("QRONOS:")
    print(response_1)
    print()

    add_turn_to_history(
        history=history,
        user_text=turn_1,
        assistant_text=response_1,
    )

    print("-" * 60)
    print()

    turn_2 = (
        "Without asking me for the phrase again, tell me "
        "the exact phrase I asked you to remember earlier "
        "in this conversation. Then briefly explain why "
        "you were able to answer from conversation context."
    )

    print("TURN 2")
    print("Brain: HEAVY")
    print()
    print("USER:")
    print(turn_2)
    print()

    try:
        response_2 = run_turn(
            orchestrator=orchestrator,
            task_type=TaskType.HEAVY,
            text=turn_2,
            history=history,
        )

    except Exception as exc:
        print("HEAVY TURN FAILED:")
        print(exc)
        return 2

    print("QRONOS:")
    print(response_2)
    print()

    memory_passed = (
        SECRET_PHRASE.lower()
        in response_2.lower()
    )

    print("-" * 60)
    print()

    if memory_passed:
        print(
            "FAST -> HEAVY MEMORY CHECK: PASS"
        )
    else:
        print(
            "FAST -> HEAVY MEMORY CHECK: FAIL"
        )

    add_turn_to_history(
        history=history,
        user_text=turn_2,
        assistant_text=response_2,
    )

    print()
    print("-" * 60)
    print()

    turn_3 = (
        "What AI model are you?"
    )

    print("TURN 3")
    print("Brain: HEAVY")
    print()
    print("USER:")
    print(turn_3)
    print()

    try:
        response_3 = run_turn(
            orchestrator=orchestrator,
            task_type=TaskType.HEAVY,
            text=turn_3,
            history=history,
        )

    except Exception as exc:
        print("HEAVY IDENTITY TURN FAILED:")
        print(exc)
        return 3

    print("QRONOS:")
    print(response_3)
    print()

    identity_text = (
        response_3.lower()
    )

    qronos_present = (
        "qronos"
        in identity_text
    )

    identity_leak = any(
        word in identity_text
        for word in (
            "qwen",
            "alibaba",
            "ollama",
            "qwen3",
        )
    )

    if (
        qronos_present
        and not identity_leak
    ):
        print(
            "HEAVY BRAIN IDENTITY CHECK: PASS"
        )
    else:
        print(
            "HEAVY BRAIN IDENTITY CHECK: FAIL"
        )

    print()
    print("=" * 60)

    if (
        memory_passed
        and qronos_present
        and not identity_leak
    ):
        print(
            "CROSS-BRAIN TEST: SUCCESS"
        )
        print("=" * 60)
        return 0

    print(
        "CROSS-BRAIN TEST: FAILED"
    )
    print("=" * 60)

    return 4


if __name__ == "__main__":
    sys.exit(
        main()
    )