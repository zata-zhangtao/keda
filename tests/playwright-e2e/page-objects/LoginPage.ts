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
    await expect(this.page.locator('form')).toBeVisible()
    await expect(this.page.getByTestId('login-submit-button')).toBeVisible()
  }

  /** Fills credentials and submits the login form. */
  async login(credentials: Credentials): Promise<void> {
    await this.page.getByTestId('login-identifier-input').fill(credentials.identifier)
    await this.page.getByTestId('login-password-input').fill(credentials.password)
    await this.page.getByTestId('login-submit-button').click()
  }
}
