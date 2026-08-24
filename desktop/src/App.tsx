import { useState } from "react";
import "./App.css";

import QronosOrb from "./components/QronosOrb";
import OrbTaskRenderer from "./components/OrbTaskRenderer";
import type { OrbState } from "./components/OrbState";

function App() {
  const [orbState, setOrbState] =
    useState<OrbState>("idle");

  return (
    <main className="app">
      <div className="ambient ambient-cyan" />
      <div className="ambient ambient-violet" />

      <header className="brand">
        <div className="brand-name">
          Q R O N O S
        </div>

        <div className="brand-beam">
          <div className="brand-beam-base" />
          <div className="brand-beam-runner" />
        </div>
      </header>

      <section className="core-zone">
        <div className="orb-shell">
          <div className="orb-ring orb-ring-1" />
          <div className="orb-ring orb-ring-2" />

          <QronosOrb
            size={460}
            state={orbState}
          />

          <OrbTaskRenderer
            state={orbState}
          />
        </div>
      </section>

      <div className="orb-debug-controls">
        <button
          type="button"
          onClick={() =>
            setOrbState("idle")
          }
        >
          Idle
        </button>

        <button
          type="button"
          onClick={() =>
            setOrbState("listening")
          }
        >
          Listening
        </button>

        <button
          type="button"
          onClick={() =>
            setOrbState("thinking")
          }
        >
          Thinking
        </button>

        <button
          type="button"
          onClick={() =>
            setOrbState("responding")
          }
        >
          Responding
        </button>
      </div>
    </main>
  );
}

export default App;