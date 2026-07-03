import { Header } from '@/components/layout/header'
import { Main } from '@/components/layout/main'
import { ProfileDropdown } from '@/components/profile-dropdown'
import { Search } from '@/components/search'
import { ThemeSwitch } from '@/components/theme-switch'
import { ProjectsTable } from './components/projects-table'
import { projects } from './data/projects'

/** Render the Projects component. */
export function Projects() {
  return (
    <>
      <Header fixed>
        <Search className='me-auto' />
        <ThemeSwitch />
        <ProfileDropdown />
      </Header>

      <Main className='flex flex-1 flex-col gap-4 sm:gap-6'>
        <div>
          <h2 className='text-2xl font-bold tracking-tight'>项目</h2>
          <p className='text-muted-foreground'>管理和跟踪所有项目进度。</p>
        </div>
        <ProjectsTable data={projects} />
      </Main>
    </>
  )
}
