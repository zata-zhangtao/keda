import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, type RenderResult } from 'vitest-browser-react'
import { type Locator, userEvent } from 'vitest/browser'
import { login, type UserSession } from '@/api/auth'
import { UserAuthForm } from './user-auth-form'

const FORM_MESSAGES = {
  identifierEmpty: '请输入用户名或邮箱',
  passwordEmpty: '请输入密码',
} as const

const MOCK_SESSION: UserSession = {
  user_id: 'user-1',
  display_name: 'Admin User',
  username: 'admin',
}

const navigate = vi.fn()
const setUserMock = vi.fn()

vi.mock('@/stores/auth-store', () => ({
  useAuthStore: () => ({
    auth: {
      setUser: setUserMock,
    },
  }),
}))

vi.mock('@/api/auth', () => ({
  login: vi.fn(),
}))

vi.mock('@tanstack/react-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-router')>()
  return {
    ...actual,
    useNavigate: () => navigate,
  }
})

describe('UserAuthForm', () => {
  describe('Rendering without redirectTo', () => {
    let screen: RenderResult
    let identifierInput: Locator
    let passwordInput: Locator
    let signInButton: Locator
    let forgotPasswordButton: Locator

    beforeEach(async () => {
      vi.clearAllMocks()
      screen = await render(<UserAuthForm />)
      identifierInput = screen.getByRole('textbox', { name: /用户名/ })
      passwordInput = screen.getByLabelText('密码')
      signInButton = screen.getByRole('button', { name: /^登录$/ })
      forgotPasswordButton = screen.getByText(/忘记密码/)
    })

    it('renders fields, submit button, and forgot password button', async () => {
      await expect.element(identifierInput).toBeInTheDocument()
      await expect.element(passwordInput).toBeInTheDocument()
      await expect.element(signInButton).toBeInTheDocument()
      await expect.element(forgotPasswordButton).toBeInTheDocument()
    })

    it('shows validation messages when submitting empty form', async () => {
      await userEvent.click(signInButton)

      await expect
        .element(screen.getByText(FORM_MESSAGES.identifierEmpty))
        .toBeInTheDocument()
      await expect
        .element(screen.getByText(FORM_MESSAGES.passwordEmpty))
        .toBeInTheDocument()
    })

    it('authenticates and navigates to default route on success', async () => {
      vi.mocked(login).mockResolvedValue(MOCK_SESSION)

      await userEvent.fill(identifierInput, 'a@b.com')
      await userEvent.fill(passwordInput, '1234567')

      await userEvent.click(signInButton)

      await vi.waitFor(() => expect(setUserMock).toHaveBeenCalledOnce())
      expect(login).toHaveBeenCalledWith({
        identifier: 'a@b.com',
        password: '1234567',
      })
      expect(setUserMock).toHaveBeenCalledWith(MOCK_SESSION)

      await vi.waitFor(() =>
        expect(navigate).toHaveBeenCalledWith({ to: '/', replace: true })
      )
    })
  })

  it('navigates to redirectTo when provided', async () => {
    vi.clearAllMocks()
    vi.mocked(login).mockResolvedValue(MOCK_SESSION)

    const { getByRole, getByLabelText } = await render(
      <UserAuthForm redirectTo='/settings' />
    )

    await userEvent.fill(getByRole('textbox', { name: /用户名/ }), 'a@b.com')
    await userEvent.fill(getByLabelText('密码'), '1234567')

    await userEvent.click(getByRole('button', { name: /^登录$/ }))

    await vi.waitFor(() => expect(setUserMock).toHaveBeenCalledOnce())

    await vi.waitFor(() =>
      expect(navigate).toHaveBeenCalledWith({
        href: '/settings',
        replace: true,
      })
    )
  })
})
