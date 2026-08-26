import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  invoke,
} from "@tauri-apps/api/core";

import "./RightTelemetryPanel.css";

type MetricTone =
  | "cyan"
  | "violet";

type SystemStatus =
  | "stable"
  | "degraded"
  | "critical";

type DiskSnapshot = {
  id: string;
  name: string;
  mountPoint: string;
  fileSystem: string;
  kind: string;
  totalBytes: number;
  availableBytes: number;
  usedBytes: number;
  usedPercent: number;
  removable: boolean;
};

type DeviceSnapshot = {
  id: string;
  name: string;
  className: string;
  status: string;
};

type TelemetrySnapshot = {
  cpuPercent: number;
  cpuBrand: string;
  physicalCores: number;
  logicalCores: number;

  memoryPercent: number;
  memoryUsedBytes: number;
  memoryTotalBytes: number;

  temperatureC:
    | number
    | null;

  disks: DiskSnapshot[];

  devices: DeviceSnapshot[];
};

type LiveMetric = {
  id: string;
  label: string;
  value: string;
  meta: string;
  tone: MetricTone;
  points: number[];
};

const HISTORY_LENGTH = 36;

function initialHistory(
  value = 0,
) {
  return Array.from(
    {
      length:
        HISTORY_LENGTH,
    },
    () => value,
  );
}

function pushHistory(
  history: number[],
  value: number,
) {
  return [
    ...history.slice(
      -(
        HISTORY_LENGTH -
        1
      ),
    ),
    Math.max(
      0,
      Math.min(
        100,
        value,
      ),
    ),
  ];
}

function normalizeHistory(
  history: number[],
) {
  return history.map(
    (value) =>
      Math.max(
        0.04,
        Math.min(
          0.96,
          value / 100,
        ),
      ),
  );
}

function formatBytes(
  bytes: number,
) {
  const gib =
    bytes /
    1024 /
    1024 /
    1024;

  if (gib >= 1024) {
    return `${(
      gib / 1024
    ).toFixed(2)} TB`;
  }

  return `${gib.toFixed(
    gib >= 100
      ? 0
      : 1,
  )} GB`;
}

function diskLabel(
  disk: DiskSnapshot,
) {
  const raw =
    disk.mountPoint
      .replace(
        /[\\/]+$/,
        "",
      )
      .trim();

  if (
    /^[A-Za-z]:$/.test(
      raw,
    )
  ) {
    return raw.toUpperCase();
  }

  return raw || disk.name;
}

function diskName(
  disk: DiskSnapshot,
) {
  if (
    disk.removable
  ) {
    return "EXTERNAL";
  }

  const letter =
    diskLabel(
      disk,
    );

  if (
    letter === "C:"
  ) {
    return "SYSTEM";
  }

  return disk.name
    .trim()
    .replace(
      /^[A-Za-z]:$/,
      "",
    )
    .trim() ||
    "STORAGE";
}

function deviceIconType(
  className: string,
) {
  const value =
    className
      .toLowerCase();

  if (
    value.includes(
      "printer",
    )
  ) {
    return "printer";
  }

  if (
    value.includes(
      "camera",
    ) ||
    value.includes(
      "image",
    )
  ) {
    return "camera";
  }

  if (
    value.includes(
      "audio",
    ) ||
    value.includes(
      "media",
    )
  ) {
    return "audio";
  }

  if (
    value.includes(
      "bluetooth",
    )
  ) {
    return "hub";
  }

  return "network";
}

function MetricIcon({
  type,
}: {
  type: string;
}) {
  if (
    type === "CPU"
  ) {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <rect
          x="7"
          y="7"
          width="10"
          height="10"
          rx="1.8"
        />

        <rect
          x="10"
          y="10"
          width="4"
          height="4"
          rx="0.8"
        />

        <path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3" />
      </svg>
    );
  }

  if (
    type === "GPU"
  ) {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <rect
          x="4"
          y="7"
          width="15"
          height="10"
          rx="2"
        />

        <circle
          cx="10"
          cy="12"
          r="2.6"
        />

        <path d="M19 10h2M19 14h2M7 17v2M11 17v2" />
      </svg>
    );
  }

  if (
    type === "RAM"
  ) {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <rect
          x="4"
          y="8"
          width="16"
          height="8"
          rx="2"
        />

        <path d="M7 10v4M10 10v4M13 10v4M16 10v4M6 16v2M9 16v2M12 16v2M15 16v2M18 16v2" />
      </svg>
    );
  }

  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <path d="M12 5a3 3 0 0 1 3 3v5.2a4.1 4.1 0 1 1-6 0V8a3 3 0 0 1 3-3Z" />

      <path d="M12 9v6" />
    </svg>
  );
}

