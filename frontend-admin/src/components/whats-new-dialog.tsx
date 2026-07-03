import { useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { markSeen, type WhatsNewPayload } from '@/lib/whats-new'

type WhatsNewDialogProps = {
  payload: WhatsNewPayload
}

/**
 * Modal that summarises the changes between the previous production version
 * and the current one. Pure presentational: persists the dismissal via
 * {@link markSeen} on close.
 */
export function WhatsNewDialog({ payload }: WhatsNewDialogProps) {
  const [open, setOpen] = useState(true)

  const handleOpenChange = (next: boolean) => {
    if (!next) {
      markSeen(payload.version)
    }
    setOpen(next)
  }

  const visibleGroups = Object.entries(payload.groups).filter(
    ([, items]) => items.length > 0
  )
  const releasedAt = formatDate(payload.generatedAt)

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className='sm:max-w-xl max-h-[80vh] overflow-y-auto'>
        <DialogHeader>
          <DialogTitle>What&apos;s New in {payload.version}</DialogTitle>
          {releasedAt && (
            <DialogDescription>Released {releasedAt}</DialogDescription>
          )}
        </DialogHeader>

        {payload.breaking.length > 0 && (
          <section
            data-testid='whats-new-breaking'
            className='rounded-md border border-destructive/50 bg-destructive/5 p-3'
          >
            <h3 className='text-sm font-semibold text-destructive'>
              Breaking Changes
            </h3>
            <ul className='mt-1 list-disc pl-5 text-sm'>
              {payload.breaking.map((description, index) => (
                <li key={index}>{description}</li>
              ))}
            </ul>
          </section>
        )}

        {visibleGroups.map(([group, items]) => (
          <section key={group} data-testid={`whats-new-group-${slug(group)}`}>
            <h3 className='text-sm font-semibold'>{group}</h3>
            <ul className='mt-1 list-disc pl-5 text-sm'>
              {items.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </section>
        ))}

        {visibleGroups.length === 0 && payload.breaking.length === 0 && (
          <p className='text-sm text-muted-foreground'>
            No notable changes documented for this release.
          </p>
        )}

        <DialogFooter>
          <Button onClick={() => handleOpenChange(false)}>Got it</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Format an ISO timestamp for display in the changelog. */
function formatDate(isoTimestamp: string): string {
  const parsed = new Date(isoTimestamp)
  if (Number.isNaN(parsed.getTime())) return ''
  return parsed.toLocaleDateString()
}

/** Convert a label into a URL-friendly slug. */
function slug(label: string): string {
  return label.toLowerCase().replace(/\s+/g, '-')
}
