import z from 'zod'
import { createFileRoute } from '@tanstack/react-router'
import { Projects } from '@/features/projects'
import { statuses } from '@/features/projects/data/data'

const projectSearchSchema = z.object({
  page: z.number().optional().catch(1),
  pageSize: z.number().optional().catch(10),
  status: z
    .array(z.enum(statuses.map((status) => status.value)))
    .optional()
    .catch([]),
  filter: z.string().optional().catch(''),
})

export const Route = createFileRoute('/_authenticated/projects/')({
  validateSearch: projectSearchSchema,
  component: Projects,
})
