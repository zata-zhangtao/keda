import type { NextConfig } from "next"

const backendUrl = process.env.BACKEND_URL || "http://localhost:8000"

const nextConfig: NextConfig = {
  devIndicators: false,
  allowedDevOrigins: ["127.0.0.1"],
  async rewrites() {
    return [
      {
        // keda 后端路由注册在 /api 前缀下（如 /api/auth/*、/api/v1/agent-runner/*），
        // 必须原样透传，不能像模板原状那样剥掉 /api。
        source: "/api/:path*",
        destination: `${backendUrl}/api/:path*`,
      },
    ]
  },
}

export default nextConfig
