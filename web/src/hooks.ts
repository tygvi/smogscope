import { useEffect, useState } from 'react'

export interface Polled<T> {
  data: T | null
  error: Error | null
}

/**
 * Fetch now, then again every `intervalMs`. Keeps the last good data when a
 * refresh fails (a brief API hiccup shouldn't blank the map). Cancels in-flight
 * requests when the component unmounts or `key` changes.
 */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
  key: string = '',
): Polled<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    const load = () =>
      fetcher(controller.signal)
        .then((d) => { setData(d); setError(null) })
        .catch((e: unknown) => {
          if (!controller.signal.aborted) setError(e instanceof Error ? e : new Error(String(e)))
        })
    load()
    const id = window.setInterval(load, intervalMs)
    return () => { controller.abort(); window.clearInterval(id) }
  }, [intervalMs, key])

  return { data, error }
}

/** True when the user asked their OS for reduced motion. */
export function usePrefersReducedMotion(): boolean {
  const query = '(prefers-reduced-motion: reduce)'
  const [reduced, setReduced] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mql = window.matchMedia(query)
    const onChange = () => setReduced(mql.matches)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [])
  return reduced
}

const timeFmt = new Intl.DateTimeFormat('en-IN', {
  weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  hour12: false, timeZone: 'Asia/Kolkata',
})
export const formatTime = (iso: string) => timeFmt.format(new Date(iso))
