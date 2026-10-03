"use client";

import { useMemo, useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { formatDuration } from "@/lib/format";
import type { CallRecording } from "@/lib/types";

function mediaUrl(callId: string, kind: "stream" | "download"): string {
  const token = getToken();
  const path =
    kind === "download"
      ? `${API_URL}/calls/${callId}/recording/download`
      : `${API_URL}/calls/${callId}/recording`;
  if (!token) return path;
  const qs = new URLSearchParams({ access_token: token });
  return `${path}?${qs.toString()}`;
}

export function CallRecordingPlayer({
  callId,
  recording,
  compact = false,
}: {
  callId: string;
  recording: CallRecording | null | undefined;
  compact?: boolean;
}) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [volume, setVolume] = useState(1);
  const [error, setError] = useState<string | null>(null);

  const status = recording?.status ?? null;
  const ready = Boolean(recording?.available || (status === "ready" && recording?.playback_endpoint));
  const streamSrc = useMemo(() => (ready ? mediaUrl(callId, "stream") : ""), [callId, ready]);

  if (!recording || !status) {
    return <p className="text-sm text-moss/60">No recording for this call.</p>;
  }

  if (status === "processing" || status === "recording" || status === "pending") {
    return (
      <p className="text-sm text-moss/70">Processing… recording will appear here when ready.</p>
    );
  }

  if (status === "failed" || !ready) {
    return <p className="text-sm text-red-700">Recording unavailable</p>;
  }

  const displayDuration =
    duration > 0 ? duration : recording.duration_seconds != null ? recording.duration_seconds : 0;

  async function togglePlay() {
    const audio = audioRef.current;
    if (!audio) return;
    setError(null);
    if (playing) {
      audio.pause();
      setPlaying(false);
      return;
    }
    try {
      await audio.play();
      setPlaying(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Playback failed");
      setPlaying(false);
    }
  }

  function handleDownload() {
    const anchor = document.createElement("a");
    anchor.href = mediaUrl(callId, "download");
    anchor.download = `call_${callId.slice(0, 8)}.${(recording?.format || "mp3").toLowerCase()}`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }

  if (compact) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <audio
          ref={audioRef}
          src={streamSrc}
          preload="none"
          onEnded={() => setPlaying(false)}
          onPause={() => setPlaying(false)}
          onPlay={() => setPlaying(true)}
        />
        <button
          type="button"
          onClick={() => void togglePlay()}
          className="rounded-md bg-moss px-2.5 py-1 text-xs font-semibold text-sand"
        >
          {playing ? "Pause" : "Play"}
        </button>
        <button
          type="button"
          onClick={handleDownload}
          className="rounded-md border border-moss/20 bg-white px-2.5 py-1 text-xs font-semibold text-moss"
        >
          Download
        </button>
        {error ? <span className="text-xs text-red-700">{error}</span> : null}
      </div>
    );
  }

  return (
    <div className="space-y-3 rounded-lg border border-moss/10 bg-mist/40 px-4 py-3">
      <audio
        ref={audioRef}
        src={streamSrc}
        preload="metadata"
        onTimeUpdate={() => setCurrentTime(audioRef.current?.currentTime ?? 0)}
        onLoadedMetadata={() => setDuration(audioRef.current?.duration || 0)}
        onEnded={() => setPlaying(false)}
        onPause={() => setPlaying(false)}
        onPlay={() => setPlaying(true)}
        onVolumeChange={() => {
          if (audioRef.current) setVolume(audioRef.current.volume);
        }}
      />

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void togglePlay()}
          className="rounded-md bg-moss px-3 py-1.5 text-sm font-semibold text-sand"
        >
          {playing ? "Pause" : "Play"}
        </button>
        <button
          type="button"
          onClick={handleDownload}
          className="rounded-md border border-moss/20 bg-white px-3 py-1.5 text-sm font-semibold text-moss"
        >
          Download MP3
        </button>
        <span className="font-mono text-xs text-moss/65">
          {formatDuration(Math.floor(currentTime))} / {formatDuration(Math.floor(displayDuration))}
          {recording.format ? ` · ${recording.format}` : ""}
        </span>
      </div>

      <input
        type="range"
        min={0}
        max={Math.max(displayDuration, 0.1)}
        step={0.1}
        value={Math.min(currentTime, displayDuration || 0)}
        onChange={(e) => {
          const next = Number(e.target.value);
          setCurrentTime(next);
          if (audioRef.current) {
            audioRef.current.currentTime = next;
          }
        }}
        className="w-full accent-moss"
        aria-label="Seek recording"
      />

      <label className="flex items-center gap-2 text-xs text-moss/70">
        Volume
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={volume}
          onChange={(e) => {
            const next = Number(e.target.value);
            setVolume(next);
            if (audioRef.current) audioRef.current.volume = next;
          }}
          className="w-32 accent-moss"
        />
      </label>

      {error ? <p className="text-xs text-red-700">{error}</p> : null}
    </div>
  );
}
