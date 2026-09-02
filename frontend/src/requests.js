import { useEffect, useRef } from 'react'

/**
 * Keep only the newest in-flight request for a view.
 *
 * Filters change faster than the server answers, and every one of these pages
 * refetches on every change. Without this, a slow earlier response could land
 * after a newer one and leave the table showing results for a filter the user
 * had already moved on from — and a page left mid-load kept fetching.
 *
 * next() hands out an AbortController and cancels the one before it. Anything
 * still running is dropped when the component unmounts.
 */
export function useLatestRequest() {
  const current = useRef(null)
  useEffect(() => () => current.current?.abort(), [])
  return {
    next() {
      current.current?.abort()
      current.current = new AbortController()
      return current.current
    },
    // Whether this attempt is still the one the view is waiting for. A superseded
    // attempt must not touch loading or error state that belongs to its successor.
    isCurrent: (controller) => current.current === controller,
  }
}

/** A request we cancelled ourselves — never an error worth showing. */
export const isAbort = (error) => error?.name === 'AbortError'
