import { teardownPlaywrightStack } from './stack-control.mjs'

export default async function globalTeardown() {
  await teardownPlaywrightStack()
}
