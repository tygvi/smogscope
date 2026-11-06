import { useEffect, useRef, useState } from 'react'
import { animate } from 'framer-motion'

/** A number that counts smoothly from its previous value to the new one. */
export default function AnimatedNumber({ value, duration = 0.9 }: { value: number; duration?: number }) {
  const [shown, setShown] = useState(value)
  const from = useRef(value)

  useEffect(() => {
    const controls = animate(from.current, value, {
      duration,
      ease: [0.16, 1, 0.3, 1],  // fast start, gentle landing
      onUpdate: (v) => { from.current = v; setShown(Math.round(v)) },
    })
    return () => controls.stop()
  }, [value, duration])

  return <>{shown}</>
}
