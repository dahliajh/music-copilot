import { useAudioCapture } from './useAudioCapture';
import { Waveform } from './Waveform';

/**
 * Self-contained mic capture + live waveform component. Intentionally
 * decoupled from the score view: it knows nothing about OSMD or
 * assessment data, and exposes only "record a take, show me it captured
 * something." The hook underneath (`useAudioCapture`) is structured so the
 * MediaRecorder-based "record fully, then analyze" path used here can be
 * swapped for a streaming/AudioWorklet pipeline later without touching
 * this component's shape much (see `audio-input` module in the project plan).
 */
export function AudioCapture() {
  const { status, errorMessage, recordingUrl, analyserNode, start, stop } =
    useAudioCapture();

  const isRecording = status === 'recording';

  return (
    <div className="audio-capture">
      <div className="audio-capture__controls">
        <button
          type="button"
          onClick={isRecording ? stop : start}
          disabled={status === 'requesting'}
          className={
            isRecording ? 'audio-capture__button audio-capture__button--stop' : 'audio-capture__button'
          }
        >
          {status === 'requesting' && 'Requesting mic…'}
          {status === 'idle' && 'Record'}
          {status === 'recording' && 'Stop'}
          {status === 'stopped' && 'Record again'}
          {status === 'error' && 'Retry'}
        </button>
        <span className="audio-capture__status">Status: {status}</span>
      </div>

      {errorMessage && (
        <p className="audio-capture__error">
          Mic error: {errorMessage}. (Mic access requires HTTPS or localhost,
          and browser permission.)
        </p>
      )}

      <Waveform analyserNode={analyserNode} />

      {recordingUrl && (
        <div className="audio-capture__playback">
          <p>Last take (no analysis yet — playback only):</p>
          <audio controls src={recordingUrl} />
        </div>
      )}
    </div>
  );
}
