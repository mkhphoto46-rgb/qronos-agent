import {
  useEffect,
  useRef,
} from "react";

const TAU = Math.PI * 2;
const MAX_DPR = 1.25;

type MemoryRole =
  | "user"
  | "qronos";

export type OracleMemoryNode = {
  id: string;
  progress: number;
  role: MemoryRole;
};

type OracleDnaSpineProps = {
  memories: OracleMemoryNode[];
  activeId: string | null;
  onSelect: (
    id: string,
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
};

type BridgeParticle = {
  progress: number;
  offset: number;
  size: number;
  alpha: number;
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

function seededRandom(
  seed: number,
) {
  let value =
    seed >>> 0;

  return () => {
    value =
      (value *
        1664525 +
        1013904223) >>>
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
    canvas.getContext("2d");

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
      "rgba(43,177,231,0.35)",
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
      "rgba(94,68,222,0.31)",
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
      "rgba(91,213,250,0.27)",
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
            strand as 0 | 1,

          clusterOffset:
            -1.25 +
            cluster * 0.5,

          sideNoise:
            (random() - 0.5) *
            5.2,

          yNoise:
            (random() - 0.5) *
            4.4,

          sizeNoise:
            0.4 +
            random() * 0.82,

          alphaNoise:
            0.46 +
            random() * 0.54,

          phaseNoise:
            (random() - 0.5) *
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

  for (
    let index = 0;
    index < 560;
    index += 1
  ) {
    particles.push({
      progress:
        random(),

      side:
        random() > 0.5
          ? 1
          : -1,

      spread:
        6 +
        random() * 62,

      size:
        0.18 +
        random() * 0.92,

      alpha:
        0.018 +
        random() * 0.16,

      phase:
        random() * TAU,
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
      row * 0.032;

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
          0.3 +
          random() * 0.37,

        alpha:
          0.045 +
          random() * 0.13,
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
        random() * TAU,

      radius:
        2 +
        Math.pow(
          random(),
          0.7,
        ) *
          19,

      size:
        0.35 +
        random() * 0.95,

      alpha:
        0.25 +
        random() * 0.65,

      phase:
        random() * TAU,
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
  onSelect,
}: OracleDnaSpineProps) {
  const canvasRef =
    useRef<HTMLCanvasElement | null>(
      null,
    );

  const activeIdRef =
    useRef<string | null>(
      activeId,
    );

  const memoriesRef =
    useRef(memories);

  const onSelectRef =
    useRef(onSelect);

  const hoveredIdRef =
    useRef<string | null>(
      null,
    );

  const knotPositionsRef =
    useRef<
      KnotScreenPosition[]
    >([]);

  useEffect(() => {
    activeIdRef.current =
      activeId;
  }, [activeId]);

  useEffect(() => {
    memoriesRef.current =
      memories;
  }, [memories]);

  useEffect(() => {
    onSelectRef.current =
      onSelect;
  }, [onSelect]);

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
      createSprite("cyan");

    const violetSprite =
      createSprite("violet");

    const whiteSprite =
      createSprite("white");

    let width = 1;
    let height = 1;
    let dpr = 1;

    let animationFrame = 0;

    let visible =
      !document.hidden;

    let bloomAmount = 0;

    let activeProgress =
      0.5;

    const resize = () => {
      const rect =
        parent.getBoundingClientRect();

      width = Math.max(
        1,
        rect.width,
      );

      height = Math.max(
        1,
        rect.height,
      );

      dpr = Math.min(
        window.devicePixelRatio ||
          1,
        MAX_DPR,
      );

      canvas.width =
        Math.round(
          width * dpr,
        );

      canvas.height =
        Math.round(
          height * dpr,
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
        alpha <= 0.006
      ) {
        return;
      }

      const renderSize =
        Math.max(
          1.8,
          size * 7,
        );

      context.globalAlpha =
        Math.min(
          1,
          alpha,
        );

      context.drawImage(
        sprite,
        x -
          renderSize / 2,
        y -
          renderSize / 2,
        renderSize,
        renderSize,
      );
    };

    const getInfluence = (
      progress: number,
      center: number,
      radius = 0.095,
    ) => {
      const distance =
        Math.abs(
          progress - center,
        );

      if (
        distance >= radius
      ) {
        return 0;
      }

      const value =
        1 -
        distance / radius;

      return (
        value *
        value *
        (3 -
          2 * value)
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

      const x =
        event.clientX -
        rect.left;

      const y =
        event.clientY -
        rect.top;

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
          knot.x - x;

        const dy =
          knot.y - y;

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
        timestamp * 0.001;

      const centerX =
        width * 0.48;

      const topPadding =
        height * 0.025;

      const usableHeight =
        height * 0.95;

      const amplitude =
        Math.min(
          width * 0.175,
          52,
        );

      const motion =
        time * 0.285;

      const activeMemory =
        memoriesRef.current.find(
          (memory) =>
            memory.id ===
            activeIdRef.current,
        );

      const targetBloom =
        activeMemory
          ? 1
          : 0;

      bloomAmount +=
        (targetBloom -
          bloomAmount) *
        0.085;

      if (activeMemory) {
        activeProgress +=
          (activeMemory.progress -
            activeProgress) *
          0.085;
      }

      /*
       * Ambient Oracle dust.
       */
      for (
        const dust of
        dustParticles
      ) {
        const influence =
          getInfluence(
            dust.progress,
            activeProgress,
            0.105,
          ) *
          bloomAmount;

        const y =
          topPadding +
          dust.progress *
            usableHeight +
          Math.sin(
            time * 0.26 +
              dust.phase,
          ) *
            3;

        const localWave =
          Math.sin(
            dust.progress *
              TAU *
              3.5 +
              motion,
          );

        const x =
          centerX +
          localWave *
            amplitude *
            0.5 +
          dust.side *
            dust.spread +
          dust.side *
            influence *
            14 +
          Math.cos(
            time * 0.2 +
              dust.phase,
          ) *
            2.2;

        const pulse =
          0.72 +
          Math.sin(
            time * 0.9 +
              dust.phase +
              dust.progress * 6,
          ) *
            0.28;

        drawSprite(
          cyanSprite,
          x,
          y,
          dust.size *
            pulse,
          dust.alpha *
            pulse *
            (1 +
              influence *
                0.95),
        );

        if (
          dust.size > 0.72 &&
          (Math.sin(
            dust.phase +
              time * 0.45,
          ) +
            1) /
            2 >
            0.82
        ) {
          drawSprite(
            whiteSprite,
            x,
            y,
            dust.size * 0.72,
            dust.alpha * 0.42,
          );
        }
      }

      /*
       * Bridges.
       */
      for (
        const bridge of
        bridgeParticles
      ) {
        const influence =
          getInfluence(
            bridge.progress,
            activeProgress,
          ) *
          bloomAmount;

        const phase =
          bridge.progress *
            TAU *
            3.5 +
          motion;

        const helixX =
          Math.sin(phase) *
          amplitude;

        const opening =
          influence * 34;

        const leftX =
          centerX -
          Math.abs(
            helixX,
          ) -
          opening;

        const rightX =
          centerX +
          Math.abs(
            helixX,
          ) +
          opening;

        const y =
          topPadding +
          bridge.progress *
            usableHeight;

        const x =
          leftX +
          (rightX -
            leftX) *
            bridge.offset;

        const centerWeight =
          Math.sin(
            bridge.offset *
              Math.PI,
          );

        const cavityFade =
          1 -
          influence * 0.94;

        drawSprite(
          whiteSprite,
          x,
          y,
          bridge.size,
          bridge.alpha *
            centerWeight *
            cavityFade,
        );
      }

      /*
       * Main particle strands.
       */
      for (
        const particle of
        helixParticles
      ) {
        const progress =
          particle.progress;

        const influence =
          getInfluence(
            progress,
            activeProgress,
          ) *
          bloomAmount;

        const phase =
          progress *
            TAU *
            3.5 +
          motion +
          particle.phaseNoise;

        const strandPhase =
          particle.strand ===
          0
            ? phase
            : phase +
              Math.PI;

        const wave =
          Math.sin(
            strandPhase,
          );

        const depth =
          (Math.cos(
            strandPhase,
          ) +
            1) /
          2;

        const openingDirection =
          particle.strand === 0
            ? -1
            : 1;

        const tearOffset =
          influence *
          openingDirection *
          34;

        const y =
          topPadding +
          progress *
            usableHeight +
          particle.yNoise;

        const baseX =
          centerX +
          wave *
            amplitude +
          tearOffset;

        const thickness =
          2.2 +
          depth * 7.2;

        const x =
          baseX +
          particle.clusterOffset *
            thickness +
          particle.sideNoise *
            (0.2 +
              depth * 0.5);

        const fadeTop =
          Math.min(
            1,
            progress /
              0.075,
          );

        const fadeBottom =
          Math.min(
            1,
            (1 -
              progress) /
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
          (0.15 +
            depth * 0.7) *
          (1 +
            influence *
              0.5);

        const size =
          particle.sizeNoise *
          (0.29 +
            depth * 0.86);

        drawSprite(
          particle.strand === 0
            ? cyanSprite
            : violetSprite,
          x,
          y,
          size,
          alpha,
        );
      }

      /*
       * Memory knots.
       */
      const knotPositions:
        KnotScreenPosition[] = [];

      for (
        const memory of
        memoriesRef.current
      ) {
        const progress =
          memory.progress;

        const phase =
          progress *
            TAU *
            3.5 +
          motion;

        const strandWave =
          Math.sin(phase);

        const knotX =
          centerX +
          strandWave *
            amplitude *
            0.46;

        const knotY =
          topPadding +
          progress *
            usableHeight;

        knotPositions.push({
          id: memory.id,
          x: knotX,
          y: knotY,
        });

        const hovered =
          hoveredIdRef.current ===
          memory.id;

        const selected =
          activeIdRef.current ===
          memory.id;

        const charge =
          selected
            ? 1
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
              (0.08 +
                (index %
                  5) *
                  0.006) +
            knot.phase;

          const organicRadius =
            knot.radius *
            (1 +
              Math.sin(
                time *
                  0.45 +
                  knot.phase,
              ) *
                0.06);

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
              (1 +
                charge *
                  0.22),
            knot.alpha *
              (0.56 +
                charge *
                  0.62),
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

      /*
       * Tear emission.
       */
      if (
        activeMemory &&
        bloomAmount >
          0.05
      ) {
        const tearY =
          topPadding +
          activeProgress *
            usableHeight;

        const tearOriginX =
          centerX +
          amplitude *
            0.15;

        const streamLength =
          Math.min(
            width * 0.48,
            132,
          );

        for (
          let index = 0;
          index < 34;
          index += 1
        ) {
          const seed =
            index / 34;

          const travel =
            (seed +
              time *
                0.19) %
            1;

          const envelope =
            Math.sin(
              travel *
                Math.PI,
            );

          const x =
            tearOriginX +
            travel *
              streamLength;

          const y =
            tearY +
            Math.sin(
              travel *
                Math.PI *
                3.2 +
                index *
                  1.7,
            ) *
              (10 *
                (1 -
                  travel));

          drawSprite(
            index %
              7 ===
              0
              ? whiteSprite
              : cyanSprite,
            x,
            y,
            index %
              7 ===
              0
              ? 0.72
              : 0.46,
            envelope *
              bloomAmount *
              0.48,
          );
        }
      }

      context.globalAlpha = 1;
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