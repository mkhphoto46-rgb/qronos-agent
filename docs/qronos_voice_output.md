# Qronos's voice

How Qronos speaks, which settings it speaks with, and why those and not others.

## What it is

Chatterbox Persian — a two-model pipeline from Resemble AI, fine-tuned for
Persian and converted to GGUF — driven by the CrispASR runtime, entirely on
this machine. Nothing is sent anywhere.

| | |
|---|---|
| Runtime | CrispASR 0.8.30, Vulkan build |
| Text model | `t3-fa-q4_k.gguf`, 369 MB |
| Vocoder | `chatterbox-s3gen-q8_0.gguf`, 348 MB |
| Output | 24 kHz mono wave |
| Code | `core/voice_runtime.py` (the interface), `core/chatterbox_runtime.py` |

Neither model is in this repository. Both live under `runtime/chatterbox/`,
which is ignored, exactly as the speech-to-text models do under
`runtime/whisper/`.

## The settings, and the measurements behind them

A sweep of thirty-eight runs across six quantisations, four step counts and
four device placements was measured on the development card. Three conclusions
came out of it, and one more came out of looking at what the sweep did not
measure.

**q4_k weights.** The quantisations were indistinguishable on quality and
clearly separated on cost. That first half is the important one and it is easy
to get wrong: ranked by error rate, `q6_k` looked best and `f16` looked worst,
which is backwards and should have been the clue. Repeating one configuration
across five seeds moved the error rate by 0.069 to 0.138, while the entire
spread *between* quantisations was 0.075 to 0.283. The ranking was noise. Cost
was not: q4_k was at once the fastest, the lightest on the graphics card and
the lightest on host memory. When one option wins on every axis that can be
measured and ties on the axis that cannot, it wins.

**The whole pipeline on the graphics card**, via
`CRISPASR_CHATTERBOX_FORCE_GPU=1`. The runtime's own default puts the text
model on the processor and only the vocoder on the card, and measured, that
default is the slow choice — 0.74 seconds of work per second of speech against
0.40 with everything on the card, and it uses *more* host memory rather than
less. Entirely on the processor is 2.4 times slower than real time and
unusable.

**Ten diffusion steps.** Twenty is slower for no measurable gain; four is no
faster than ten because by then the fixed costs dominate.

**And the model is not reloaded per sentence.** This is the part the sweep did
not examine, and it turned out to be most of the time. Every run in it launched
the executable afresh, so each sentence paid to load the model again: across
fifteen sentences the gap between wall time and generation time was a constant
1.6 seconds. For a short acknowledgement — which is most of what an assistant
actually says — that is three times the cost of the work itself.

So Qronos runs CrispASR as a local server with the model resident and speaks
over the loopback address. Measured on the same card:

| | benchmark, one process per line | resident model |
|---|---|---|
| acknowledgement | 2.15 s | 0.88 s |
| a fact with numbers | 2.62 s | 1.33 s |

## Holding the card, and giving it back

Qronos deliberately stopped keeping its brains resident: the fast brain cost
3,442 MiB to save 1.7 seconds, which is not a good trade on somebody else's
graphics card. The voice is a different trade — a fraction of the memory, a
comparable saving, and asked for far more often — so it stays loaded. But only
while there is a conversation happening. After two minutes of silence it
releases the card by itself.

## It will not squeeze onto a full card

The voice needs about **1,497 MiB while it is speaking**, and it refuses to
start below roughly 2,500 MiB free.

The gap between those two numbers, and between them and the model's own size,
is the whole point — and getting it wrong is not a rounding error. Loading the
model moves the card by 542 MiB, which is what a naive reading gives. The sweep
measured the *peak* for these settings at 1,497, because the vocoder and the
diffusion buffers are allocated while it speaks and not before. And even 1,497
free is not enough in practice:

| free graphics memory | seconds of work per second of speech |
|---|---|
| ~12,000 MiB | 0.33 – 0.65 |
| 1,739 MiB | 2.89 – 7.09 |
| 931 MiB | 3.01 |

At 1,739 MiB free — more than the model needs to load, more than its measured
peak — the voice **did not fail**. It got slow, in the way that is worst:
silently. No error, audio fine, several times slower than a person can listen,
and the whole time fighting the application that was there first. That is what
the first version of this check got wrong: it used the load-time figure of 640
MiB, let the voice through on a card it could only crawl on, and the live
harness caught it.

Where it stops being fast is bracketed, not known — nothing was measured
between 1,739 and 12,000 MiB free — so the headroom is deliberately generous
and sits outside the range observed to fail. Being too cautious costs a message
the user can read; being not cautious enough costs a voice that appears broken.

> Qronos cannot speak right now. The voice needs 1497 MB of graphics memory and
> 1481 MB is free. Speaking anyway would push another application off the card,
> so it waits instead.

Checked on the way in only: a voice already speaking is not cut off because
something else grew. A card that cannot be read at all is not treated as a card
with no room — `read_gpu_status()` returns nothing both when the read fails and
when there is no NVIDIA card in the machine, and refusing on that would leave
Qronos permanently mute on every AMD and Intel machine.

The check lives in `core/chatterbox_runtime.py` and asks only about the card,
so the voice needs nothing else in Qronos to work. There is a general safety
floor in the resource work — `core/hard_floor.py`, on another branch — which
asks this and more besides; the numbers here are deliberately identical to that
module's, so when the two meet, merging them is a deletion rather than a
decision.

## Testing it

`tests/test_chatterbox_runtime.py` — 39 tests, no models needed. A stand-in
HTTP server plays the part of CrispASR, so the whole path an utterance takes is
exercised on the Linux machine the suite runs on. What it cannot tell you is
whether the audio sounds like Persian.

`tools/test_qronos_voice_live.py` — real runtime, real weights, real audio,
and it reports what everything cost. When the card is busy it says so and
measures what it still can, rather than reporting timings that describe
whatever else was open.

## Known limits

- **The licence is not settled.** The GGUF conversion is published as MIT, but
  it derives from weights released under CC BY-NC 4.0, which is
  non-commercial. Nothing here distributes the weights — they are downloaded to
  an ignored directory — but anyone intending to ship this needs to resolve
  that first. Recorded here as a fact, not as advice.
- **Qronos does not speak yet.** This is the ability to produce speech. Wiring
  it into a voice turn, so an answer is spoken rather than written, is a
  separate change.
- **Long text is one utterance.** CrispASR can stream sentence by sentence
  (`--tts-stream`), which would let a long answer start playing after its first
  sentence instead of after all of it. Not built. On the measured numbers a
  254-character introduction takes about ten seconds to produce, all of which
  is currently silence.
- **Only the Vulkan build has been measured.** A CUDA build of the same version
  exists and may well be faster on this card. Untested.
- **One voice.** The default. CrispASR supports voice profiles via
  `--voice-dir`; nothing here uses them.
