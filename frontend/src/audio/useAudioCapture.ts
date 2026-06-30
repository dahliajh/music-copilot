import { useCallback, useRef, useState } from 'react';

export type CaptureStatus = 'idle' | 'requesting' | 'recording' | 'stopped' | 'error';

export interface UseAudioCaptureResult {
  status: CaptureStatus;
  errorMessage: string | null;
  /** Object URL for the most recently completed recording, if any. */
  recordingUrl: string | null;
  /** Live AnalyserNode, available only while `status === 'recording'`.
   *  Exposed so a waveform renderer can pull frequency/time-domain data
   *  without this hook needing to know anything about canvas drawing. */
  analyserNode: AnalyserNode | null;
  start: () => Promise<void>;
  stop: () => void;
}

/**
 * Encapsulates mic capture: getUserMedia -> MediaRecorder (for the
 * eventual upload-for-analysis path) + an AnalyserNode tap (for the live
 * waveform). Deliberately exposes only start/stop and the analyser node so
 * this can later be swapped for a streaming/AudioWorklet-based capture
 * module (see `audio-input` in music-copilot-mvp-plan.md) without changing
 * how callers consume it.
 */
export function useAudioCapture(): UseAudioCaptureResult {
  const [status, setStatus] = useState<CaptureStatus>('idle');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [recordingUrl, setRecordingUrl] = useState<string | null>(null);
  const [analyserNode, setAnalyserNode] = useState<AnalyserNode | null>(null);

  const streamRef = useRef<MediaStream | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const cleanup = useCallback(() => {
    mediaRecorderRef.current = null;
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track: MediaStreamTrack) => track.stop());
      streamRef.current = null;
    }
    if (audioContextRef.current) {
      audioContextRef.current.close().catch(() => {
        /* ignore close errors on teardown */
      });
      audioContextRef.current = null;
    }
    setAnalyserNode(null);
  }, []);

  const start = useCallback(async () => {
    setErrorMessage(null);
    setRecordingUrl(null);
    setStatus('requesting');

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      // --- live waveform tap ---
      const AudioContextCtor =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext;
      const audioContext = new AudioContextCtor();
      audioContextRef.current = audioContext;

      const source = audioContext.createMediaStreamSource(stream);
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 2048;
      source.connect(analyser);
      setAnalyserNode(analyser);

      // --- recording for later upload/analysis ---
      chunksRef.current = [];
      const mimeType = MediaRecorder.isTypeSupported('audio/webm')
        ? 'audio/webm'
        : '';
      const mediaRecorder = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);
      mediaRecorderRef.current = mediaRecorder;

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunksRef.current.push(event.data);
        }
      };
      mediaRecorder.onstop = () => {
        const blob = new Blob(chunksRef.current, {
          type: mediaRecorder.mimeType || 'audio/webm',
        });
        setRecordingUrl(URL.createObjectURL(blob));
        cleanup();
        setStatus('stopped');
      };

      mediaRecorder.start();
      setStatus('recording');
    } catch (err) {
      console.error('Mic capture failed', err);
      setErrorMessage(err instanceof Error ? err.message : String(err));
      cleanup();
      setStatus('error');
    }
  }, [cleanup]);

  const stop = useCallback(() => {
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
      mediaRecorderRef.current.stop();
    } else {
      cleanup();
      setStatus('idle');
    }
  }, [cleanup]);

  return { status, errorMessage, recordingUrl, analyserNode, start, stop };
}
