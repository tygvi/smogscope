import { formatTime } from '../hooks'

interface Props {
  times: string[]
  index: number | null            // null = live
  playing: boolean
  onChange: (i: number | null) => void
  onTogglePlay: () => void
}

/** 72-hour playback: scrub or play through past hours; "Live" returns to now. */
export default function TimelineBar({ times, index, playing, onChange, onTogglePlay }: Props) {
  if (times.length < 2) return null
  const last = times.length - 1
  const current = index ?? last
  const live = index == null

  return (
    <div className="timeline glass">
      <button className="play-btn" onClick={onTogglePlay} aria-label={playing ? 'Pause playback' : 'Play last 72 hours'}>
        {playing ? '❚❚' : '▶'}
      </button>
      <div className="timeline-track">
        <input
          type="range"
          min={0}
          max={last}
          value={current}
          onChange={(e) => {
            const i = Number(e.target.value)
            onChange(i === last ? null : i)
          }}
          aria-label="Time"
          aria-valuetext={formatTime(times[current])}
        />
        <div className="timeline-labels">
          <span>{formatTime(times[0])}</span>
          <strong className={live ? 'live' : ''}>{live ? '● Live' : formatTime(times[current])}</strong>
          <span>{formatTime(times[last])}</span>
        </div>
      </div>
      <button className="live-btn" onClick={() => onChange(null)} disabled={live}>Live</button>
    </div>
  )
}
