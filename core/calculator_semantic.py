from __future__ import annotations

from dataclasses import dataclass
import re
import time

from core.accessibility_action_executor import (
    AccessibilityActionExecutor,
)
from core.accessibility_targeting import (
    AccessibilityTargetLocator,
)
from core.action_cancellation import (
    ActionCancelled,
    CancellationToken,
    checkpoint,
)
from core.actions import (
    ActionOutcome,
    ActionRequest,
    ActionResult,
)
from core.computer_executor import (
    ComputerAction,
    ComputerExecutor,
    ComputerOperation,
)
from core.windows_accessibility import (
    actionable_element_by_runtime_id,
    actionable_elements_in_window,
)
from security.gate import (
    evaluate,
    issue_grant,
)
from security.permissions import (
    ActionCategory,
    PermissionDecision,
)


class CalculatorSemanticParseError(ValueError):
    pass


class CalculatorSemanticPermissionRequired(PermissionError):
    pass


class CalculatorSemanticExecutionError(RuntimeError):
    pass


class CalculatorSemanticCancelled(CalculatorSemanticExecutionError):
    """Stop/Pause/shutdown ended the sequence; ``steps_invoked`` were applied."""

    def __init__(self, reason: str, *, steps_invoked: int, steps_total: int) -> None:
        self.reason = reason
        self.steps_invoked = int(steps_invoked)
        self.steps_total = int(steps_total)
        super().__init__(
            f"Calculator sequence cancelled ({reason}) after "
            f"{self.steps_invoked} of {self.steps_total} step(s)."
        )


@dataclass(frozen=True)
class CalculatorSemanticPlan:
    left_digits: str
    operator: str
    right_digits: str
    automation_ids: tuple[str, ...]
    expected_display: str
    result_text: str
    expression: str


@dataclass(frozen=True)
class CalculatorSemanticRun:
    expression: str
    result_text: str
    display_name: str
    invoked_runtime_ids: tuple[str, ...]
    hwnd: int
    pid: int


_DIGIT_TRANSLATION = str.maketrans(
    "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"
    "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669",
    "01234567890123456789",
)


_NUMBER_WORDS = {
    "\u0635\u0641\u0631": "0",
    "\u06cc\u06a9": "1",
    "\u06cc\u0647": "1",
    "\u062f\u0648": "2",
    "\u0633\u0647": "3",
    "\u0686\u0647\u0627\u0631": "4",
    "\u067e\u0646\u062c": "5",
    "\u0634\u0634": "6",
    "\u0647\u0641\u062a": "7",
    "\u0647\u0634\u062a": "8",
    "\u0646\u0647": "9",
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}


_CALCULATOR_MARKERS = (
    "\u0645\u0627\u0634\u06cc\u0646 \u062d\u0633\u0627\u0628",
    "\u0645\u0627\u0634\u06cc\u0646\u200c\u062d\u0633\u0627\u0628",
    "calculator",
    "calc",
)


_OPERATOR_ALIASES = {
    "multiply": (
        "\u0636\u0631\u0628\u062f\u0631",
        "\u0636\u0631\u0628",
        "\u00d7",
        "times",
    ),
    "plus": (
        "\u0628\u0647 \u0639\u0644\u0627\u0648\u0647",
        "\u0628\u0639\u0644\u0627\u0648\u0647",
        "\u062c\u0645\u0639",
        "plus",
    ),
    "minus": (
        "\u0645\u0646\u0647\u0627\u06cc",
        "\u0645\u0646\u0647\u0627",
        "\u062a\u0641\u0631\u06cc\u0642",
        "minus",
    ),
    "divide": (
        "\u062a\u0642\u0633\u06cc\u0645 \u0628\u0631",
        "\u062a\u0642\u0633\u06cc\u0645",
        "divide by",
        "divide",
    ),
}


_OPERATOR_BUTTONS = {
    "multiply": "multiplyButton",
    "plus": "plusButton",
    "minus": "minusButton",
    "divide": "divideButton",
}