function DeviceIcon({
  type,
}: {
  type: string;
}) {
  if (
    type === "printer"
  ) {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path d="M7 9V4h10v5M7 17H5a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-2" />

        <rect
          x="7"
          y="14"
          width="10"
          height="6"
          rx="1"
        />
      </svg>
    );
  }

  if (
    type === "camera"
  ) {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <rect
          x="4"
          y="6"
          width="16"
          height="12"
          rx="2"
        />

        <circle
          cx="12"
          cy="12"
          r="3"
        />
      </svg>
    );
  }

  if (
    type === "audio"
  ) {
    return (
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path d="M5 10v4h4l5 4V6L9 10H5Z" />

        <path d="M17 9c1 .8 1.5 1.8 1.5 3S18 14.2 17 15" />
      </svg>
    );
  }

  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <circle
        cx="12"
        cy="12"
        r="3"
      />

      <circle
        cx="5"
        cy="6"
        r="2"
      />

      <circle
        cx="19"
        cy="6"
        r="2"
      />

      <circle
        cx="5"
        cy="18"
        r="2"
      />

      <circle
        cx="19"
        cy="18"
        r="2"
      />

      <path d="m7 7.5 3 2.7M17 7.5l-3 2.7M7 16.5l3-2.7M17 16.5l-3-2.7" />
    </svg>
  );
}

function StorageIcon({
  removable,
}: {
  removable: boolean;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      {removable ? (
        <>
          <rect
            x="7"
            y="4"
            width="10"
            height="16"
            rx="2"
          />

          <path d="M10 8h4M10 16h4" />
        </>
      ) : (
        <>
          <path d="M5 7.5 12 4l7 3.5-7 3.5-7-3.5Z" />

          <path d="m5 12 7 3.5 7-3.5M5 16.5 12 20l7-3.5" />
        </>
      )}
    </svg>
  );
}

function QronosStorageIcon() {
  return (
    <svg
      className="qrt-qronos-glyph"
      viewBox="0 0 28 28"
      aria-hidden="true"
    >
      <path
        className="qrt-qronos-glyph-shell"
        d="M14 3.5 23 8.2 14 13 5 8.2 14 3.5Z"
      />

      <path
        className="qrt-qronos-glyph-shell"
        d="M5 8.2v10.2L14 24l9-5.6V8.2"
      />

      <path
        className="qrt-qronos-glyph-axis"
        d="M14 13v11"
      />

      <circle
        className="qrt-qronos-glyph-core"
        cx="14"
        cy="13"
        r="2.6"
      />

      <path
        className="qrt-qronos-glyph-axis"
        d="M9.2 10.5 14 13l4.8-2.5"
      />
    </svg>
  );
}

function makePath(
  points: number[],
) {
  const width = 100;
  const height = 20;

  if (
    points.length < 2
  ) {
    return "M 0 10 L 100 10";
  }

  const step =
    width /
    (
      points.length -
      1
    );

  return points
    .map(
      (
        value,
        index,
      ) => {
        const normalized =
          Math.max(
            0.04,
            Math.min(
              0.96,
              value,
            ),
          );

        const x =
          index * step;

        const y =
          height -
          normalized *
            height;

        return `${
          index === 0
            ? "M"
            : "L"
        } ${x.toFixed(
          2,
        )} ${y.toFixed(
          2,
        )}`;
      },
    )
    .join(" ");
}

function MetricGraph({
  metric,
}: {
  metric: LiveMetric;
}) {
  const path =
    makePath(
      metric.points,
    );

  const gradientId =
    `qrt-real-energy-${metric.id}`;

  return (
    <svg
      className={`qrt-metric-graph qrt-metric-graph-${metric.tone}`}
      viewBox="0 0 100 20"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <defs>
        <linearGradient
          id={gradientId}
          gradientUnits="userSpaceOnUse"
          x1="-55"
          x2="-10"
        >
          <stop
            offset="0%"
            stopColor="currentColor"
            stopOpacity="0"
          />

          <stop
            offset="38%"
            stopColor="currentColor"
            stopOpacity="0.12"
          />

          <stop
            offset="50%"
            stopColor="#effeff"
            stopOpacity="1"
          />

          <stop
            offset="62%"
            stopColor="currentColor"
            stopOpacity="0.14"
          />

          <stop
            offset="100%"
            stopColor="currentColor"
            stopOpacity="0"
          />

          <animate
            attributeName="x1"
            values="-55;110"
            dur="4.5s"
            repeatCount="indefinite"
          />

          <animate
            attributeName="x2"
            values="-10;155"
            dur="4.5s"
            repeatCount="indefinite"
          />
        </linearGradient>
      </defs>

      <path
        className="qrt-metric-graph-glow"
        d={path}
      />

      <path
        className="qrt-metric-graph-line"
        d={path}
      />

      <path
        className="qrt-metric-graph-energy"
        d={path}
        stroke={`url(#${gradientId})`}
      />
    </svg>
  );
}

