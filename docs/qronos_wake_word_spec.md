# Qronos Wake Word Specification

## 1. Purpose

The Qronos Wake Word feature detects the spoken phrase:

Qronos

A successful detection produces a `wake_word_detected` event.

The feature must work locally.

The feature must not send microphone audio to a cloud service.

## 2. Scope

This feature contains:

- Wake-word model integration.
- OpenWakeWord engine adapter.
- Audio frame processing.
- Detection threshold configuration.
- False-positive protection.
- Event generation.
- Unit tests.

This feature does not contain:

- Speech-to-text.
- Natural-language command processing.
- LLM execution.
- Permission decisions.
- Command execution.
- Voice responses.

## 3. Runtime Flow

Microphone

    ↓

AudioInput

    ↓

16 kHz mono int16 frames

    ↓

OpenWakeWordEngine

    ↓

Qronos wake-word model

    ↓

Detection threshold

    ↓

VoiceTriggerService

    ↓

wake_word_detected event

## 4. Model

Target phrase:

Qronos

The production model must be a custom wake-word model.

The test environment may use an existing OpenWakeWord model.

The production model must not depend on the `Hey Jarvis` test model.

## 5. Model Interface

The rest of Qronos must not depend directly on OpenWakeWord.

The adapter must expose:

```text
start()
stop()
pause()
resume()
is_running()
process_audio(audio_data)