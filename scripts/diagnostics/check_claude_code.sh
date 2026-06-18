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

parse_myip_json() {
    if ! command -v python3 >/dev/null 2>&1; then
        return 1
    fi

    printf '%s' "$1" | python3 -c '
import json
import sys

try:
    myip_payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)

ip_address = myip_payload.get("ip", "")
country_code = myip_payload.get("cc", "")

print(ip_address.strip() if isinstance(ip_address, str) else "")
print(country_code.strip() if isinstance(country_code, str) else "")
print("")
'
}

parse_httpbin_ip_json() {
    if ! command -v python3 >/dev/null 2>&1; then
        return 1
    fi

    printf '%s' "$1" | python3 -c '
import json
import sys

try:
    httpbin_payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)

origin_value = httpbin_payload.get("origin", "")
if not isinstance(origin_value, str):
    raise SystemExit(1)

first_origin_ip = origin_value.split(",")[0].strip()
print(first_origin_ip)
print("")
print("")
'
}

print_response_summary() {
    local response_payload="$1"
    local response_summary

    response_summary=$(printf '%s' "$response_payload" | tr '\n' ' ' | sed 's/[[:space:]][[:space:]]*/ /g' | cut -c 1-160)
    if [ -n "$response_summary" ]; then
        echo "      响应摘要: $response_summary"
    fi
}

fetch_json_payload() {
    local source_name="$1"
    local source_url="$2"
    local curl_stderr_file
    local curl_combined_output
    local curl_exit_code
    local curl_error_text
    local http_status
    local response_payload

    # Capture stderr to a separate file so curl warnings (TLS notices, captive
    # portals, etc.) cannot interleave into the response body we parse.
    curl_stderr_file=$(mktemp)
    curl_combined_output=$(curl -sS -m 5 -w "\n%{http_code}" "$source_url" 2>"$curl_stderr_file")
    curl_exit_code=$?
    curl_error_text=$(cat "$curl_stderr_file")
    rm -f "$curl_stderr_file"

    if [ "$curl_exit_code" -ne 0 ]; then
        echo "   ⚠️ $source_name 请求失败 (curl exit $curl_exit_code)，改用备用查询源。"
        print_response_summary "$curl_error_text"
        return 1
    fi

    http_status="${curl_combined_output##*$'\n'}"
    response_payload="${curl_combined_output%$'\n'$http_status}"

    if [ "$http_status" != "200" ]; then
        echo "   ⚠️ $source_name 返回 HTTP ${http_status}，改用备用查询源。"
        print_response_summary "$response_payload"
        return 1
    fi

    FETCHED_JSON_PAYLOAD="$response_payload"
}

set_parsed_ip_fields() {
    local parsed_ip_fields="$1"

    IP=$(printf '%s\n' "$parsed_ip_fields" | sed -n '1p')
    COUNTRY=$(printf '%s\n' "$parsed_ip_fields" | sed -n '2p')
    ORG=$(printf '%s\n' "$parsed_ip_fields" | sed -n '3p')
}

query_ipinfo_source() {
    local parsed_ip_fields

    if ! fetch_json_payload "ipinfo.io" "https://ipinfo.io/json"; then
        return 1
    fi
    if ! parsed_ip_fields=$(parse_ip_info_json "$FETCHED_JSON_PAYLOAD"); then
        echo "   ⚠️ 无法解析 ipinfo.io 返回的 JSON 响应，改用备用查询源。"
        print_response_summary "$FETCHED_JSON_PAYLOAD"
        return 1
    fi

    set_parsed_ip_fields "$parsed_ip_fields"
    IP_SOURCE="ipinfo.io"
}

query_myip_source() {
    local parsed_ip_fields

    if ! fetch_json_payload "api.myip.com" "https://api.myip.com"; then
        return 1
    fi
    if ! parsed_ip_fields=$(parse_myip_json "$FETCHED_JSON_PAYLOAD"); then
        echo "   ⚠️ 无法解析 api.myip.com 返回的 JSON 响应，改用备用查询源。"
        print_response_summary "$FETCHED_JSON_PAYLOAD"
        return 1
    fi

    set_parsed_ip_fields "$parsed_ip_fields"
    IP_SOURCE="api.myip.com"
}

query_httpbin_source() {
    local parsed_ip_fields

    if ! fetch_json_payload "httpbin.org" "https://httpbin.org/ip"; then
        return 1
    fi
    if ! parsed_ip_fields=$(parse_httpbin_ip_json "$FETCHED_JSON_PAYLOAD"); then
        echo "   ⚠️ 无法解析 httpbin.org 返回的 JSON 响应。"
        print_response_summary "$FETCHED_JSON_PAYLOAD"
        return 1
    fi

    set_parsed_ip_fields "$parsed_ip_fields"
    IP_SOURCE="httpbin.org"
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
FETCHED_JSON_PAYLOAD=""
IP_SOURCE=""
IP=""
COUNTRY=""
ORG=""
if query_ipinfo_source || query_myip_source || query_httpbin_source; then
    if [ -z "$IP" ]; then
        echo "   ❌ 获取到了 IP 查询响应，但没有解析到出口 IP。"
    else
        echo "   查询源: $IP_SOURCE"
        echo "   当前 IP: $IP"

        if [ -n "$COUNTRY" ]; then
            echo "   所在国家: $COUNTRY"
        else
            echo "   所在国家: 未知（$IP_SOURCE 不提供地区字段）"
        fi

        if [ -n "$ORG" ]; then
            echo "   网络提供商: $ORG"
        else
            echo "   网络提供商: 未知（$IP_SOURCE 不提供网络提供商字段）"
        fi

        # 常见的 Claude 不支持地区
        if [ -z "$COUNTRY" ]; then
            echo "   ⚠️ 关键检查未执行: 当前查询源（$IP_SOURCE）不返回地区字段，无法验证节点是否位于 Claude 不支持的地区。"
            echo "      这不代表地区没问题。若节点位于 CN/HK/RU/MO 等地区，Claude 仍会返回 403。"
            echo "      请在代理客户端手动确认出口节点地区，或换用可返回地区信息的网络后重新运行本检查。"
        elif [[ "$COUNTRY" == "CN" || "$COUNTRY" == "HK" || "$COUNTRY" == "RU" || "$COUNTRY" == "MO" ]]; then
            echo "   ❌ 严重警告: 你的节点位于不支持的地区 ($COUNTRY)！Claude 极可能会在登录或使用时拒绝服务(返回 403)。请更换欧美、日韩、新加坡等节点。"
        else
            echo "   ✅ IP 地区 ($COUNTRY) 看起来没问题。"
        fi
    fi
else
    echo "   ❌ 所有 IP 查询源都失败，无法获取出口 IP；这时才需要检查代理、DNS 或网络是否彻底断开。"
fi
echo "=================================================="
echo "🏁 检测完成。"
