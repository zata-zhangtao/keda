import { useEffect, useState } from 'react'

import { loadManifest, shouldShow, type WhatsNewPayload } from '@/lib/whats-new'

import { WhatsNewDialog } from './whats-new-dialog'

/**
 * Mounts once at the application root. Loads the build-time manifest and
 * renders the {@link WhatsNewDialog} when a new production version is
 * detected. Staging builds never trigger the modal.
 */
export function WhatsNewHost() {
  const [payload, setPayload] = useState<WhatsNewPayload | null>(null)

  useEffect(() => {
    let cancelled = false

    void loadManifest().then((manifest) => {
      if (cancelled) return
      const decision = shouldShow(manifest)
      if (decision.kind === 'show') {
        setPayload(decision.payload)
      }
    })

    return () => {
      cancelled = true
    }
  }, [])

  if (payload === null) return null
  return <WhatsNewDialog payload={payload} />
}
