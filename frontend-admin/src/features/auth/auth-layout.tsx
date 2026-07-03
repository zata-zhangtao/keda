import { Logo } from '@/assets/logo'

type AuthLayoutProps = {
  children: React.ReactNode
}

/** Root layout for the auth section. */
export function AuthLayout({ children }: AuthLayoutProps) {
  return (
    <div className='grid min-h-svh lg:grid-cols-2'>
      {/* Left brand panel */}
      <div className='relative hidden flex-col justify-between overflow-hidden bg-zinc-900 p-10 text-white lg:flex'>
        <div className='absolute inset-0 bg-[radial-gradient(circle_at_top_right,_var(--tw-gradient-stops))] from-indigo-600/20 via-zinc-900 to-zinc-950' />
        <div className='absolute -bottom-24 -left-24 size-96 rounded-full bg-indigo-600/10 blur-3xl' />

        <div className='relative z-10 flex items-center gap-2'>
          <Logo className='size-7 text-white' />
          <span className='text-xl font-semibold tracking-tight'>Zata</span>
        </div>

        <div className='relative z-10 max-w-md'>
          <h2 className='text-3xl font-bold tracking-tight'>
            构建下一代管理平台
          </h2>
          <p className='mt-4 text-zinc-400'>
            统一项目、任务与团队协作，让复杂流程变得简单可追踪。
          </p>
          <ul className='mt-8 space-y-3 text-sm text-zinc-300'>
            <li className='flex items-center gap-2'>
              <span className='inline-block size-1.5 rounded-full bg-indigo-400' />
              项目与任务一站式管理
            </li>
            <li className='flex items-center gap-2'>
              <span className='inline-block size-1.5 rounded-full bg-indigo-400' />
              实时会话与权限控制
            </li>
            <li className='flex items-center gap-2'>
              <span className='inline-block size-1.5 rounded-full bg-indigo-400' />
              现代化 React + FastAPI 架构
            </li>
          </ul>
        </div>

        <div className='relative z-10 text-sm text-zinc-500'>
          © {new Date().getFullYear()} Zata. All rights reserved.
        </div>
      </div>

      {/* Right form panel */}
      <div className='flex flex-col items-center justify-center p-6 lg:p-10'>
        <div className='flex w-full max-w-sm flex-col gap-6'>
          <div className='flex items-center gap-2 lg:hidden'>
            <Logo className='size-6' />
            <span className='text-lg font-semibold tracking-tight'>Zata</span>
          </div>
          {children}
        </div>
      </div>
    </div>
  )
}
