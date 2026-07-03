import { ContentSection } from '../components/content-section'
import { NotificationsForm } from './notifications-form'

/** Render the SettingsNotifications component. */
export function SettingsNotifications() {
  return (
    <ContentSection
      title='Notifications'
      desc='Configure how you receive notifications.'
    >
      <NotificationsForm />
    </ContentSection>
  )
}
