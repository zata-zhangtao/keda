import { type Project } from './schema'

export const projects: Project[] = [
  {
    id: 'PRJ-001',
    name: 'Zata 核心平台',
    description: '统一后端服务与基础设施搭建',
    status: 'active',
    owner: 'Alice Chen',
    createdAt: '2026-01-15T08:00:00Z',
  },
  {
    id: 'PRJ-002',
    name: '管理后台前端',
    description: '基于 React + shadcn/ui 的管理界面',
    status: 'active',
    owner: 'Bob Liu',
    createdAt: '2026-02-10T08:00:00Z',
  },
  {
    id: 'PRJ-003',
    name: '移动端适配',
    description: 'PWA 与响应式布局优化',
    status: 'planning',
    owner: 'Carol Wang',
    createdAt: '2026-03-05T08:00:00Z',
  },
  {
    id: 'PRJ-004',
    name: '数据迁移工具',
    description: '旧系统数据清洗与导入脚本',
    status: 'archived',
    owner: 'David Zhang',
    createdAt: '2025-11-20T08:00:00Z',
  },
  {
    id: 'PRJ-005',
    name: 'AI 助手集成',
    description: '接入 LLM 提供智能问答与代码审查',
    status: 'active',
    owner: 'Eve Li',
    createdAt: '2026-04-01T08:00:00Z',
  },
  {
    id: 'PRJ-006',
    name: '权限系统重构',
    description: 'RBAC 与资源级权限控制',
    status: 'planning',
    owner: 'Frank Zhao',
    createdAt: '2026-05-12T08:00:00Z',
  },
]
