import { Circle, PauseCircle, PlayCircle } from 'lucide-react'

export const statuses = [
  {
    value: 'active',
    label: '进行中',
    icon: PlayCircle,
  },
  {
    value: 'planning',
    label: '规划中',
    icon: Circle,
  },
  {
    value: 'archived',
    label: '已归档',
    icon: PauseCircle,
  },
]
