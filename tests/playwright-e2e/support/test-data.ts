import { randomUUID } from 'node:crypto'

/**
 * Builds a unique, traceable resource name for one test-owned server-side entity.
 * The short UUID suffix makes parallel runs safe and makes logs easy to grep.
 *
 * Example: buildUniqueName('pw-user') → 'pw-user-3f8a1b2c'
 */
export function buildUniqueName(prefix: string): string {
  return `${prefix}-${randomUUID().slice(0, 8)}`
}
