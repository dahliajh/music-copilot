import { AudioCapture } from './audio/AudioCapture';
import { ScoreView } from './score/ScoreView';
import './App.css';

/**
 * Phase 0 spike entry point. Wires together:
 *  - ScoreView: renders sample-bass-excerpt.musicxml via OSMD and applies
 *    fake mistake highlights from a hardcoded mock assessment result.
 *  - AudioCapture: mic recording + live waveform, fully independent of the
 *    score (no wiring between recording and assessment yet — that's the
 *    real pipeline future phases build).
 */
function App() {
  return (
    <div className="app">
      <header className="app__header">
        <h1>Music Copilot — Phase 0 Spike</h1>
        <p className="app__subtitle">
          Proving OSMD note-highlighting end-to-end with fake mistake data,
          plus basic mic capture. No real audio analysis yet.
        </p>
      </header>

      <main className="app__main">
        <section className="app__section">
          <h2>Score (with fake highlights)</h2>
          <ScoreView />
        </section>

        <section className="app__section">
          <h2>Mic capture</h2>
          <AudioCapture />
        </section>
      </main>
    </div>
  );
}

export default App;
