import { z } from 'zod'

export const projectSchema = z.object({
  id: z.string(),
  name: z.string(),
  description: z.string(),
  status: z.enum(['active', 'archived', 'planning']),
  owner: z.string(),
  createdAt: z.string(),
})

export type Project = z.infer<typeof projectSchema>
