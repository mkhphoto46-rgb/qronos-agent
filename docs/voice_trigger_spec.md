# Qronos Voice Trigger Specification

## 1. Purpose

The Voice Trigger detects the wake word:

`Qronos`

The detector starts a voice interaction session.

The first version only handles wake-word detection.

Speech-to-text, command execution, and voice responses are separate features.

## 2. Goals

The first version must:

* Detect the wake word locally.
* Work without Internet access.
* Work without Ollama.
* Run continuously in the background.
* Use low CPU resources.
* Continue working during Gaming and Creator activity.
* Avoid sending microphone audio to a remote service.
* Provide a clear state change when the wake word is detected.

## 3. Non-Goals

The first version must not:

* Convert normal speech to text.
* Execute commands.
* Control the operating system.
* Send microphone audio to a cloud service.
* Load the Heavy Brain.
* Keep an LLM model loaded only for wake-word detection.
* Perform continuous cloud speech recognition.

## 4. System Flow

```text
Microphone
    ↓
Audio Capture
    ↓
Wake Word Detector
    ↓
"Qronos" detected
    ↓
Voice Session Started
    ↓
Future Speech-to-Text Feature
```

## 5. Runtime States

The Voice Trigger has these states:

### Disabled

The microphone is not used by the Voice Trigger.

### Listening

The microphone is active and the wake-word detector is running.

### Triggered

The wake word was detected.

The system starts a voice session.

### Paused

Wake-word detection is temporarily stopped.

The system must be able to return to Listening.

### Error

The Voice Trigger cannot access the required audio device or detector.

The error must not crash Qronos.

## 6. Privacy Rules

Microphone access is disabled by default.

The user must explicitly enable the Voice Trigger.

The wake-word detector must process audio locally.

Raw microphone audio must not be uploaded by the wake-word detector.

No microphone recording file may be created unless another feature explicitly requests recording and the user has approved it.

## 7. Resource Rules

Wake-word detection must be treated as a lightweight background service.

Normal state:

```text
Wake Word: ON
Fast Brain: normal lifecycle
Heavy Brain: normal lifecycle
```

Gaming or Creator state:

```text
Wake Word: ON
Fast Brain: On-Demand
Heavy Brain: BLOCK
Vision: OFF
Background AI: OFF
```

Critical resource state:

```text
Wake Word: ON when possible
Fast Brain: On-Demand
Heavy Brain: BLOCK
Vision: OFF
Background AI: OFF
```

The Voice Trigger must not be disabled only because Gaming or Creator activity is detected.

## 8. Wake Word Behavior

The detector must distinguish the wake word from normal speech.

Required behavior:

```text
User: "Hello, how are you?"
→ No trigger

User: "Qronos"
→ Trigger

User: "Qronos, open..."
→ Trigger
```

The first implementation should prioritize low false positives.

A false positive that repeatedly interrupts the user is considered a major usability problem.

## 9. Audio Requirements

The audio subsystem must support:

* Microphone device discovery.
* Selecting the active microphone.
* Detecting unavailable microphones.
* Starting and stopping audio capture.
* Handling audio-device errors.
* Recovering from temporary device failures.

The audio layer must be isolated from the wake-word engine.

## 10. Detector Interface

The wake-word implementation must expose a simple interface:

```text
start()
stop()
pause()
resume()
is_running()
process_audio()
```

The rest of Qronos must not depend directly on a specific wake-word library.

This allows the detector implementation to be replaced later.

## 11. Architecture

The feature should use these logical components:

```text
VoiceTriggerService
        ↓
AudioInput
        ↓
WakeWordEngine
        ↓
VoiceTriggerEvent
        ↓
Future Voice Session Manager
```

### VoiceTriggerService

Controls the lifecycle.

### AudioInput

Handles microphone access.

### WakeWordEngine

Detects the wake word.

### VoiceTriggerEvent

Reports a successful detection.

## 12. Event Contract

A successful detection should produce an internal event containing:

```text
event_type = "wake_word_detected"
wake_word = "Qronos"
timestamp = detection time
```

The event must not contain raw microphone audio.

## 13. Failure Handling

Microphone unavailable:

```text
State → ERROR
Qronos continues running
```

Wake-word engine failure:

```text
State → ERROR
Qronos continues running
```

Temporary microphone failure:

```text
Attempt recovery
↓
If recovery succeeds → LISTENING
If recovery fails → ERROR
```

Voice Trigger failure must never terminate the main Qronos process.

## 14. Testing Requirements

Unit tests must not require a real microphone.

Unit tests must not require a real audio device.

Unit tests must not require Ollama.

Unit tests must not require Internet access.

Unit tests must use simulated audio input.

The test suite must verify:

* Initial state.
* Start.
* Stop.
* Pause.
* Resume.
* Wake-word detection.
* Non-wake-word audio.
* Microphone failure.
* Detector failure.
* Recovery.
* Event generation.
* Privacy behavior.
* State transitions.

## 15. Integration Testing

A separate integration test may use a real microphone.

Integration tests must not run as part of the normal GitHub CI test suite.

The integration test must be explicitly started.

Example:

```text
python -m unittest discover -s integration_tests -v
```

## 16. Security Requirements

The Voice Trigger must not:

* Execute commands directly.
* Bypass Qronos permissions.
* Enable remote access.
* Store microphone recordings without authorization.
* Send audio to external services without explicit approval.

The wake word only starts a voice session.

The permission system remains responsible for actions after the session starts.

## 17. Performance Requirements

Initial target:

```text
CPU usage: low
Memory usage: low
Background network traffic: 0
LLM usage before trigger: 0
```

The implementation must be benchmarked on the development machine before optimization decisions are made.

## 18. Definition of Done

The first Voice Trigger feature is complete only when:

* Wake-word engine is isolated behind an interface.
* Microphone access is opt-in.
* Local wake-word detection works.
* State transitions are tested.
* Failure recovery is tested.
* No Ollama dependency exists.
* No Internet dependency exists.
* Unit tests pass locally.
* GitHub CI passes.
* Feature is merged through a Pull Request.
* Feature branch is deleted after merge.

## 19. Future Extensions

Future features may add:

```text
Voice Trigger
    ↓
Speech-to-Text
    ↓
Intent Detection
    ↓
Permission Engine
    ↓
Task Router
    ↓
Model Manager
    ↓
Orchestrator
```

These features must remain separate from the wake-word detector.

## 20. Design Principle

The Voice Trigger is an input gateway.

It is not an AI brain.

It must remain small, local, replaceable, testable, and independent from the model layer.
