/** Live has no output-done event. Observe the remote player, not transcript delivery gaps. */
export function monitorVoiceAudio(stream: MediaStream) {
  if (typeof AudioContext === "undefined") return undefined;
  try {
    const context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);
    const samples = new Uint8Array(analyser.fftSize);
    void context.resume().catch(() => {});
    return {
      sample: (): boolean | undefined => {
        if (context.state !== "running") return undefined;
        analyser.getByteTimeDomainData(samples);
        const power = samples.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0);
        return Math.sqrt(power / samples.length) > 0.01;
      },
      resume: () => context.resume(),
      close: () => {
        source.disconnect();
        analyser.disconnect();
        void context.close().catch(() => {});
      },
    };
  } catch {
    return undefined;
  }
}

export class VoicePlaybackDrain {
  private heardAudio = false;
  private lastActivity: number;
  constructor(private started: number) {
    this.lastActivity = started;
  }
  caption(now: number) {
    this.lastActivity = now;
  }
  ready(now: number, audible: boolean | undefined) {
    if (audible) {
      this.heardAudio = true;
      this.lastActivity = now;
      return false;
    }
    const elapsed = now - this.started;
    const quiet = now - this.lastActivity;
    // Leave time for the requested farewell to start, then drain real playback.
    if (audible === false && this.heardAudio) return elapsed >= 4000 && quiet >= 2000;
    // No output at all, or a blocked player: release the microphone eventually.
    if (audible === false && !this.heardAudio) return elapsed >= 12000 && quiet >= 3000;
    // Older browsers cannot inspect remote audio. Use a conservative bounded fallback.
    return elapsed >= 45000 && quiet >= 5000;
  }
}
