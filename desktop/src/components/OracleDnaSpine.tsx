import {
  useEffect,
  useRef,
} from "react";

const TAU = Math.PI * 2;
const MAX_DPR = 1.25;

const RELEASE_DURATION_MS =
  1200;

const MEMORY_SIGNAL_DURATION_MS =
  1900;

type MemoryRole =
  | "user"
  | "qronos";

export type OracleMemoryPhase =
  | "closed"
  | "opening"
  | "open"
  | "closing";

export type OracleMemoryNode = {
  id: string;
  progress: number;
  role: MemoryRole;
};

export type OracleMemoryAnchor = {
  id: string;
  clientX: number;
  clientY: number;
};

type OracleDnaSpineProps = {
  memories: OracleMemoryNode[];

  activeId: string | null;

  phase:
    OracleMemoryPhase;

  onSelect: (
    id: string,
  ) => void;

  onAnchorChange?: (
    anchor:
      OracleMemoryAnchor | null,
  ) => void;
};

type HelixParticle = {
  progress: number;
  strand: 0 | 1;
  clusterOffset: number;
  sideNoise: number;
  yNoise: number;
  sizeNoise: number;
  alphaNoise: number;
  phaseNoise: number;
};

type DustParticle = {
  progress: number;
  side: number;
  spread: number;
  size: number;
  alpha: number;
  phase: number;

  twinkleStrength: number;
  twinkleSpeed: number;
  twinklePhase: number;
};

type BridgeParticle = {
  progress: number;
  offset: number;
  size: number;
  alpha: number;
  pulse: number;
};

type KnotParticle = {
  angle: number;
  radius: number;
  size: number;
  alpha: number;
  phase: number;
};

type KnotScreenPosition = {
  id: string;
  x: number;
  y: number;
};

type FrozenKnot = {
  id: string;
  x: number;
  y: number;
};

type ReleasedKnot = {
  id: string;
  x: number;
  y: number;
  startedAt: number;
};

type MemorySignal = {
  fromProgress: number;
  toProgress: number;
  startedAt: number;
};

function clamp01(
  value: number,
) {
  return Math.max(
    0,
    Math.min(
      1,
      value,
    ),
  );
}

function smoothStep(
  value: number,
) {
  const t =
    clamp01(
      value,
    );

  return (
    t *
    t *
    (3 - 2 * t)
  );
}

function smootherStep(
  value: number,
) {
  const t =
    clamp01(
      value,
    );

  return (
    t *
    t *
    t *
    (
      t *
        (
          t * 6 -
          15
        ) +
      10
    )
  );
}

function mix(
  a: number,
  b: number,
  t: number,
) {
  return (
    a +
    (b - a) * t
  );
}

function seededRandom(
  seed: number,
) {
  let value =
    seed >>> 0;

  return () => {
    value =
      (
        value *
          1664525 +
        1013904223
      ) >>>
      0;

    return (
      value /
      4294967296
    );
  };
}

function createSprite(
  type:
    | "cyan"
    | "violet"
    | "white",
) {
  const canvas =
    document.createElement(
      "canvas",
    );

  canvas.width = 32;
  canvas.height = 32;

  const context =
    canvas.getContext(
      "2d",
    );

  if (!context) {
    return canvas;
  }

  const gradient =
    context.createRadialGradient(
      16,
      16,
      0,
      16,
      16,
      16,
    );

  if (type === "cyan") {
    gradient.addColorStop(
      0,
      "rgba(240,253,255,1)",
    );

    gradient.addColorStop(
      0.14,
      "rgba(116,233,255,0.98)",
    );

    gradient.addColorStop(
      0.42,
      "rgba(43,177,231,0.42)",
    );
  } else if (
    type === "violet"
  ) {
    gradient.addColorStop(
      0,
      "rgba(244,240,255,1)",
    );

    gradient.addColorStop(
      0.14,
      "rgba(160,136,255,0.96)",
    );

    gradient.addColorStop(
      0.42,
      "rgba(94,68,222,0.35)",
    );
  } else {
    gradient.addColorStop(
      0,
      "rgba(255,255,255,1)",
    );

    gradient.addColorStop(
      0.15,
      "rgba(211,249,255,0.98)",
    );

    gradient.addColorStop(
      0.44,
      "rgba(91,213,250,0.34)",
    );
  }

  gradient.addColorStop(
    1,
    "rgba(0,0,0,0)",
  );

  context.fillStyle =
    gradient;

  context.fillRect(
    0,
    0,
    32,
    32,
  );

  return canvas;
}

