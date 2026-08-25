import {
  useEffect,
  useRef,
} from "react";

type SystemMetric = {
  label: string;
  value: string;
  detail: string;
};

type OrbSystemRendererProps = {
  active: boolean;
  metrics: SystemMetric[];
};

type Point = {
  x: number;
  y: number;
};

type Particle = {
  sourceX: number;
  sourceY: number;
  targetX: number;
  targetY: number;
  phase: number;
  size: number;
  brightness: number;
};

type GlyphMap = Record<
  string,
  number[][]
>;

const MAX_DPR = 1.5;

const GLYPHS: GlyphMap = {
  "0": [
    [1, 1, 1],
    [1, 0, 1],
    [1, 0, 1],
    [1, 0, 1],
    [1, 1, 1],
  ],

  "1": [
    [0, 1, 0],
    [1, 1, 0],
    [0, 1, 0],
    [0, 1, 0],
    [1, 1, 1],
  ],

  "2": [
    [1, 1, 1],
    [0, 0, 1],
    [1, 1, 1],
    [1, 0, 0],
    [1, 1, 1],
  ],

  "3": [
    [1, 1, 1],
    [0, 0, 1],
    [1, 1, 1],
    [0, 0, 1],
    [1, 1, 1],
  ],

  "4": [
    [1, 0, 1],
    [1, 0, 1],
    [1, 1, 1],
    [0, 0, 1],
    [0, 0, 1],
  ],

  "5": [
    [1, 1, 1],
    [1, 0, 0],
    [1, 1, 1],
    [0, 0, 1],
    [1, 1, 1],
  ],

  "6": [
    [1, 1, 1],
    [1, 0, 0],
    [1, 1, 1],
    [1, 0, 1],
    [1, 1, 1],
  ],

  "7": [
    [1, 1, 1],
    [0, 0, 1],
    [0, 1, 0],
    [0, 1, 0],
    [0, 1, 0],
  ],

  "8": [
    [1, 1, 1],
    [1, 0, 1],
    [1, 1, 1],
    [1, 0, 1],
    [1, 1, 1],
  ],

  "9": [
    [1, 1, 1],
    [1, 0, 1],
    [1, 1, 1],
    [0, 0, 1],
    [1, 1, 1],
  ],

  C: [
    [1, 1, 1],
    [1, 0, 0],
    [1, 0, 0],
    [1, 0, 0],
    [1, 1, 1],
  ],

  P: [
    [1, 1, 0],
    [1, 0, 1],
    [1, 1, 0],
    [1, 0, 0],
    [1, 0, 0],
  ],

  U: [
    [1, 0, 1],
    [1, 0, 1],
    [1, 0, 1],
    [1, 0, 1],
    [1, 1, 1],
  ],

  G: [
    [1, 1, 1],
    [1, 0, 0],
    [1, 0, 1],
    [1, 0, 1],
    [1, 1, 1],
  ],

  R: [
    [1, 1, 0],
    [1, 0, 1],
    [1, 1, 0],
    [1, 1, 0],
    [1, 0, 1],
  ],

  A: [
    [0, 1, 0],
    [1, 0, 1],
    [1, 1, 1],
    [1, 0, 1],
    [1, 0, 1],
  ],

  M: [
    [1, 0, 1],
    [1, 1, 1],
    [1, 1, 1],
    [1, 0, 1],
    [1, 0, 1],
  ],

  T: [
    [1, 1, 1],
    [0, 1, 0],
    [0, 1, 0],
    [0, 1, 0],
    [0, 1, 0],
  ],

  E: [
    [1, 1, 1],
    [1, 0, 0],
    [1, 1, 0],
    [1, 0, 0],
    [1, 1, 1],
  ],
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

function smoothstep(
  value: number,
) {
  const t =
    clamp01(value);

  return (
    t *
    t *
    (
      3 -
      2 * t
    )
  );
}

function OrbSystemRenderer({
  active,
  metrics,
}: OrbSystemRendererProps) {
  const canvasRef =
    useRef<HTMLCanvasElement | null>(
      null,
    );

  const activeRef =
    useRef(active);

  useEffect(() => {
    activeRef.current = active;
  }, [active]);

  useEffect(() => {
    const canvas =
      canvasRef.current;

    if (!canvas) {
      return;
    }

    const ctx =
      canvas.getContext(
        "2d",
        {
          alpha: true,
          desynchronized: true,
        },
      );

    if (!ctx) {
      return;
    }

    let width = 460;
    let height = 460;
    let scale = 1;
    let dpr = 1;

    const particles:
      Particle[] = [];

    const connectorPoints:
      Particle[] = [];

    let morph = 0;
    let animationFrame = 0;
    let previousTimestamp = 0;
    let phase = 0;

    const createTextPoints = (
      text: string,
      x: number,
      y: number,
      dotSpacing: number,
      letterSpacing: number,
    ) => {
      const points: Point[] =
        [];

      let cursorX = x;

      for (
        const rawCharacter of text
      ) {
        const character =
          rawCharacter.toUpperCase();

        if (
          character === " "
        ) {
          cursorX +=
            dotSpacing * 3;

          continue;
        }

        if (
          character === "%"
        ) {
          points.push({
            x: cursorX,
            y,
          });

          points.push({
            x:
              cursorX +
              dotSpacing * 2,
            y:
              y +
              dotSpacing * 4,
          });

          for (
            let index = 0;
            index < 5;
            index += 1
          ) {
            points.push({
              x:
                cursorX +
                dotSpacing *
                  0.42 *
                  index,

              y:
                y +
                dotSpacing *
                  (
                    3.6 -
                    index * 0.82
                  ),
            });
          }

          cursorX +=
            dotSpacing * 3.7 +
            letterSpacing;

          continue;
        }

        if (
          character === "°"
        ) {
          points.push({
            x: cursorX,
            y,
          });

          points.push({
            x:
              cursorX +
              dotSpacing,
            y,
          });

          points.push({
            x: cursorX,
            y:
              y +
              dotSpacing,
          });

          points.push({
            x:
              cursorX +
              dotSpacing,
            y:
              y +
              dotSpacing,
          });

          cursorX +=
            dotSpacing * 2.2 +
            letterSpacing;

          continue;
        }

        const glyph =
          GLYPHS[character];

        if (!glyph) {
          cursorX +=
            dotSpacing * 3 +
            letterSpacing;

          continue;
        }

        for (
          let row = 0;
          row < glyph.length;
          row += 1
        ) {
          for (
            let column = 0;
            column <
            glyph[row].length;
            column += 1
          ) {
            if (
              glyph[row][column] !==
              1
            ) {
              continue;
            }

            points.push({
              x:
                cursorX +
                column *
                  dotSpacing,

              y:
                y +
                row *
                  dotSpacing,
            });
          }
        }

        cursorX +=
          dotSpacing * 3 +
          letterSpacing;
      }

      return points;
    };

    const textWidth = (
      text: string,
      dotSpacing: number,
      letterSpacing: number,
    ) => {
      let total = 0;

      for (
        const character of text
      ) {
        if (
          character === " "
        ) {
          total +=
            dotSpacing * 3;

          continue;
        }

        if (
          character === "%"
        ) {
          total +=
            dotSpacing * 3.7 +
            letterSpacing;

          continue;
        }

        if (
          character === "°"
        ) {
          total +=
            dotSpacing * 2.2 +
            letterSpacing;

          continue;
        }

        total +=
          dotSpacing * 3 +
          letterSpacing;
      }

      return Math.max(
        0,
        total - letterSpacing,
      );
    };

    const pushMorphPoints = (
      targets: Point[],
      sourceRadius: number,
      seedOffset: number,
    ) => {
      for (
        let index = 0;
        index < targets.length;
        index += 1
      ) {
        const target =
          targets[index];

        const seed =
          index +
          seedOffset;

        const angle =
          (
            seed /
            Math.max(
              1,
              targets.length,
            )
          ) *
            Math.PI *
            2 +
          Math.sin(
            seed * 13.731,
          ) *
            0.55;

        const radius =
          sourceRadius +
          (
            (
              Math.sin(
                seed * 29.173,
              ) +
              1
            ) /
            2
          ) *
            18;

        particles.push({
          sourceX:
            230 +
            Math.cos(angle) *
              radius,

          sourceY:
            230 +
            Math.sin(angle) *
              radius *
              0.94,

          targetX:
            target.x,

          targetY:
            target.y,

          phase:
            (
              (
                Math.sin(
                  seed * 41.713,
                ) +
                1
              ) /
              2
            ) *
            Math.PI *
            2,

          size:
            0.72 +
            (
              (
                Math.cos(
                  seed * 17.31,
                ) +
                1
              ) /
              2
            ) *
              0.42,

          brightness:
            0.68 +
            (
              (
                Math.sin(
                  seed * 23.71,
                ) +
                1
              ) /
              2
            ) *
              0.32,
        });
      }
    };

    const pushConnector = (
      x1: number,
      y1: number,
      x2: number,
      y2: number,
      count: number,
      seedOffset: number,
    ) => {
      for (
        let index = 0;
        index < count;
        index += 1
      ) {
        const t =
          count <= 1
            ? 0
            : index /
              (
                count - 1
              );

        const targetX =
          x1 +
          (
            x2 - x1
          ) *
            t;

        const targetY =
          y1 +
          (
            y2 - y1
          ) *
            t;

        const angle =
          Math.atan2(
            y1 - 230,
            x1 - 230,
          ) +
          (
            index -
            count / 2
          ) *
            0.01;

        connectorPoints.push({
          sourceX:
            230 +
            Math.cos(angle) *
              136,

          sourceY:
            230 +
            Math.sin(angle) *
              136,

          targetX,
          targetY,

          phase:
            index *
              0.55 +
            seedOffset,

          size:
            0.72,

          brightness:
            0.72,
        });
      }
    };

    const rebuildGeometry = () => {
      particles.length = 0;
      connectorPoints.length = 0;

      const layout = [
        {
          label:
            metrics[0]?.label ??
            "CPU",
          value:
            metrics[0]?.value ??
            "18%",
          detail:
            metrics[0]?.detail ??
            "",
          x: 42,
          y: 116,
          connector: {
            x1: 115,
            y1: 176,
            x2: 80,
            y2: 153,
          },
        },
        {
          label:
            metrics[1]?.label ??
            "GPU",
          value:
            metrics[1]?.value ??
            "07%",
          detail:
            metrics[1]?.detail ??
            "",
          x: 326,
          y: 116,
          connector: {
            x1: 345,
            y1: 176,
            x2: 380,
            y2: 153,
          },
        },
        {
          label:
            metrics[2]?.label ??
            "RAM",
          value:
            metrics[2]?.value ??
            "41%",
          detail:
            metrics[2]?.detail ??
            "",
          x: 42,
          y: 310,
          connector: {
            x1: 115,
            y1: 284,
            x2: 80,
            y2: 307,
          },
        },
        {
          label:
            metrics[3]?.label ??
            "TEMP",
          value:
            metrics[3]?.value ??
            "47°",
          detail:
            metrics[3]?.detail ??
            "",
          x: 316,
          y: 310,
          connector: {
            x1: 345,
            y1: 284,
            x2: 380,
            y2: 307,
          },
        },
      ];

      layout.forEach(
        (
          item,
          itemIndex,
        ) => {
          const labelSpacing =
            2.55;

          const labelLetterSpacing =
            2.1;

          const valueSpacing =
            4.25;

          const valueLetterSpacing =
            3.1;

          const labelWidth =
            textWidth(
              item.label,
              labelSpacing,
              labelLetterSpacing,
            );

          const valueWidth =
            textWidth(
              item.value,
              valueSpacing,
              valueLetterSpacing,
            );

          const labelPoints =
            createTextPoints(
              item.label,
              item.x,
              item.y,
              labelSpacing,
              labelLetterSpacing,
            );

          const valuePoints =
            createTextPoints(
              item.value,
              item.x +
                (
                  labelWidth -
                  valueWidth
                ) /
                  2,
              item.y + 23,
              valueSpacing,
              valueLetterSpacing,
            );

          pushMorphPoints(
            labelPoints,
            128,
            100 +
              itemIndex * 300,
          );

          pushMorphPoints(
            valuePoints,
            134,
            200 +
              itemIndex * 400,
          );

          pushConnector(
            item.connector.x1,
            item.connector.y1,
            item.connector.x2,
            item.connector.y2,
            12,
            itemIndex * 3,
          );
        },
      );

      const systemLabel =
        createTextPoints(
          "SYSTEM",
          184,
          54,
          2.45,
          2.2,
        );

      pushMorphPoints(
        systemLabel,
        132,
        2500,
      );
    };

    const resize = () => {
      const parent =
        canvas.parentElement;

      if (!parent) {
        return;
      }

      const rect =
        parent.getBoundingClientRect();

      width =
        Math.max(
          40,
          rect.width,
        );

      height =
        Math.max(
          40,
          rect.height,
        );

      scale =
        Math.min(
          width,
          height,
        ) /
        460;

      dpr =
        Math.min(
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

      ctx.setTransform(
        dpr,
        0,
        0,
        dpr,
        0,
        0,
      );
    };

    rebuildGeometry();
    resize();

    const resizeObserver =
      new ResizeObserver(() => {
        resize();
      });

    if (
      canvas.parentElement
    ) {
      resizeObserver.observe(
        canvas.parentElement,
      );
    }

    const drawParticle = (
      x: number,
      y: number,
      radius: number,
      alpha: number,
      hot = false,
    ) => {
      if (
        alpha <= 0.002
      ) {
        return;
      }

      ctx.beginPath();

      ctx.fillStyle =
        hot
          ? `rgba(211,252,255,${alpha})`
          : `rgba(83,220,255,${alpha})`;

      ctx.arc(
        x,
        y,
        radius,
        0,
        Math.PI * 2,
      );

      ctx.fill();

      if (
        hot &&
        alpha > 0.28
      ) {
        ctx.beginPath();

        ctx.fillStyle =
          `rgba(73,205,255,${
            alpha * 0.08
          })`;

        ctx.arc(
          x,
          y,
          radius * 4.2,
          0,
          Math.PI * 2,
        );

        ctx.fill();
      }
    };

    const drawParticleSet = (
      set: Particle[],
      localMorph: number,
      timePhase: number,
      connector = false,
    ) => {
      const eased =
        smoothstep(
          localMorph,
        );

      for (
        let index = 0;
        index < set.length;
        index += 1
      ) {
        const particle =
          set[index];

        const curve =
          Math.sin(
            eased *
              Math.PI,
          );

        const swirl =
          particle.phase +
          timePhase *
            (
              connector
                ? 0.55
                : 0.34
            );

        const x =
          particle.sourceX +
          (
            particle.targetX -
            particle.sourceX
          ) *
            eased +
          Math.cos(swirl) *
            curve *
            (
              connector
                ? 2.8
                : 6.5
            );

        const y =
          particle.sourceY +
          (
            particle.targetY -
            particle.sourceY
          ) *
            eased +
          Math.sin(swirl) *
            curve *
            (
              connector
                ? 2
                : 4.2
            );

        const pulse =
          0.86 +
          (
            (
              Math.sin(
                timePhase *
                  1.7 +
                  particle.phase,
              ) +
              1
            ) /
            2
          ) *
            0.14;

        const alpha =
          (
            0.1 +
            eased * 0.8
          ) *
          particle.brightness *
          pulse;

        const radius =
          particle.size *
          (
            0.72 +
            eased * 0.5
          );

        drawParticle(
          x,
          y,
          radius,
          alpha,
          !connector &&
            eased > 0.86 &&
            index % 11 === 0,
        );
      }
    };

    const render = (
      timestamp: number,
    ) => {
      const delta =
        previousTimestamp === 0
          ? 1 / 60
          : Math.max(
              0,
              Math.min(
                0.033,
                (
                  timestamp -
                  previousTimestamp
                ) /
                  1000,
              ),
            );

      previousTimestamp =
        timestamp;

      phase +=
        delta * 1.25;

      const targetMorph =
        activeRef.current
          ? 1
          : 0;

      const response =
        1 -
        Math.exp(
          -delta *
            (
              activeRef.current
                ? 3.2
                : 4.4
            ),
        );

      morph +=
        (
          targetMorph -
          morph
        ) *
        response;

      ctx.clearRect(
        0,
        0,
        width,
        height,
      );

      if (
        morph < 0.002
      ) {
        animationFrame =
          requestAnimationFrame(
            render,
          );

        return;
      }

      ctx.save();

      ctx.translate(
        width / 2 -
          230 * scale,
        height / 2 -
          230 * scale,
      );

      ctx.scale(
        scale,
        scale,
      );

      drawParticleSet(
        connectorPoints,
        morph,
        phase,
        true,
      );

      drawParticleSet(
        particles,
        morph,
        phase,
        false,
      );

      if (
        morph > 0.66
      ) {
        const detailAlpha =
          smoothstep(
            (
              morph -
              0.66
            ) /
              0.34,
          );

        const details = [
          {
            text:
              metrics[0]?.detail ??
              "",
            x: 42,
            y: 171,
            align:
              "left" as const,
          },
          {
            text:
              metrics[1]?.detail ??
              "",
            x: 418,
            y: 171,
            align:
              "right" as const,
          },
          {
            text:
              metrics[2]?.detail ??
              "",
            x: 42,
            y: 365,
            align:
              "left" as const,
          },
          {
            text:
              metrics[3]?.detail ??
              "",
            x: 418,
            y: 365,
            align:
              "right" as const,
          },
        ];

        ctx.font =
          "7px Bahnschrift, Segoe UI, sans-serif";

        ctx.textBaseline =
          "middle";

        for (
          const detail of details
        ) {
          ctx.textAlign =
            detail.align;

          ctx.fillStyle =
            `rgba(159,205,224,${
              detailAlpha *
              0.58
            })`;

          ctx.fillText(
            detail.text,
            detail.x,
            detail.y,
          );
        }

        ctx.textAlign =
          "center";

        ctx.fillStyle =
          `rgba(105,232,177,${
            detailAlpha *
            0.7
          })`;

        ctx.font =
          "7px Bahnschrift, Segoe UI, sans-serif";

        ctx.fillText(
          "●   SYSTEM STABLE",
          230,
          403,
        );
      }

      ctx.restore();

      animationFrame =
        requestAnimationFrame(
          render,
        );
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
    };
  }, [metrics]);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      style={{
        position: "absolute",
        inset: 0,
        zIndex: 7,
        pointerEvents: "none",
      }}
    />
  );
}

export default OrbSystemRenderer;