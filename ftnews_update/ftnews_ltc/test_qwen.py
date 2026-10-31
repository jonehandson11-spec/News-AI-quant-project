from openai import OpenAI
import json

# 连接本地 Ollama 引擎（端口固定为 11434，api_key 随便填）
client = OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama"
)

# 模拟一条刚才从 FT 抓下来的真实时政新闻
news_sample = "The US government announced sweeping new export controls on advanced semiconductor manufacturing equipment, citing national security concerns."

print("正在调用本地 RTX 5070 Ti 上的 Qwen2.5-14B 分析新闻...")

response = client.chat.completions.create(
    model="qwen2.5:14b",
    messages=[
        {
            "role": "system",
            "content": (
                "你是一个量化金融时政分析师。请分析新闻并严格只输出 JSON 格式，包含以下字段：\n"
                "{\n"
                '  "event_type": "string (如 export_control, monetary_policy, geopolitics)",\n'
                '  "sector": "string (受影响行业板块)",\n'
                '  "sentiment_score": float (范围 -1.0 到 1.0, 负数利空, 正数利好),\n'
                '  "brief_reason": "string (简短分析理由)"\n'
                "}"
            )
        },
        {"role": "user", "content": news_sample}
    ],
    response_format={"type": "json_object"} # 强制开启纯净 JSON 模式
)

# 解析返回结果
result = json.loads(response.choices[0].message.content)
print("\n✅ 本地模型解析成功：")
print(json.dumps(result, indent=2, ensure_ascii=False))