import type { Page } from '@playwright/test'
import { expect } from '@playwright/test'
import type { Credentials } from '../support/env'

/**
 * Page object for the application login screen.
 * Adapt the selectors to match your actual login form.
 */
export class LoginPage {
  constructor(private readonly page: Page) {}

  /** Waits for the login form to be interactive. */
  async waitForReady(): Promise<void> {
    // TODO: replace with a selector that uniquely identifies your login form
    await expect(this.page.locator('form')).toBeVisible()
  }

  /** Fills credentials and submits the login form. */
  async login(credentials: Credentials): Promise<void> {
    // TODO: replace with your login form's field selectors
    await this.page.getByLabel('Username').fill(credentials.identifier)
    await this.page.getByLabel('Password').fill(credentials.password)
    await this.page.getByRole('button', { name: /sign in|log in/i }).click()
  }
}