function buildHelixParticles() {
  const random =
    seededRandom(48192);

  const particles:
    HelixParticle[] = [];

  const steps = 190;

  for (
    let step = 0;
    step < steps;
    step += 1
  ) {
    const progress =
      step /
      (steps - 1);

    for (
      let strand = 0;
      strand < 2;
      strand += 1
    ) {
      for (
        let cluster = 0;
        cluster < 6;
        cluster += 1
      ) {
        particles.push({
          progress,

          strand:
            strand as
              | 0
              | 1,

          clusterOffset:
            -1.25 +
            cluster * 0.5,

          sideNoise:
            (random() -
              0.5) *
            5.2,

          yNoise:
            (random() -
              0.5) *
            4.4,

          sizeNoise:
            0.4 +
            random() *
              0.82,

          alphaNoise:
            0.46 +
            random() *
              0.54,

          phaseNoise:
            (random() -
              0.5) *
            0.14,
        });
      }
    }
  }

  return particles;
}

function buildDustParticles() {
  const random =
    seededRandom(81231);

  const particles:
    DustParticle[] = [];

  /*
   * Original:
   * 560
   *
   * Current target:
   * ~56% more than original.
   *
   * 560 × 1.56 ≈ 874
   */
  for (
    let index = 0;
    index < 874;
    index += 1
  ) {
    const extraParticle =
      index >= 560;

    const canTwinkle =
      random() <
      0.18;

    particles.push({
      progress:
        random(),

      side:
        random() >
        0.5
          ? 1
          : -1,

      spread:
        4 +
        Math.pow(
          random(),
          0.82,
        ) *
          68,

      size:
        extraParticle
          ? 0.13 +
            random() *
              0.74
          : 0.18 +
            random() *
              0.92,

      alpha:
        extraParticle
          ? 0.011 +
            random() *
              0.09
          : 0.018 +
            random() *
              0.16,

      phase:
        random() *
        TAU,

      twinkleStrength:
        canTwinkle
          ? 0.28 +
            random() *
              0.48
          : 0,

      twinkleSpeed:
        0.24 +
        random() *
          0.42,

      twinklePhase:
        random() *
        TAU,
    });
  }

  return particles;
}

function buildBridgeParticles() {
  const random =
    seededRandom(19317);

  const particles:
    BridgeParticle[] = [];

  for (
    let row = 0;
    row < 30;
    row += 1
  ) {
    const progress =
      0.035 +
      row *
        0.032;

    for (
      let dot = 1;
      dot <= 10;
      dot += 1
    ) {
      particles.push({
        progress,

        offset:
          dot / 11,

        size:
          0.38 +
          random() *
            0.46,

        alpha:
          0.11 +
          random() *
            0.16,

        pulse:
          random() *
          TAU,
      });
    }
  }

  return particles;
}

function buildKnotParticles() {
  const random =
    seededRandom(59217);

  const particles:
    KnotParticle[] = [];

  for (
    let index = 0;
    index < 62;
    index += 1
  ) {
    particles.push({
      angle:
        random() *
        TAU,

      radius:
        2 +
        Math.pow(
          random(),
          0.7,
        ) *
          19,

      size:
        0.35 +
        random() *
          0.95,

      alpha:
        0.25 +
        random() *
          0.65,

      phase:
        random() *
        TAU,
    });
  }

  return particles;
}

const helixParticles =
  buildHelixParticles();

const dustParticles =
  buildDustParticles();

const bridgeParticles =
  buildBridgeParticles();

const knotParticles =
  buildKnotParticles();

