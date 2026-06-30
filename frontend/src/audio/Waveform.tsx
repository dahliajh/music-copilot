import { useEffect, useRef } from 'react';

interface WaveformProps {
  analyserNode: AnalyserNode | null;
  width?: number;
  height?: number;
}

/**
 * Draws a live time-domain waveform from an AnalyserNode onto a canvas
 * using requestAnimationFrame. Purely a visualization proof-of-capture for
 * Phase 0 — no analysis happens here.
 */
export function Waveform({ analyserNode, width = 600, height = 120 }: WaveformProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (!analyserNode || !canvasRef.current) {
      return;
    }

    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const bufferLength = analyserNode.fftSize;
    const dataArray = new Uint8Array(bufferLength);

    const draw = () => {
      rafRef.current = requestAnimationFrame(draw);
      analyserNode.getByteTimeDomainData(dataArray);

      ctx.fillStyle = '#0b0f14';
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      ctx.lineWidth = 2;
      ctx.strokeStyle = '#4caf50';
      ctx.beginPath();

      const sliceWidth = canvas.width / bufferLength;
      let x = 0;
      for (let i = 0; i < bufferLength; i++) {
        const v = dataArray[i] / 128.0; // normalize around 1.0
        const y = (v * canvas.height) / 2;
        if (i === 0) {
          ctx.moveTo(x, y);
        } else {
          ctx.lineTo(x, y);
        }
        x += sliceWidth;
      }
      ctx.lineTo(canvas.width, canvas.height / 2);
      ctx.stroke();
    };

    draw();

    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [analyserNode]);

  return (
    <canvas
      ref={canvasRef}
      width={width}
      height={height}
      className="waveform-canvas"
    />
  );
}