function SystemStatusIndicator({
  status,
}: {
  status: SystemStatus;
}) {
  const label =
    status === "stable"
      ? "STABLE"
      : status ===
          "degraded"
        ? "DEGRADED"
        : "CRITICAL";

  return (
    <div
      className={`qrt-status-preview qrt-status-${status}`}
    >
      <span className="qrt-status-emitter">
        <i className="qrt-status-core" />
        <i className="qrt-status-ring qrt-status-ring-a" />
        <i className="qrt-status-ring qrt-status-ring-b" />
      </span>

      <span className="qrt-status-label">
        {label}
      </span>
    </div>
  );
}


async function openStorage(
  disk: DiskSnapshot,
) {
  try {
    await invoke(
      "open_storage_path",
      {
        path:
          disk.mountPoint,
      },
    );
  } catch (
    error
  ) {
    console.error(
      "Qronos storage open error:",
      error,
    );
  }
}

async function openDevice(
  device: DeviceSnapshot,
) {
  try {
    await invoke(
      "open_device_properties",
      {
        instanceId:
          device.id,
      },
    );
  } catch (
    error
  ) {
    console.error(
      "Qronos device open error:",
      error,
    );
  }
}

function RealStorageRow({
  disk,
}: {
  disk: DiskSnapshot;
}) {
  const letter =
    diskLabel(
      disk,
    );

  const name =
    diskName(
      disk,
    );

  return (
    <article
      className="qrt-storage-row"
      role="button"
      tabIndex={0}
      onClick={() => {
        void openStorage(
          disk,
        );
      }}
      onKeyDown={(
        event,
      ) => {
        if (
          event.key ===
            "Enter" ||
          event.key ===
            " "
        ) {
          event.preventDefault();

          void openStorage(
            disk,
          );
        }
      }}
    >
      <div className="qrt-storage-icon">
        <StorageIcon
          removable={
            disk.removable
          }
        />

        <span className="qrt-icon-energy-pulse" />
      </div>

      <div className="qrt-storage-copy">
        <div className="qrt-storage-drive-title">
          <strong className="qrt-storage-drive-letter">
            {
              letter.replace(
                ":",
                "",
              )
            }
          </strong>

          {letter.endsWith(
            ":",
          ) && (
            <strong className="qrt-storage-drive-colon">
              :
            </strong>
          )}

          <span className="qrt-storage-drive-name">
            {name}
          </span>
        </div>

        <span className="qrt-storage-meta">
          {disk.kind}
          {disk.fileSystem
            ? ` • ${disk.fileSystem}`
            : ""}
        </span>
      </div>

      <div className="qrt-storage-meter">
        <span className="qrt-storage-detail">
          {formatBytes(
            disk.availableBytes,
          )}{" "}
          FREE
        </span>

        <div className="qrt-storage-track">
          <i
            style={{
              width:
                `${disk.usedPercent}%`,
            }}
          />
        </div>
      </div>

      <span className="qrt-storage-percent">
        {Math.round(
          disk.usedPercent,
        )}
        %
      </span>
    </article>
  );
}

