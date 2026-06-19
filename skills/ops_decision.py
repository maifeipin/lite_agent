import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import concurrent.futures
from pydantic import BaseModel, Field
from typing import List, Dict, Literal, Optional
from skill_engine import skill
from config_loader import load_config
from model_router import ModelRouter

# =========================================================
# 1. Schema 定义 (稳定输出协议)
# =========================================================
class CriteriaScore(BaseModel):
    name: str = Field(description="维度名称")
    score: int = Field(ge=0, le=100, description="该维度的得分(0-100)")
    weight: float = Field(ge=0, le=1.0, description="该维度在总分中的权重")
    rationale: str = Field(description="打分理由")

class DecisionResult(BaseModel):
    decision_type: str = Field(description="结论类型方向，例如：'值得执行', '高风险放弃', '供大于求'")
    base_scores: Dict[str, int] = Field(description="固定四维得分，包含 profitability, execution, timeliness, confidence")
    extra_scores: List[CriteriaScore] = Field(description="额外维度的得分", default_factory=list)
    overall_score: int = Field(ge=0, le=100, description="计算出的综合得分")
    reasoning: str = Field(description="核心论证逻辑")
    evidence_gaps: List[str] = Field(description="缺失的证据或前提假设", default_factory=list)
    action_item: str = Field(description="下一步建议的行动指令")

class PeerReview(BaseModel):
    reviewed_model_id: str
    valid_points: List[str] = Field(description="对方有道理的观点")
    missing_evidence: List[str] = Field(description="对方缺失的证据")
    logic_errors: List[str] = Field(description="对方推理错误或事实遗漏的地方")
    disagreement_type: Literal["fact", "weight", "preference", "uncertainty"] = Field(description="分歧类型")
    suggested_score_delta: int = Field(ge=-20, le=20, description="建议总分调整量")
    should_escalate_to_human: bool = Field(description="是否因为分歧严重而必须交由人类裁决")

# =========================================================
# 2. 核心调度与实现
# =========================================================
def _get_models_from_config() -> List[str]:
    cfg = load_config()
    # 默认调用系统中比较强力的几个模型
    return cfg.get("committee", {}).get("models", ["claude-3-5-sonnet", "deepseek-v4-pro"])

def _get_task_profile(task_type: str) -> dict:
    cfg = load_config()
    profiles = cfg.get("committee", {}).get("task_profiles", {})
    return profiles.get(task_type, {
        "base_weights": {
            "profitability": 0.4, 
            "execution": 0.3, 
            "timeliness": 0.2, 
            "confidence": 0.1
        },
        "extra_criteria": [],
        "description": "通用决策评估"
    })

def _call_model(router: ModelRouter, model_name: str, prompt: str, schema_cls) -> str:
    """并发调用大模型，并强制输出指定 Schema"""
    client = router.get_client(model_name)
    provider = router.get_provider(model_name)
    if not client:
        return '{"error": "Model client not found"}'
        
    sys_prompt = f"You are an expert AI committee judge. You must output valid JSON matching exactly this schema:\n{schema_cls.model_json_schema()}"
    
    try:
        model_id = router.models_cfg.get(model_name, {}).get("model", model_name)
        if provider == "gemini":
            res = client.models.generate_content(
                model=model_id,
                contents=sys_prompt + "\n\n" + prompt
            )
            return res.text
        else:
            # 兼容 OpenAI 格式
            response = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                timeout=45
            )
            return response.choices[0].message.content
    except Exception as e:
        return f'{{"error": "{str(e)}" }}'

def _calculate_variance(scores: List[int]) -> float:
    if len(scores) < 2: return 0.0
    mean = sum(scores) / len(scores)
    variance = sum((x - mean) ** 2 for x in scores) / len(scores)
    return variance ** 0.5

@skill(
    name='ops_decision',
    description='多模型评判委员会引擎。当你需要对某个长篇复杂报告、需求、或者价格趋势进行多个专家模型的并行综合打分与盲审时调用。',
    params={
        'task_type': {'type': 'string', 'description': '评估的任务类型，如果没有指定请传 default', 'default': 'default'},
        'topic': {'type': 'string', 'description': '需要交给委员会进行评估的议题（或长文本 Raw Data 摘要）'}
    }
)
def ops_decision(task_type: str, topic: str) -> str:
    router = ModelRouter(load_config())
    models = _get_models_from_config()
    profile = _get_task_profile(task_type)
    
    # 构建输入 Prompt
    prompt = f"任务配置指南与权重: {json.dumps(profile, ensure_ascii=False)}\n\n请严格按照上述 Schema 对以下议题进行打分和评价：\n\n{topic}"
    
    results = {}
    scores = []
    
    # 步骤二：并发调用模型数组
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as executor:
        future_to_model = {executor.submit(_call_model, router, m, prompt, DecisionResult): m for m in models}
        for future in concurrent.futures.as_completed(future_to_model):
            m = future_to_model[future]
            res_str = future.result()
            try:
                # 清理首尾非 JSON 字符
                start = res_str.find('{')
                end = res_str.rfind('}') + 1
                if start != -1 and end != 0:
                    res_str = res_str[start:end]
                
                res_obj = DecisionResult.model_validate_json(res_str)
                results[m] = res_obj
                scores.append(res_obj.overall_score)
            except Exception as e:
                results[m] = f"解析失败 (Error: {e})\nRaw: {res_str[:150]}..."
                
    # 步骤三：计算标准差及核心字段一致性
    if not scores:
        return f"🚨 所有模型均未能成功输出合法的评分 Schema，原始结果: {results}"
        
    std_dev = _calculate_variance(scores)
    
    output = [f"📊 委员会评审完毕 | 参与评委数: {len(results)} | 得分方差: {std_dev:.2f}", ""]
    
    for m, res in results.items():
        if isinstance(res, DecisionResult):
            output.append(f"### 🤖 评委 [{m}]: `{res.overall_score}分` | `{res.decision_type}`")
            output.append(f"- **核心理由**: {res.reasoning}")
            if res.evidence_gaps:
                output.append(f"- **证据缺失**: {', '.join(res.evidence_gaps)}")
            output.append(f"- **行动项**: {res.action_item}\n")
        else:
            output.append(f"### 🤖 评委 [{m}]: 评估失败")
            output.append(f"- {res}\n")
            
    output.append("---")
    
    # 步骤四：分歧判断与处理机制
    if std_dev > 25:
        output.append("🚨 **高级警报：模型重度分歧 (方差>25)**")
        output.append("> 已熔断后续自动判定流程，请人类介入查阅上方详细 reasoning。")
    elif std_dev > 12:
        output.append("⚠️ **警告：模型中度分歧**")
        output.append("> (根据架构决议，V2 将在此处触发一轮 `PeerReview` 进行结构化纠错。目前处于 MVP 阶段，请人类参阅分歧项)")
    else:
        output.append("✅ **委员会达成高度共识**")
        output.append("> 可以信任并执行评委们给出的下一步 Action Item。")
        
    return "\n".join(output)
