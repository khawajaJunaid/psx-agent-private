import json
from tools.llm import complete

def analyse_sentiment(company_name, headlines):
    if not headlines:
        return {"company": company_name, "sentiment": "neutral", "score": 0.0, "summary": "No recent news found."}
    headlines_text = "\n".join(f"- {h}" for h in headlines)
    prompt = f"""
You are a financial analyst reviewing news about {company_name}, listed on the Pakistan Stock Exchange (PSX).
Recent headlines:
{headlines_text}
Respond ONLY with a JSON object (no markdown, no explanation):
{{"sentiment": "positive" | "neutral" | "negative", "score": <float -1.0 to 1.0>, "summary": "<one sentence>", "key_themes": ["<theme1>", "<theme2>"]}}
"""
    raw = complete(None, prompt, max_tokens=300)
    try:
        result = json.loads(raw)
        result["company"] = company_name
        return result
    except json.JSONDecodeError:
        return {"company": company_name, "sentiment": "neutral", "score": 0.0, "summary": raw}
