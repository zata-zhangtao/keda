import { AgentForm } from "@/components/agent/agent-form"

/** Render the newagent page. */
export default function NewAgentPage() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-3xl font-bold">新建 Agent</h1>
        <p className="text-muted-foreground">配置 Agent 的提示词、模型和工具</p>
      </div>
      <AgentForm />
    </div>
  )
}