function RightTelemetryPanel() {
  const [
    snapshot,
    setSnapshot,
  ] =
    useState<
      TelemetrySnapshot | null
    >(null);

  const [
    cpuHistory,
    setCpuHistory,
  ] =
    useState<number[]>(
      () =>
        initialHistory(),
    );

  const [
    ramHistory,
    setRamHistory,
  ] =
    useState<number[]>(
      () =>
        initialHistory(),
    );

  const [
    telemetryError,
    setTelemetryError,
  ] =
    useState(false);

  const deviceScrollRef =
    useRef<HTMLDivElement | null>(
      null,
    );

  useEffect(() => {
    let disposed =
      false;

    let busy = false;

    const refresh =
      async () => {
        if (busy) {
          return;
        }

        busy = true;

        try {
          const next =
            await invoke<TelemetrySnapshot>(
              "get_system_snapshot",
            );

          if (
            disposed
          ) {
            return;
          }

          setSnapshot(
            next,
          );

          setTelemetryError(
            false,
          );

          setCpuHistory(
            (current) =>
              pushHistory(
                current,
                next.cpuPercent,
              ),
          );

          setRamHistory(
            (current) =>
              pushHistory(
                current,
                next.memoryPercent,
              ),
          );
        } catch (
          error
        ) {
          console.error(
            "Qronos telemetry error:",
            error,
          );

          if (
            !disposed
          ) {
            setTelemetryError(
              true,
            );
          }
        } finally {
          busy = false;
        }
      };

    void refresh();

    const timer =
      window.setInterval(
        () => {
          void refresh();
        },
        1000,
      );

    return () => {
      disposed = true;

      window.clearInterval(
        timer,
      );
    };
  }, []);

  useEffect(() => {
    const element =
      deviceScrollRef.current;

    if (!element) {
      return;
    }

    const handleWheel = (
      event: WheelEvent,
    ) => {
      if (
        element.scrollWidth <=
        element.clientWidth
      ) {
        return;
      }

      const movement =
        Math.abs(
          event.deltaY,
        ) >
        Math.abs(
          event.deltaX,
        )
          ? event.deltaY
          : event.deltaX;

      if (
        Math.abs(
          movement,
        ) < 0.5
      ) {
        return;
      }

      event.preventDefault();

      element.scrollLeft +=
        movement *
        1.15;
    };

    element.addEventListener(
      "wheel",
      handleWheel,
      {
        passive: false,
      },
    );

    return () => {
      element.removeEventListener(
        "wheel",
        handleWheel,
      );
    };
  }, []);

  const metrics =
    useMemo<
      LiveMetric[]
    >(
      () => {
        const cpu =
          snapshot
            ?.cpuPercent ??
          0;

        const ram =
          snapshot
            ?.memoryPercent ??
          0;

        const temp =
          snapshot
            ?.temperatureC ??
          null;

        return [
          {
            id: "cpu",
            label:
              "CPU",
            value:
              snapshot
                ? `${Math.round(
                    cpu,
                  )}%`
                : "--",
            meta:
              snapshot
                ? `${snapshot.physicalCores}C / ${snapshot.logicalCores}T`
                : "LIVE",
            tone:
              "cyan",
            points:
              normalizeHistory(
                cpuHistory,
              ),
          },

          {
            id: "gpu",
            label:
              "GPU",
            value:
              "--",
            meta:
              "NATIVE NEXT",
            tone:
              "cyan",
            points:
              normalizeHistory(
                initialHistory(
                  0,
                ),
              ),
          },

          {
            id: "ram",
            label:
              "RAM",
            value:
              snapshot
                ? `${Math.round(
                    ram,
                  )}%`
                : "--",
            meta:
              snapshot
                ? `${formatBytes(
                    snapshot.memoryUsedBytes,
                  )} / ${formatBytes(
                    snapshot.memoryTotalBytes,
                  )}`
                : "LIVE",
            tone:
              "cyan",
            points:
              normalizeHistory(
                ramHistory,
              ),
          },

          {
            id: "temp",
            label:
              "TEMP",
            value:
              temp !== null
                ? `${Math.round(
                    temp,
                  )}°C`
                : "--",
            meta:
              temp !== null
                ? "LIVE"
                : "UNAVAILABLE",
            tone:
              "violet",
            points:
              normalizeHistory(
                initialHistory(
                  temp ??
                    0,
                ),
              ),
          },
        ];
      },
      [
        snapshot,
        cpuHistory,
        ramHistory,
      ],
    );

  const status:
    SystemStatus =
      telemetryError
        ? "critical"
        : snapshot
          ? (
              snapshot.cpuPercent >
                92 ||
              snapshot.memoryPercent >
                94
            )
            ? "critical"
            : (
                snapshot.cpuPercent >
                  78 ||
                snapshot.memoryPercent >
                  85
              )
              ? "degraded"
              : "stable"
          : "degraded";

  const realDisks =
    snapshot?.disks ??
    [];

  const devices =
    snapshot?.devices ??
    [];

  return (
    <aside
      className="qrt-panel"
      dir="ltr"
      aria-label="Qronos telemetry"
    >
      <section className="qrt-system-block">
        <header className="qrt-heading qrt-system-heading">
          <span>
            SYSTEM
          </span>

          <div className="qrt-heading-dots">
            <i />
            <i />
            <i />
          </div>
        </header>

        <div className="qrt-system-grid">
          {metrics.map(
            (
              metric,
            ) => (
              <article
                key={
                  metric.id
                }
                className={`qrt-metric qrt-metric-${metric.tone}`}
              >
                <div className="qrt-metric-icon">
                  <MetricIcon
                    type={
                      metric.label
                    }
                  />

                  <span className="qrt-icon-energy-pulse" />
                </div>

                <span className="qrt-metric-label">
                  {
                    metric.label
                  }
                </span>

                <div className="qrt-metric-wave">
                  <MetricGraph
                    metric={
                      metric
                    }
                  />
                </div>

                <div className="qrt-metric-reading">
                  <strong>
                    {
                      metric.value
                    }
                  </strong>

                  <span>
                    {
                      metric.meta
                    }
                  </span>
                </div>
              </article>
            ),
          )}
        </div>

        <div className="qrt-status-slot">
          <SystemStatusIndicator
            status={
              status
            }
          />
        </div>
      </section>

      <section className="qrt-storage-block">
        <header className="qrt-heading">
          <span>
            STORAGE
          </span>

          <small>
            LIVE
          </small>
        </header>

        <div className="qrt-qronos-fixed">
          <article className="qrt-storage-row qrt-storage-row-pinned">
            <div className="qrt-storage-icon qrt-qronos-storage-icon">
              <QronosStorageIcon />

              <span className="qrt-qronos-orbit qrt-qronos-orbit-a" />
              <span className="qrt-qronos-orbit qrt-qronos-orbit-b" />
              <span className="qrt-qronos-orbit qrt-qronos-orbit-c" />
              <span className="qrt-qronos-orbit qrt-qronos-orbit-d" />
              <span className="qrt-qronos-orbit qrt-qronos-orbit-e" />
            </div>

            <div className="qrt-storage-copy">
              <span className="qrt-storage-name">
                QRONOS STORAGE
              </span>

              <span className="qrt-storage-meta">
                LOCAL SYSTEM
              </span>
            </div>

            <div className="qrt-storage-meter">
              <span className="qrt-storage-detail">
                LIVE INDEX
              </span>

              <div className="qrt-storage-track">
                <i
                  style={{
                    width:
                      snapshot
                        ? "100%"
                        : "0%",
                  }}
                />
              </div>
            </div>

            <span className="qrt-storage-percent">
              {snapshot
                ? "LIVE"
                : "--"}
            </span>
          </article>
        </div>

        <div className="qrt-drive-scroll">
          {realDisks.map(
            (
              disk,
            ) => (
              <RealStorageRow
                key={
                  disk.id
                }
                disk={
                  disk
                }
              />
            ),
          )}
        </div>
      </section>

      <section className="qrt-devices-block">
        <header className="qrt-heading">
          <span>
            DEVICES
          </span>

          <small>
            {
              devices.length
            }{" "}
            CONNECTED
          </small>
        </header>

        <div
          ref={
            deviceScrollRef
          }
          className="qrt-device-scroll"
        >
          {devices.map(
            (
              device,
            ) => (
              <article
                key={
                  device.id
                }
                className="qrt-device"
                role="button"
                tabIndex={0}
                onClick={() => {
                  void openDevice(
                    device,
                  );
                }}
                onKeyDown={(
                  event,
                ) => {
                  if (
                    event.key ===
                      "Enter" ||
                    event.key ===
                      " "
                  ) {
                    event.preventDefault();

                    void openDevice(
                      device,
                    );
                  }
                }}
              >
                <div className="qrt-device-icon">
                  <DeviceIcon
                    type={
                      deviceIconType(
                        device.className,
                      )
                    }
                  />

                  <span className="qrt-device-state qrt-device-state-online" />

                  <span className="qrt-icon-energy-pulse" />
                </div>

                <div className="qrt-device-copy">
                  <span
                    className="qrt-device-name"
                    title={
                      device.name
                    }
                  >
                    {
                      device.name
                    }
                  </span>

                  <span className="qrt-device-type">
                    {
                      device.className
                    }
                  </span>
                </div>
              </article>
            ),
          )}
        </div>
      </section>
    </aside>
  );
}

export default RightTelemetryPanel;