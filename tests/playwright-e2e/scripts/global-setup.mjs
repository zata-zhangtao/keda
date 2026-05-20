import { ensurePlaywrightStackReady } from './stack-control.mjs'

export default async function globalSetup() {
  await ensurePlaywrightStackReady()
}
