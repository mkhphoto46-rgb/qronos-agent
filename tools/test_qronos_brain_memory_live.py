from __future__ import annotations

import sys

from core.brain_runtime import (
    BrainMessage,
    BrainMessageRole,
)
from core.orchestrator import Orchestrator
from core.task_plan import TaskPlan
from core.task_router import TaskType


CODE_WORD = "Blue Falcon"


def run_turn(
    orchestrator: Orchestrator,
    text: str,
    history: list[BrainMessage],
) -> str:
    plan = TaskPlan(
        goal=text
    )

    plan.add_step(
        task_type=TaskType.FAST,
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


def main() -> int:
    print()
    print("=" * 60)
    print("QRONOS DIRECT BRAIN MEMORY TEST")
    print("=" * 60)
    print()

    orchestrator = Orchestrator()

    history: list[BrainMessage] = []

    turn_1 = (
        "For this conversation, remember the code word "
        f"'{CODE_WORD}'. Reply briefly that you understood."
    )

    print("TURN 1 USER:")
    print(turn_1)
    print()

    try:
        response_1 = run_turn(
            orchestrator=orchestrator,
            text=turn_1,
            history=history,
        )

    except Exception as exc:
        print("TURN 1 FAILED:")
        print(exc)
        return 1

    print("TURN 1 QRONOS:")
    print(response_1)
    print()

    history.append(
        BrainMessage(
            role=BrainMessageRole.USER,
            content=turn_1,
        )
    )

    history.append(
        BrainMessage(
            role=BrainMessageRole.ASSISTANT,
            content=response_1,
        )
    )

    turn_2 = (
        "What code word did I ask you to remember "
        "earlier in this conversation?"
    )

    print("-" * 60)
    print()
    print("TURN 2 USER:")
    print(turn_2)
    print()

    try:
        response_2 = run_turn(
            orchestrator=orchestrator,
            text=turn_2,
            history=history,
        )

    except Exception as exc:
        print("TURN 2 FAILED:")
        print(exc)
        return 2

    print("TURN 2 QRONOS:")
    print(response_2)
    print()

    memory_passed = (
        CODE_WORD.lower()
        in response_2.lower()
    )

    print("-" * 60)
    print()

    if memory_passed:
        print("MEMORY CHECK: PASS")
    else:
        print("MEMORY CHECK: FAIL")

    print()

    history.append(
        BrainMessage(
            role=BrainMessageRole.USER,
            content=turn_2,
        )
    )

    history.append(
        BrainMessage(
            role=BrainMessageRole.ASSISTANT,
            content=response_2,
        )
    )

    turn_3 = "What AI model are you?"

    print("TURN 3 USER:")
    print(turn_3)
    print()

    try:
        response_3 = run_turn(
            orchestrator=orchestrator,
            text=turn_3,
            history=history,
        )

    except Exception as exc:
        print("TURN 3 FAILED:")
        print(exc)
        return 3

    print("TURN 3 QRONOS:")
    print(response_3)
    print()

    lower_identity = (
        response_3.lower()
    )

    qronos_present = (
        "qronos"
        in lower_identity
    )

    identity_leak = any(
        word in lower_identity
        for word in (
            "qwen",
            "alibaba",
            "ollama",
            "qwen3",
        )
    )

    print("-" * 60)
    print()

    if (
        qronos_present
        and not identity_leak
    ):
        print("IDENTITY CHECK: PASS")
    else:
        print("IDENTITY CHECK: FAIL")

    print()

    print("=" * 60)

    if (
        memory_passed
        and qronos_present
        and not identity_leak
    ):
        print("DIRECT BRAIN TEST: SUCCESS")
        print("=" * 60)
        return 0

    print("DIRECT BRAIN TEST: FAILED")
    print("=" * 60)

    return 4


if __name__ == "__main__":
    sys.exit(
        main()
    )