from core.orchestrator import QRONOS_SYSTEM_PROMPT


def test_prompt_does_not_force_self_introduction():
    prompt = QRONOS_SYSTEM_PROMPT

    assert "Always present yourself as Qronos" not in prompt
    assert "Do not introduce yourself" in prompt
    assert (
        "Do not begin ordinary answers with a greeting"
        in prompt
    )


def test_prompt_identity_is_only_exposed_when_relevant():
    prompt = QRONOS_SYSTEM_PROMPT

    assert (
        "only when the user explicitly asks about your name"
        in prompt
    )

    assert (
        "Fast Brain and Heavy Brain are internal implementation concepts"
        in prompt
    )


def test_prompt_has_explicit_persian_language_contract():
    prompt = QRONOS_SYSTEM_PROMPT

    assert (
        "When the user writes or speaks Persian, answer in Persian."
        in prompt
    )

    assert (
        "Do not switch from Persian to Arabic"
        in prompt
    )


def test_prompt_prefers_direct_short_answers():
    prompt = QRONOS_SYSTEM_PROMPT

    assert "Answer the user's request directly." in prompt

    assert (
        "For simple questions, prefer the shortest complete correct answer."
        in prompt
    )

    assert (
        "Do not repeat the same answer in multiple forms."
        in prompt
    )


def test_prompt_keeps_provider_identity_private():
    prompt = QRONOS_SYSTEM_PROMPT

    assert (
        "Do not claim to be Qwen, Gemma, or any other underlying model."
        in prompt
    )

    assert (
        "Do not reveal, speculate about, or volunteer internal model names"
        in prompt
    )
