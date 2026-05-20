#!/bin/bash

parse_ip_info_json() {
    if ! command -v python3 >/dev/null 2>&1; then
        return 1
    fi

    printf '%s' "$1" | python3 -c '
import json
import sys

try:
    ip_info_payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)

for field_name in ("ip", "country", "org"):
    field_value = ip_info_payload.get(field_name, "")
    print(field_value.strip() if isinstance(field_value, str) else "")
'
}

echo "🔍 开始检测 Claude Code 终端网络环境..."
echo "=================================================="

# 1. 检查环境变量中的代理设置
echo "🌐 1. 当前终端代理环境变量:"
if [ -z "${HTTP_PROXY:-}" ] && [ -z "${HTTPS_PROXY:-}" ] && [ -z "${http_proxy:-}" ] && [ -z "${https_proxy:-}" ] && [ -z "${all_proxy:-}" ]; then
    echo "   ⚠️ 未检测到任何代理环境变量！"
    echo "   如果你的网络需要代理，请先执行类似: export HTTPS_PROXY=http://127.0.0.1:7890"
else
    [ -n "$HTTP_PROXY" ] && echo "   HTTP_PROXY  = $HTTP_PROXY"
    [ -n "$HTTPS_PROXY" ] && echo "   HTTPS_PROXY = $HTTPS_PROXY"
    [ -n "$http_proxy" ] && echo "   http_proxy  = $http_proxy"
    [ -n "$https_proxy" ] && echo "   https_proxy = $https_proxy"
    [ -n "$all_proxy" ] && echo "   all_proxy   = $all_proxy"
fi
echo "=================================================="

# 2. 定义要测试的域名 (Claude 核心服务)
DOMAINS=(
    "https://api.anthropic.com"
    "https://claude.ai"
)

# 3. 测试连通性
echo "📡 2. 测试 Claude 服务连通性 (超时时间设为 5 秒)..."
for domain in "${DOMAINS[@]}"; do
    # 使用 curl 获取 HTTP 状态码
    HTTP_STATUS=$(curl -o /dev/null -s -w "%{http_code}\n" --connect-timeout 5 "$domain")

    case "$HTTP_STATUS" in
        ""|000)
            echo "   ❌ 失败: 无法连接到 $domain (连接超时、代理异常或被拒绝)"
            ;;
        2??|3??)
            echo "   ✅ 成功: $domain (HTTP 状态码: $HTTP_STATUS)"
            ;;
        403)
            echo "   ⚠️ 可达: $domain (HTTP 状态码: 403，说明服务可达，但当前请求被拒绝)"
            ;;
        404)
            echo "   ⚠️ 可达: $domain (HTTP 状态码: 404，说明域名可达，但当前路径不是有效页面)"
            ;;
        ???)
            echo "   ⚠️ 可达: $domain (HTTP 状态码: $HTTP_STATUS，说明服务有响应，但结果不算正常)"
            ;;
        *)
            echo "   ❌ 失败: $domain 返回了无法识别的状态值: $HTTP_STATUS"
            ;;
    esac
done
echo "=================================================="

# 4. 测试归属地验证
echo "🌍 3. 测试当前终端出口 IP 与地区..."
if IP_INFO=$(curl -fsS -m 5 https://ipinfo.io/json 2>/dev/null); then
    if PARSED_IP_INFO=$(parse_ip_info_json "$IP_INFO"); then
        IP=$(printf '%s\n' "$PARSED_IP_INFO" | sed -n '1p')
        COUNTRY=$(printf '%s\n' "$PARSED_IP_INFO" | sed -n '2p')
        ORG=$(printf '%s\n' "$PARSED_IP_INFO" | sed -n '3p')

        if [ -z "$IP" ] || [ -z "$COUNTRY" ] || [ -z "$ORG" ]; then
            echo "   ❌ 获取到了 IP 信息响应，但关键字段不完整，无法判断当前出口地区。"
            echo "   原始响应: $IP_INFO"
        else
            echo "   当前 IP: $IP"
            echo "   所在国家: $COUNTRY"
            echo "   网络提供商: $ORG"

            # 常见的 Claude 不支持地区
            if [[ "$COUNTRY" == "CN" || "$COUNTRY" == "HK" || "$COUNTRY" == "RU" || "$COUNTRY" == "MO" ]]; then
                echo "   ❌ 严重警告: 你的节点位于不支持的地区 ($COUNTRY)！Claude 极可能会在登录或使用时拒绝服务(返回 403)。请更换欧美、日韩、新加坡等节点。"
            else
                echo "   ✅ IP 地区 ($COUNTRY) 看起来没问题。"
            fi
        fi
    else
        echo "   ❌ 无法解析 ipinfo.io 返回的 JSON 响应。"
        echo "   原始响应: $IP_INFO"
    fi
else
    echo "   ❌ 无法获取 IP 信息，请检查网络是否彻底断开。"
fi
echo "=================================================="
echo "🏁 检测完成。"
