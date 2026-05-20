type CleanupAction = () => Promise<void>

/**
 * Collects async cleanup callbacks and runs them in reverse registration order.
 * Use inside fixtures to guarantee teardown even when tests fail.
 *
 * Example:
 *   const registry = new CleanupRegistry()
 *   registry.add(async () => { await api.deleteResource(id) })
 *   try { await use(registry) } finally { await registry.runAll() }
 */
export class CleanupRegistry {
  private readonly cleanupActionList: CleanupAction[] = []

  add(cleanupAction: CleanupAction): void {
    this.cleanupActionList.push(cleanupAction)
  }

  async runAll(): Promise<void> {
    const errorList: Error[] = []

    for (const action of [...this.cleanupActionList].reverse()) {
      try {
        await action()
      } catch (error) {
        errorList.push(error instanceof Error ? error : new Error(String(error)))
      }
    }

    if (errorList.length > 0) {
      throw new AggregateError(errorList, 'One or more Playwright cleanup actions failed.')
    }
  }
}