function OracleDnaSpine({
  memories,
  activeId,
  phase,
  onSelect,
  onAnchorChange,
}: OracleDnaSpineProps) {
  const canvasRef =
    useRef<HTMLCanvasElement | null>(
      null,
    );

  const activeIdRef =
    useRef<string | null>(
      activeId,
    );

  const previousActiveIdRef =
    useRef<string | null>(
      activeId,
    );

  const phaseRef =
    useRef<OracleMemoryPhase>(
      phase,
    );

  const memoriesRef =
    useRef(memories);

  const onSelectRef =
    useRef(onSelect);

  const onAnchorChangeRef =
    useRef(onAnchorChange);

  const hoveredIdRef =
    useRef<string | null>(
      null,
    );

  const knotPositionsRef =
    useRef<
      KnotScreenPosition[]
    >([]);

  const frozenKnotRef =
    useRef<FrozenKnot | null>(
      null,
    );

  const releasedKnotRef =
    useRef<ReleasedKnot | null>(
      null,
    );

  const memorySignalRef =
    useRef<MemorySignal | null>(
      null,
    );

  useEffect(() => {
    const previous =
      previousActiveIdRef.current;

    if (
      previous &&
      activeId &&
      previous !==
        activeId
    ) {
      const previousMemory =
        memoriesRef.current.find(
          (memory) =>
            memory.id ===
            previous,
        );

      const nextMemory =
        memoriesRef.current.find(
          (memory) =>
            memory.id ===
            activeId,
        );

      if (
        previousMemory &&
        nextMemory
      ) {
        memorySignalRef.current =
          {
            fromProgress:
              previousMemory.progress,

            toProgress:
              nextMemory.progress,

            startedAt:
              performance.now(),
          };
      }
    }

    if (
      previous &&
      previous !==
        activeId &&
      frozenKnotRef.current?.id ===
        previous
    ) {
      releasedKnotRef.current =
        {
          id:
            frozenKnotRef.current.id,

          x:
            frozenKnotRef.current.x,

          y:
            frozenKnotRef.current.y,

          startedAt:
            performance.now(),
        };

      frozenKnotRef.current =
        null;
    }

    activeIdRef.current =
      activeId;

    previousActiveIdRef.current =
      activeId;

    if (!activeId) {
      onAnchorChangeRef.current?.(
        null,
      );
    }
  }, [activeId]);

  useEffect(() => {
    phaseRef.current =
      phase;
  }, [phase]);

  useEffect(() => {
    memoriesRef.current =
      memories;
  }, [memories]);

  useEffect(() => {
    onSelectRef.current =
      onSelect;
  }, [onSelect]);

  useEffect(() => {
    onAnchorChangeRef.current =
      onAnchorChange;
  }, [onAnchorChange]);

  useEffect(() => {
    const canvas =
      canvasRef.current;

    if (!canvas) {
      return;
    }

    const parent =
      canvas.parentElement;

    if (!parent) {
      return;
    }

    const context =
      canvas.getContext(
        "2d",
        {
          alpha: true,
        },
      );

    if (!context) {
      return;
    }

    const cyanSprite =
      createSprite(
        "cyan",
      );

    const violetSprite =
      createSprite(
        "violet",
      );

    const whiteSprite =
      createSprite(
        "white",
      );

    let width = 1;
    let height = 1;
    let dpr = 1;

    let animationFrame = 0;

    let visible =
      !document.hidden;

    const publishFrozenAnchor =
      () => {
        const frozen =
          frozenKnotRef.current;

        if (!frozen) {
          return;
        }

        const rect =
          canvas.getBoundingClientRect();

        const scaleX =
          width > 0
            ? rect.width /
              width
            : 1;

        const scaleY =
          height > 0
            ? rect.height /
              height
            : 1;

        onAnchorChangeRef.current?.(
          {
            id:
              frozen.id,

            clientX:
              rect.left +
              frozen.x *
                scaleX,

            clientY:
              rect.top +
              frozen.y *
                scaleY,
          },
        );
      };

    const resize =
      () => {
        width =
          Math.max(
            1,
            parent.clientWidth,
          );

        height =
          Math.max(
            1,
            parent.clientHeight,
          );

        dpr =
          Math.min(
            window.devicePixelRatio ||
              1,
            MAX_DPR,
          );

        canvas.width =
          Math.round(
            width *
              dpr,
          );

        canvas.height =
          Math.round(
            height *
              dpr,
          );

        canvas.style.width =
          `${width}px`;

        canvas.style.height =
          `${height}px`;

        context.setTransform(
          dpr,
          0,
          0,
          dpr,
          0,
          0,
        );

        window.requestAnimationFrame(
          publishFrozenAnchor,
        );
      };

    const drawSprite = (
      sprite:
        HTMLCanvasElement,
      x: number,
      y: number,
      size: number,
      alpha: number,
    ) => {
      if (
        alpha <=
        0.006
      ) {
        return;
      }

      const renderSize =
        Math.max(
          1.8,
          size *
            7,
        );

      context.globalAlpha =
        Math.min(
          1,
          alpha,
        );

      context.drawImage(
        sprite,
        x -
          renderSize /
            2,
        y -
          renderSize /
            2,
        renderSize,
        renderSize,
      );
    };

    const resizeObserver =
      new ResizeObserver(
        resize,
      );

    resizeObserver.observe(
      parent,
    );

    resize();

    const handleVisibility =
      () => {
        visible =
          !document.hidden;
      };

    document.addEventListener(
      "visibilitychange",
      handleVisibility,
    );

    const handlePointerMove = (
      event: PointerEvent,
    ) => {
      const rect =
        canvas.getBoundingClientRect();

      const scaleX =
        width > 0
          ? rect.width /
            width
          : 1;

      const scaleY =
        height > 0
          ? rect.height /
            height
          : 1;

      const x =
        (
          event.clientX -
          rect.left
        ) /
        Math.max(
          0.001,
          scaleX,
        );

      const y =
        (
          event.clientY -
          rect.top
        ) /
        Math.max(
          0.001,
          scaleY,
        );

      let nearest:
        | string
        | null = null;

      let nearestDistance =
        34;

      for (
        const knot of
        knotPositionsRef.current
      ) {
        const dx =
          knot.x -
          x;

        const dy =
          knot.y -
          y;

        const distance =
          Math.sqrt(
            dx * dx +
              dy * dy,
          );

        if (
          distance <
          nearestDistance
        ) {
          nearestDistance =
            distance;

          nearest =
            knot.id;
        }
      }

      hoveredIdRef.current =
        nearest;

      canvas.style.cursor =
        nearest
          ? "pointer"
          : "default";
    };

    const handlePointerLeave =
      () => {
        hoveredIdRef.current =
          null;

        canvas.style.cursor =
          "default";
      };

    const handlePointerDown =
      () => {
        const hovered =
          hoveredIdRef.current;

        if (!hovered) {
          return;
        }

        onSelectRef.current(
          hovered,
        );
      };

    canvas.addEventListener(
      "pointermove",
      handlePointerMove,
    );

    canvas.addEventListener(
      "pointerleave",
      handlePointerLeave,
    );

    canvas.addEventListener(
      "pointerdown",
      handlePointerDown,
    );

    const render = (
      timestamp: number,
    ) => {
      animationFrame =
        requestAnimationFrame(
          render,
        );

      if (!visible) {
        return;
      }

      context.clearRect(
        0,
        0,
        width,
        height,
      );

      const time =
        timestamp *
        0.001;

      const centerX =
        width *
        0.48;

      const topPadding =
        height *
        0.025;

      const usableHeight =
        height *
        0.95;

      const amplitude =
        Math.min(
          width *
            0.175,
          52,
        );

      const motion =
        time *
        0.285;

      const activeSignal =
        memorySignalRef.current;

      let signalRaw = 0;

      let signalTravel = 0;

      let signalCurrentProgress =
        0;

      let signalMin = 0;
      let signalMax = 0;

      let signalVisualEnvelope =
        0;

      let signalMotionEnvelope =
        0;

      let signalWaveRadius =
        0.01;

      if (activeSignal) {
        signalRaw =
          clamp01(
            (
              timestamp -
              activeSignal.startedAt
            ) /
              MEMORY_SIGNAL_DURATION_MS,
          );

        /*
         * Important:
         *
         * The wave reaches the destination
         * around 80% of the total animation.
         *
         * After that:
         * its position DOES NOT move anymore.
         * It only fades away.
         */
        signalTravel =
          smootherStep(
            (
              signalRaw -
              0.08
            ) /
              0.72,
          );

        signalCurrentProgress =
          mix(
            activeSignal.fromProgress,
            activeSignal.toProgress,
            signalTravel,
          );

        signalMin =
          Math.min(
            activeSignal.fromProgress,
            activeSignal.toProgress,
          );

        signalMax =
          Math.max(
            activeSignal.fromProgress,
            activeSignal.toProgress,
          );

        /*
         * Visual envelope:
         *
         * born softly,
         * stays visible,
         * fades to zero after reaching destination.
         */
        const visualBirth =
          smootherStep(
            signalRaw /
              0.2,
          );

        const visualDeath =
          1 -
          smootherStep(
            (
              signalRaw -
              0.78
            ) /
              0.18,
          );

        signalVisualEnvelope =
          visualBirth *
          visualDeath;

        /*
         * Positional deformation ends earlier
         * than the visible wave.
         *
         * This is what removes the visual recoil:
         * by the time the signal is fading at B,
         * DNA positional deformation is already
         * almost completely neutral.
         */
        const motionBirth =
          smootherStep(
            signalRaw /
              0.2,
          );

        const motionDeath =
          1 -
          smootherStep(
            (
              signalRaw -
              0.64
            ) /
              0.2,
          );

        signalMotionEnvelope =
          motionBirth *
          motionDeath;

        /*
         * Wave radius grows softly,
         * then collapses before disappearance.
         */
        const radiusBirth =
          smootherStep(
            signalRaw /
              0.24,
          );

        const radiusDeath =
          1 -
          smootherStep(
            (
              signalRaw -
              0.72
            ) /
              0.22,
          );

        signalWaveRadius =
          mix(
            0.012,
            0.12,
            radiusBirth *
              radiusDeath,
          );
      }

      const getSignalProfile = (
        progress: number,
      ) => {
        if (
          !activeSignal
        ) {
          return 0;
        }

        if (
          progress <
            signalMin -
              0.14 ||
          progress >
            signalMax +
              0.14
        ) {
          return 0;
        }

        const distance =
          Math.abs(
            progress -
              signalCurrentProgress,
          );

        if (
          distance >=
          signalWaveRadius
        ) {
          return 0;
        }

        const local =
          1 -
          distance /
            signalWaveRadius;

        return smootherStep(
          local,
        );
      };

      /*
       * Ambient DNA dust.
       */
      for (
        const dust of
        dustParticles
      ) {
        const signalProfile =
          getSignalProfile(
            dust.progress,
          );

        const visualInfluence =
          signalProfile *
          signalVisualEnvelope;

        const motionInfluence =
          signalProfile *
          signalMotionEnvelope;

        const signalDirection =
          activeSignal
            ? activeSignal.toProgress >=
              activeSignal.fromProgress
              ? 1
              : -1
            : 0;

        const y =
          topPadding +
          dust.progress *
            usableHeight +
          Math.sin(
            time *
              0.26 +
              dust.phase,
          ) *
            3 +
          motionInfluence *
            signalDirection *
            5.5;

        const localWave =
          Math.sin(
            dust.progress *
              TAU *
              3.5 +
              motion +
              motionInfluence *
                0.18,
          );

        const x =
          centerX +
          localWave *
            amplitude *
            (
              0.5 -
              motionInfluence *
                0.04
            ) +
          dust.side *
            dust.spread +
          Math.cos(
            time *
              0.2 +
              dust.phase +
              motionInfluence *
                1.2,
          ) *
            (
              2.2 +
              motionInfluence *
                2.8
            );

        const basePulse =
          0.72 +
          Math.sin(
            time *
              0.9 +
              dust.phase +
              dust.progress *
                6,
          ) *
            0.28;

        /*
         * Rare ambient twinkle.
         *
         * Only selected particles have
         * twinkleStrength > 0.
         *
         * The threshold means most of the time
         * nothing special happens.
         */
        const twinkleWave =
          (
            Math.sin(
              time *
                dust.twinkleSpeed +
                dust.twinklePhase,
            ) +
            1
          ) /
          2;

        const twinkle =
          dust.twinkleStrength >
          0
            ? smootherStep(
                (
                  twinkleWave -
                  0.76
                ) /
                  0.24,
              ) *
              dust.twinkleStrength
            : 0;

        const twinkleAlphaBoost =
          1 +
          twinkle *
            1.35;

        const twinkleSizeBoost =
          1 +
          twinkle *
            0.22;

        drawSprite(
          cyanSprite,
          x,
          y,
          dust.size *
            basePulse *
            twinkleSizeBoost *
            (
              1 +
              visualInfluence *
                0.18
            ),
          dust.alpha *
            basePulse *
            twinkleAlphaBoost *
            (
              1 +
              visualInfluence *
                0.6
            ),
        );

        /*
         * A very small number of stronger twinkles
         * get a tiny white core near peak brightness.
         */
        if (
          twinkle >
            0.42 &&
          dust.size >
            0.42
        ) {
          drawSprite(
            whiteSprite,
            x,
            y,
            dust.size *
              0.52,
            dust.alpha *
              twinkle *
              0.72,
          );
        }

        if (
          dust.size >
            0.72 &&
          (
            Math.sin(
              dust.phase +
                time *
                  0.45,
            ) +
            1
          ) /
            2 >
            0.82
        ) {
          drawSprite(
            whiteSprite,
            x,
            y,
            dust.size *
              0.72,
            dust.alpha *
              0.42 *
              (
                1 +
                visualInfluence *
                  0.8
              ),
          );
        }
      }

      /*
       * DNA bridges.
       */
      for (
        const bridge of
        bridgeParticles
      ) {
        const signalProfile =
          getSignalProfile(
            bridge.progress,
          );

        const visualInfluence =
          signalProfile *
          signalVisualEnvelope;

        const motionInfluence =
          signalProfile *
          signalMotionEnvelope;

        const phaseValue =
          bridge.progress *
            TAU *
            3.5 +
          motion +
          motionInfluence *
            0.22;

        const localAmplitude =
          amplitude *
          (
            1 -
            motionInfluence *
              0.09
          );

        const helixX =
          Math.sin(
            phaseValue,
          ) *
          localAmplitude;

        const leftX =
          centerX -
          Math.abs(
            helixX,
          );

        const rightX =
          centerX +
          Math.abs(
            helixX,
          );

        const y =
          topPadding +
          bridge.progress *
            usableHeight;

        const x =
          leftX +
          (
            rightX -
            leftX
          ) *
            bridge.offset;

        const centerWeight =
          Math.sin(
            bridge.offset *
              Math.PI,
          );

        const pulse =
          0.72 +
          Math.sin(
            time *
              1.85 +
              bridge.pulse +
              bridge.progress *
                5.8,
          ) *
            0.28;

        const baseAlpha =
          bridge.alpha *
          centerWeight *
          pulse *
          1.35;

        const cascadeBoost =
          1 +
          visualInfluence *
            2.15;

        const sizeBoost =
          1 +
          visualInfluence *
            0.22;

        drawSprite(
          whiteSprite,
          x,
          y,
          bridge.size *
            1.06 *
            sizeBoost,
          baseAlpha *
            cascadeBoost,
        );

        drawSprite(
          cyanSprite,
          x,
          y,
          bridge.size *
            0.68 *
            sizeBoost,
          baseAlpha *
            0.34 *
            cascadeBoost,
        );
      }

      /*
       * Main DNA strands.
       */
      for (
        const particle of
        helixParticles
      ) {
        const progress =
          particle.progress;

        const signalProfile =
          getSignalProfile(
            progress,
          );

        const visualInfluence =
          signalProfile *
          signalVisualEnvelope;

        const motionInfluence =
          signalProfile *
          signalMotionEnvelope;

        const phaseBoost =
          motionInfluence *
          0.34;

        const phaseValue =
          progress *
            TAU *
            3.5 +
          motion +
          particle.phaseNoise +
          phaseBoost;

        const strandPhase =
          particle.strand ===
          0
            ? phaseValue
            : phaseValue +
              Math.PI;

        const wave =
          Math.sin(
            strandPhase,
          );

        const depth =
          (
            Math.cos(
              strandPhase,
            ) +
            1
          ) /
          2;

        const localAmplitude =
          amplitude *
          (
            1 -
            motionInfluence *
              0.11
          );

        const signalDirection =
          activeSignal
            ? activeSignal.toProgress >=
              activeSignal.fromProgress
              ? 1
              : -1
            : 0;

        const y =
          topPadding +
          progress *
            usableHeight +
          particle.yNoise +
          motionInfluence *
            signalDirection *
            5.5;

        const baseX =
          centerX +
          wave *
            localAmplitude;

        const thickness =
          2.2 +
          depth *
            7.2 +
          motionInfluence *
            1.5;

        const x =
          baseX +
          particle.clusterOffset *
            thickness +
          particle.sideNoise *
            (
              0.2 +
              depth *
                0.5
            );

        const fadeTop =
          Math.min(
            1,
            progress /
              0.075,
          );

        const fadeBottom =
          Math.min(
            1,
            (
              1 -
              progress
            ) /
              0.09,
          );

        const edgeFade =
          Math.min(
            fadeTop,
            fadeBottom,
          );

        const alpha =
          edgeFade *
          particle.alphaNoise *
          (
            0.15 +
            depth *
              0.7
          ) *
          (
            1 +
            visualInfluence *
              0.72
          );

        const size =
          particle.sizeNoise *
          (
            0.29 +
            depth *
              0.86
          ) *
          (
            1 +
            visualInfluence *
              0.14
          );

        drawSprite(
          particle.strand ===
            0
            ? cyanSprite
            : violetSprite,
          x,
          y,
          size,
          alpha,
        );
      }

      /*
       * Subtle moving signal.
       *
       * Important:
       * the tail no longer collapses/reverses
       * during fade-out.
       *
       * Once the signal reaches B,
       * its geometry stays fixed and simply fades.
       */
      if (
        activeSignal
      ) {
        const signalAlpha =
          signalVisualEnvelope;

        const trailCount =
          28;

        const movingDown =
          activeSignal.toProgress >=
          activeSignal.fromProgress;

        for (
          let trailIndex = 0;
          trailIndex <
          trailCount;
          trailIndex += 1
        ) {
          const normalizedTrail =
            trailIndex /
            (
              trailCount -
              1
            );

          const direction =
            movingDown
              ? -1
              : 1;

          /*
           * Note:
           * no signalMotionEnvelope multiplier here.
           *
           * This prevents the tail from shrinking
           * toward the head and looking like
           * it moves backward near the end.
           */
          const trailProgress =
            clamp01(
              signalCurrentProgress +
                direction *
                  normalizedTrail *
                  0.11,
            );

          const localFade =
            Math.pow(
              1 -
                normalizedTrail,
              1.6,
            );

          for (
            let strand = 0;
            strand < 2;
            strand += 1
          ) {
            const signalProfile =
              getSignalProfile(
                trailProgress,
              );

            const motionInfluence =
              signalProfile *
              signalMotionEnvelope;

            const phaseValue =
              trailProgress *
                TAU *
                3.5 +
              motion +
              motionInfluence *
                0.34 +
              (
                strand === 0
                  ? 0
                  : Math.PI
              );

            const localAmplitude =
              amplitude *
              (
                1 -
                motionInfluence *
                  0.11
              );

            const x =
              centerX +
              Math.sin(
                phaseValue,
              ) *
                localAmplitude;

            const y =
              topPadding +
              trailProgress *
                usableHeight;

            const sprite =
              strand === 0
                ? cyanSprite
                : violetSprite;

            drawSprite(
              sprite,
              x,
              y,
              0.58 +
                localFade *
                  0.18,
              signalAlpha *
                localFade *
                0.34,
            );

            if (
              trailIndex %
                6 ===
              0
            ) {
              drawSprite(
                whiteSprite,
                x,
                y,
                0.38,
                signalAlpha *
                  localFade *
                  0.22,
              );
            }
          }
        }

        /*
         * Source birth.
         */
        if (
          signalRaw <
          0.24
        ) {
          const birthProgress =
            smootherStep(
              signalRaw /
                0.24,
            );

          const birthPulse =
            Math.sin(
              birthProgress *
                Math.PI,
            );

          const sourceProgress =
            activeSignal.fromProgress;

          const sourcePhase =
            sourceProgress *
              TAU *
              3.5 +
            motion;

          const sourceX =
            centerX +
            Math.sin(
              sourcePhase,
            ) *
              amplitude *
              0.46;

          const sourceY =
            topPadding +
            sourceProgress *
              usableHeight;

          drawSprite(
            cyanSprite,
            sourceX,
            sourceY,
            0.9 +
              birthPulse *
                0.42,
            birthPulse *
              0.16,
          );

          drawSprite(
            whiteSprite,
            sourceX,
            sourceY,
            0.44 +
              birthPulse *
                0.16,
            birthPulse *
              0.12,
          );
        }

        /*
         * Destination fade.
         *
         * No pulse that rises and falls after arrival.
         *
         * The glow rises while approaching,
         * then simply fades away.
         */
        if (
          signalRaw >
          0.62
        ) {
          const arrivalIn =
            smootherStep(
              (
                signalRaw -
                0.62
              ) /
                0.16,
            );

          const arrivalOut =
            1 -
            smootherStep(
              (
                signalRaw -
                0.8
              ) /
                0.16,
            );

          const arrivalGlow =
            arrivalIn *
            arrivalOut;

          const targetProgress =
            activeSignal.toProgress;

          const targetPhase =
            targetProgress *
              TAU *
              3.5 +
            motion;

          const targetFrozen =
            frozenKnotRef.current;

          const targetX =
            targetFrozen
              ? targetFrozen.x
              : centerX +
                Math.sin(
                  targetPhase,
                ) *
                  amplitude *
                  0.46;

          const targetY =
            targetFrozen
              ? targetFrozen.y
              : topPadding +
                targetProgress *
                  usableHeight;

          drawSprite(
            violetSprite,
            targetX,
            targetY,
            1.08,
            arrivalGlow *
              0.18,
          );

          drawSprite(
            whiteSprite,
            targetX,
            targetY,
            0.52,
            arrivalGlow *
              0.14,
          );
        }

        /*
         * By ~96% everything is already invisible
         * and positionally neutral.
         *
         * Remove the signal before another visible
         * frame can reveal any reset.
         */
        if (
          signalRaw >=
          0.97
        ) {
          memorySignalRef.current =
            null;
        }
      }

      /*
       * Memory knots.
       */
      const knotPositions:
        KnotScreenPosition[] = [];

      const activeIdNow =
        activeIdRef.current;

      for (
        const memory of
        memoriesRef.current
      ) {
        const progress =
          memory.progress;

        const phaseValue =
          progress *
            TAU *
            3.5 +
          motion;

        const strandWave =
          Math.sin(
            phaseValue,
          );

        const naturalX =
          centerX +
          strandWave *
            amplitude *
            0.46;

        const naturalY =
          topPadding +
          progress *
            usableHeight;

        let knotX =
          naturalX;

        let knotY =
          naturalY;

        const selected =
          activeIdNow ===
          memory.id;

        if (selected) {
          if (
            !frozenKnotRef.current ||
            frozenKnotRef.current.id !==
              memory.id
          ) {
            frozenKnotRef.current =
              {
                id:
                  memory.id,

                x:
                  naturalX,

                y:
                  naturalY,
              };

            const canvasRect =
              canvas.getBoundingClientRect();

            const scaleX =
              width > 0
                ? canvasRect.width /
                  width
                : 1;

            const scaleY =
              height > 0
                ? canvasRect.height /
                  height
                : 1;

            onAnchorChangeRef.current?.(
              {
                id:
                  memory.id,

                clientX:
                  canvasRect.left +
                  naturalX *
                    scaleX,

                clientY:
                  canvasRect.top +
                  naturalY *
                    scaleY,
              },
            );
          }

          knotX =
            frozenKnotRef.current.x;

          knotY =
            frozenKnotRef.current.y;
        } else if (
          releasedKnotRef.current?.id ===
          memory.id
        ) {
          const release =
            releasedKnotRef.current;

          const elapsed =
            timestamp -
            release.startedAt;

          const amount =
            smoothStep(
              elapsed /
                RELEASE_DURATION_MS,
            );

          knotX =
            release.x +
            (
              naturalX -
              release.x
            ) *
              amount;

          knotY =
            release.y +
            (
              naturalY -
              release.y
            ) *
              amount;

          if (
            amount >=
            0.999
          ) {
            releasedKnotRef.current =
              null;
          }
        }

        knotPositions.push({
          id:
            memory.id,

          x:
            knotX,

          y:
            knotY,
        });

        const hovered =
          hoveredIdRef.current ===
          memory.id;

        const selectedStrength =
          phaseRef.current ===
            "closing"
            ? 0.78
            : 1;

        const charge =
          selected
            ? selectedStrength
            : hovered
              ? 0.74
              : 0.16;

        for (
          let index = 0;
          index <
          knotParticles.length;
          index += 1
        ) {
          const knot =
            knotParticles[
              index
            ];

          const orbit =
            knot.angle +
            time *
              (
                0.08 +
                (
                  index %
                  5
                ) *
                  0.006
              ) +
            knot.phase;

          const organicRadius =
            knot.radius *
            (
              1 +
              Math.sin(
                time *
                  0.45 +
                  knot.phase,
              ) *
                0.06
            );

          const x =
            knotX +
            Math.cos(
              orbit,
            ) *
              organicRadius;

          const y =
            knotY +
            Math.sin(
              orbit,
            ) *
              organicRadius *
              0.72;

          const roleSprite =
            memory.role ===
            "qronos"
              ? violetSprite
              : cyanSprite;

          drawSprite(
            index %
              11 ===
              0
              ? whiteSprite
              : roleSprite,
            x,
            y,
            knot.size *
              (
                1 +
                charge *
                  0.22
              ),
            knot.alpha *
              (
                0.56 +
                charge *
                  0.62
              ),
          );
        }

        drawSprite(
          whiteSprite,
          knotX,
          knotY,
          1.9,
          0.78 +
            charge *
              0.22,
        );

        drawSprite(
          memory.role ===
            "qronos"
            ? violetSprite
            : cyanSprite,
          knotX,
          knotY,
          2.8,
          0.12 +
            charge *
              0.18,
        );
      }

      knotPositionsRef.current =
        knotPositions;

      context.globalAlpha =
        1;
    };

    animationFrame =
      requestAnimationFrame(
        render,
      );

    return () => {
      cancelAnimationFrame(
        animationFrame,
      );

      resizeObserver.disconnect();

      document.removeEventListener(
        "visibilitychange",
        handleVisibility,
      );

      canvas.removeEventListener(
        "pointermove",
        handlePointerMove,
      );

      canvas.removeEventListener(
        "pointerleave",
        handlePointerLeave,
      );

      canvas.removeEventListener(
        "pointerdown",
        handlePointerDown,
      );
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="oracle-dna-canvas"
      aria-label="Recent memory DNA"
    />
  );
}

export default OracleDnaSpine;