def _normalise(text: str) -> str:
    value = str(
        text
        or ""
    ).translate(
        _DIGIT_TRANSLATION
    )

    value = (
        value
        .replace("\u200c", " ")
        .replace("\u064a", "\u06cc")
        .replace("\u0643", "\u06a9")
        .lower()
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def _contains_calculator(
    text: str,
) -> bool:
    return any(
        marker in text
        for marker in _CALCULATOR_MARKERS
    )


def _number_candidates(
    text: str,
) -> list[str]:
    value = _normalise(
        text
    )

    found: list[
        tuple[int, str]
    ] = []

    for match in re.finditer(
        r"(?<!\w)\d{1,6}(?!\w)",
        value,
    ):
        found.append(
            (
                match.start(),
                match.group(0),
            )
        )

    for word, digit in _NUMBER_WORDS.items():
        offset = 0

        while True:
            index = value.find(
                word,
                offset,
            )

            if index < 0:
                break

            end = (
                index
                + len(word)
            )

            before_ok = (
                index == 0
                or not value[
                    index - 1
                ].isalnum()
            )

            after_ok = (
                end == len(value)
                or not value[
                    end
                ].isalnum()
            )

            if before_ok and after_ok:
                found.append(
                    (
                        index,
                        digit,
                    )
                )

            offset = (
                index
                + len(word)
            )

    found.sort(
        key=lambda item:
            item[0]
    )

    return [
        digit
        for _, digit
        in found
    ]


def _expected_result(
    left: int,
    operator: str,
    right: int,
) -> int:
    if operator == "multiply":
        return left * right

    if operator == "plus":
        return left + right

    if operator == "minus":
        result = left - right

        if result < 0:
            raise CalculatorSemanticParseError(
                "Negative Calculator results are not enabled."
            )

        return result

    if operator == "divide":
        if right == 0:
            raise CalculatorSemanticParseError(
                "Division by zero is not allowed."
            )

        if left % right:
            raise CalculatorSemanticParseError(
                "Non-integer division is not enabled."
            )

        return left // right

    raise CalculatorSemanticParseError(
        "Unsupported Calculator operator."
    )


def parse_calculator_semantic_command(
    text: str,
) -> CalculatorSemanticPlan:
    cleaned = _normalise(
        text
    )

    if not cleaned:
        raise CalculatorSemanticParseError(
            "Calculator command is empty."
        )

    if not _contains_calculator(
        cleaned
    ):
        raise CalculatorSemanticParseError(
            "The command does not explicitly target Calculator."
        )

    matches: list[
        tuple[int, int, str]
    ] = []

    for operator, aliases in _OPERATOR_ALIASES.items():
        for alias in aliases:
            index = cleaned.find(
                alias
            )

            if index >= 0:
                matches.append(
                    (
                        index,
                        index + len(
                            alias
                        ),
                        operator,
                    )
                )

    if not matches:
        raise CalculatorSemanticParseError(
            "No supported Calculator operator was found."
        )

    start, end, operator = min(
        matches,
        key=lambda item:
            item[0],
    )

    left = _number_candidates(
        cleaned[:start]
    )

    right = _number_candidates(
        cleaned[end:]
    )

    if not left:
        raise CalculatorSemanticParseError(
            "Calculator command is missing the left number."
        )

    if not right:
        raise CalculatorSemanticParseError(
            "Calculator command is missing the right number."
        )

    left_digits = left[-1]
    right_digits = right[0]

    result = _expected_result(
        int(
            left_digits
        ),
        operator,
        int(
            right_digits
        ),
    )

    ids = [
        "clearButton",
    ]

    ids.extend(
        f"num{digit}Button"
        for digit in left_digits
    )

    ids.append(
        _OPERATOR_BUTTONS[
            operator
        ]
    )

    ids.extend(
        f"num{digit}Button"
        for digit in right_digits
    )

    ids.append(
        "equalButton"
    )

    symbol = {
        "multiply": "\u00d7",
        "plus": "+",
        "minus": "-",
        "divide": "\u00f7",
    }[
        operator
    ]

    return CalculatorSemanticPlan(
        left_digits=left_digits,
        operator=operator,
        right_digits=right_digits,
        automation_ids=tuple(
            ids
        ),
        expected_display=(
            f"Display is {result}"
        ),
        result_text=str(
            result
        ),
        expression=(
            f"{left_digits} "
            f"{symbol} "
            f"{right_digits}"
        ),
    )

class CalculatorSemanticExecutor:
    def __init__(
        self,
        computer_executor: ComputerExecutor,
    ) -> None:
        self.computer_executor = (
            computer_executor
        )

    @staticmethod
    def _boundary(
        cancel_token: CancellationToken | None,
        total_steps: int,
    ) -> None:
        """A checkpoint before any Calculator step has been invoked."""
        try:
            checkpoint(cancel_token)
        except ActionCancelled as exc:
            raise CalculatorSemanticCancelled(
                exc.reason,
                steps_invoked=0,
                steps_total=total_steps,
            ) from exc

    def _approval(
        self,
        verdict,
        *,
        voice_confirmed: bool,
    ):
        if not verdict.needs_approval:
            return None

        if (
            voice_confirmed
            and verdict.decision
            is PermissionDecision.REQUIRE_VOICE_CONFIRMATION
        ):
            return verdict.decision

        raise CalculatorSemanticPermissionRequired(
            f"Action requires {verdict.decision.value}."
        )

    def execute(
        self,
        plan: CalculatorSemanticPlan,
        *,
        voice_confirmed: bool,
        cancel_token: CancellationToken | None = None,
    ) -> CalculatorSemanticRun:
        """
        Stop is honoured before launch, after launch, after UIA discovery,
        before the grant and before every Invoke. One Invoke is a single
        native call: it completes or fails as a unit, and Stop takes effect
        at the next boundary. A stopped sequence sends no recovery input.
        """
        total_steps = len(plan.automation_ids)
        self._boundary(cancel_token, total_steps)

        # ----------------------------------------------------
        # 1. Open/activate Calculator through Qronos's normal
        #    packaged-app path. This returns the settled binding.
        # ----------------------------------------------------

        prepared_open = (
            self.computer_executor.prepare(
                ComputerAction(
                    ComputerOperation.OPEN_APP,
                    app_id="calculator",
                )
            )
        )

        if prepared_open.verdict.refused:
            raise CalculatorSemanticPermissionRequired(
                "Calculator launch is refused by the current permission policy."
            )

        open_approval = self._approval(
            prepared_open.verdict,
            voice_confirmed=voice_confirmed,
        )

        opened = (
            self.computer_executor.execute_prepared(
                prepared_open,
                approval=open_approval,
                reevaluate=False,
                cancel_token=cancel_token,
            )
        )

        if opened.result.outcome is ActionOutcome.CANCELLED:
            raise CalculatorSemanticCancelled(
                cancel_token.reason if cancel_token is not None else "cancelled",
                steps_invoked=0,
                steps_total=total_steps,
            )

        if (
            opened.result.outcome
            is not ActionOutcome.SUCCEEDED
        ):
            raise CalculatorSemanticExecutionError(
                opened.result.detail
                or "Calculator could not be opened."
            )

        binding = opened.window_binding

        if binding is None:
            raise CalculatorSemanticExecutionError(
                "Calculator opened without a verified WindowBinding."
            )

        # ----------------------------------------------------
        # 2. Discover exact UIA controls by AutomationID.
        # ----------------------------------------------------

        self._boundary(cancel_token, total_steps)

        elements = (
            actionable_elements_in_window(
                binding.hwnd
            )
        )

        by_automation_id: dict[
            str,
            list,
        ] = {}

        for element in elements:
            key = str(
                element.automation_id
                or ""
            ).strip()

            if key:
                by_automation_id.setdefault(
                    key,
                    [],
                ).append(
                    element
                )

        displays = by_automation_id.get(
            "CalculatorResults",
            [],
        )

        if len(displays) != 1:
            raise CalculatorSemanticExecutionError(
                "CalculatorResults is missing or ambiguous."
            )

        display = displays[0]
        display_runtime_id = (
            display.runtime_id
        )

        step_elements = []

        for automation_id in (
            plan.automation_ids
        ):
            matches = by_automation_id.get(
                automation_id,
                [],
            )

            matches = [
                item
                for item in matches
                if (
                    item.role == "button"
                    and item.invoke_pattern
                    and item.enabled
                )
            ]

            if len(matches) != 1:
                raise CalculatorSemanticExecutionError(
                    "Expected exactly one enabled invokable "
                    f"{automation_id!r}; found {len(matches)}."
                )

            step_elements.append(
                matches[0]
            )

        # ----------------------------------------------------
        # 3. One bounded GUI request for this exact sequence.
        # ----------------------------------------------------

        self._boundary(cancel_token, total_steps)

        request = ActionRequest(
            category=ActionCategory.GUI_AUTOMATION,
            target=f"app:{binding.app_id}",
            summary=(
                "Execute one exact Calculator UI Automation "
                f"sequence for {plan.expression}."
            ),
            parameters={
                "operation":
                    "calculator_uia_sequence",
                "sequence":
                    ">".join(
                        plan.automation_ids
                    ),
                "stepCount":
                    len(
                        plan.automation_ids
                    ),
                "expectedDisplay":
                    plan.expected_display,
                "stateRuntimeId":
                    display_runtime_id,
            },
        )

        audit = (
            self.computer_executor.audit
        )

        verdict = evaluate(
            request,
            audit=(
                audit.record_verdict
                if audit is not None
                else None
            ),
        )

        if verdict.refused:
            raise CalculatorSemanticPermissionRequired(
                "Calculator GUI automation is refused by the current policy."
            )

        gui_approval = self._approval(
            verdict,
            voice_confirmed=voice_confirmed,
        )

        grant = issue_grant(
            request,
            verdict,
            approval=gui_approval,
            ttl_seconds=120.0,
        )

        locator = AccessibilityTargetLocator(
            None
        )

        executor = AccessibilityActionExecutor(
            self.computer_executor.window_binder,
            self.computer_executor,
            locator,
            cancel_token=cancel_token,
        )

        invoked: list[str] = []

        try:

            for index, expected in enumerate(
                step_elements
            ):

                checkpoint(cancel_token)
                run = executor.invoke_target(
                    binding,
                    expected.name,
                    role="button",
                    grant=grant,
                )

                actual_runtime_id = (
                    run.target.element.runtime_id
                )

                if (
                    actual_runtime_id
                    != expected.runtime_id
                ):
                    raise CalculatorSemanticExecutionError(
                        "UIA locator resolved a different RuntimeId "
                        f"for {expected.automation_id!r}."
                    )

                invoked.append(
                    actual_runtime_id
                )

                settle = (
                    0.08
                    if index + 1
                    < len(step_elements)
                    else 0.18
                )
                if cancel_token is not None:
                    cancel_token.sleep(settle)
                else:
                    time.sleep(settle)

            # ------------------------------------------------
            # 4. Independent machine-readable postcondition.
            # ------------------------------------------------

            final_display = (
                actionable_element_by_runtime_id(
                    binding.hwnd,
                    display_runtime_id,
                )
            )

            if final_display is None:
                raise CalculatorSemanticExecutionError(
                    "CalculatorResults disappeared after the sequence."
                )

            if (
                final_display.runtime_id
                != display_runtime_id
            ):
                raise CalculatorSemanticExecutionError(
                    "CalculatorResults RuntimeId changed unexpectedly."
                )

            if (
                final_display.name
                != plan.expected_display
            ):
                raise CalculatorSemanticExecutionError(
                    "Calculator postcondition mismatch: "
                    f"expected {plan.expected_display!r}, "
                    f"observed {final_display.name!r}."
                )

            self.computer_executor.window_binder.revalidate(
                binding,
                require_foreground=False,
                require_same_layout=False,
            )

            result = ActionResult(
                action_id=request.action_id,
                outcome=ActionOutcome.SUCCEEDED,
                detail=(
                    f"Verified Calculator result: "
                    f"{final_display.name}."
                ),
            )

            if audit is not None:
                audit.record_result(
                    request,
                    result,
                )

            return CalculatorSemanticRun(
                expression=plan.expression,
                result_text=plan.result_text,
                display_name=final_display.name,
                invoked_runtime_ids=tuple(
                    invoked
                ),
                hwnd=binding.hwnd,
                pid=binding.snapshot.pid,
            )

        except ActionCancelled as exc:

            # Stop means no further input, including recovery: the display
            # may hold a partial expression, and the outcome says so.
            result = ActionResult(
                action_id=request.action_id,
                outcome=ActionOutcome.CANCELLED,
                detail=(
                    f"Cancelled ({exc.reason}) after {len(invoked)} of "
                    f"{total_steps} Calculator step(s); no recovery input sent."
                ),
            )

            if audit is not None:
                audit.record_result(
                    request,
                    result,
                )

            raise CalculatorSemanticCancelled(
                exc.reason,
                steps_invoked=len(invoked),
                steps_total=total_steps,
            ) from exc

        except Exception as exc:

            # ------------------------------------------------
            # Fail closed. If a partial arithmetic sequence was
            # executed, attempt only the exact already-discovered
            # Clear control as bounded recovery.
            # ------------------------------------------------

            if invoked:
                try:
                    clear_matches = (
                        by_automation_id.get(
                            "clearButton",
                            [],
                        )
                    )

                    if len(clear_matches) == 1:
                        clear = clear_matches[0]

                        executor.invoke_target(
                            binding,
                            clear.name,
                            role="button",
                            grant=grant,
                        )
                except Exception:
                    pass

            result = ActionResult(
                action_id=request.action_id,
                outcome=ActionOutcome.FAILED,
                detail=(
                    f"{type(exc).__name__}: {exc}"
                ),
            )

            if audit is not None:
                audit.record_result(
                    request,
                    result,
                )

            if isinstance(
                exc,
                CalculatorSemanticExecutionError,
            ):
                raise

            raise CalculatorSemanticExecutionError(
                str(exc)
            ) from exc
